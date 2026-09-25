import contextlib
import json
import math
import os
import sqlite3
import threading
import time
from pathlib import Path

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, 'data')
DB_PATH = os.environ.get('RIFTSENSE_TIMELINE_DB') or os.path.join(DATA_DIR, 'timeline.db')
DEFAULT_BUDGET = int(os.environ.get('RIFTSENSE_INFERENCE_BUDGET', '40'))
MAX_TEXT = 4000
MAX_JSON = 8000
SCHEMA_VERSION = 2

_lock = threading.Lock()

EVENT_KINDS = (
    'game_start', 'game_end', 'death', 'item', 'objective',
    'heartbeat', 'error', 'status', 'coverage_start', 'coverage_stop',
)
ADVICE_KINDS = ('coach', 'death', 'note')
INFERENCE_STATUS = ('started', 'ok', 'failed', 'timeout', 'skipped',
                    'cancelled', 'superseded')
BUDGET_EXCLUDED = ('skipped', 'cancelled', 'superseded')
COVERAGE_KINDS = ('coverage_start', 'coverage_stop')


def configure(path):
    global DB_PATH
    DB_PATH = path


def _uri(path, readonly=False):
    uri = Path(os.path.abspath(path)).as_uri()
    return uri + '?mode=ro' if readonly else uri


def _connect(readonly=False):
    if readonly:
        con = sqlite3.connect(_uri(DB_PATH, readonly=True), uri=True, timeout=5)
    else:
        directory = os.path.dirname(DB_PATH)
        if directory:
            os.makedirs(directory, exist_ok=True)
        con = sqlite3.connect(DB_PATH, timeout=5)
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('PRAGMA synchronous=NORMAL')
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA busy_timeout=5000')
    return con


def _init(con):
    con.executescript(
        'CREATE TABLE IF NOT EXISTS games ('
        ' id INTEGER PRIMARY KEY AUTOINCREMENT,'
        ' session_id TEXT, started_at INTEGER, ended_at INTEGER,'
        ' champ TEXT, mode TEXT, map INTEGER);'
        'CREATE TABLE IF NOT EXISTS events ('
        ' id INTEGER PRIMARY KEY AUTOINCREMENT, game_id INTEGER,'
        ' t_game REAL, at_ts INTEGER, kind TEXT, label TEXT, json TEXT);'
        'CREATE TABLE IF NOT EXISTS advice ('
        ' id INTEGER PRIMARY KEY AUTOINCREMENT, game_id INTEGER,'
        ' t_game REAL, at_ts INTEGER, kind TEXT, text TEXT,'
        ' session_id TEXT, meta_json TEXT);'
        'CREATE TABLE IF NOT EXISTS inference ('
        ' id INTEGER PRIMARY KEY AUTOINCREMENT, game_id INTEGER, request_id TEXT,'
        ' started_at INTEGER, finished_at INTEGER, status TEXT,'
        ' tokens_in INTEGER, tokens_out INTEGER, cost REAL, reason TEXT);'
        'CREATE TABLE IF NOT EXISTS intervals ('
        ' id INTEGER PRIMARY KEY AUTOINCREMENT, game_id INTEGER, session_id TEXT,'
        ' kind TEXT, started_at INTEGER, ended_at INTEGER, source TEXT, json TEXT);'
        'CREATE TABLE IF NOT EXISTS quarantine ('
        ' id INTEGER PRIMARY KEY AUTOINCREMENT, at_ts INTEGER, session_id TEXT,'
        ' kind TEXT, label TEXT, text TEXT, json TEXT);'
        'CREATE INDEX IF NOT EXISTS idx_games_session ON games(session_id, ended_at);'
        'CREATE INDEX IF NOT EXISTS idx_events_game ON events(game_id, at_ts);'
        'CREATE INDEX IF NOT EXISTS idx_advice_game ON advice(game_id, at_ts);'
        'CREATE INDEX IF NOT EXISTS idx_inference_game ON inference(game_id, request_id);'
        'CREATE INDEX IF NOT EXISTS idx_intervals_game ON intervals(game_id, started_at);'
        'CREATE INDEX IF NOT EXISTS idx_quarantine_session ON quarantine(session_id, at_ts);'
    )
    _migrate(con)
    con.commit()


def _migrate(con):
    columns = {row[1] for row in con.execute('PRAGMA table_info(inference)')}
    if columns and 'reason' not in columns:
        con.execute('ALTER TABLE inference ADD COLUMN reason TEXT')
    version = con.execute('PRAGMA user_version').fetchone()[0]
    if version != SCHEMA_VERSION:
        con.execute('PRAGMA user_version = %d' % SCHEMA_VERSION)


def init_db():
    with _lock:
        con = _connect()
        try:
            _init(con)
        finally:
            con.close()


def _read_connect():
    try:
        return _connect(readonly=True)
    except sqlite3.Error:
        init_db()
        return _connect(readonly=True)


@contextlib.contextmanager
def _writer():
    with _lock:
        con = _connect()
        try:
            _init(con)
            con.execute('BEGIN IMMEDIATE')
            try:
                yield con
            except BaseException:
                con.rollback()
                raise
            con.commit()
        finally:
            con.close()


def _clean(value, limit):
    if value is None:
        return None
    text = str(value)
    return text[:limit]


def _payload_json(payload):
    if payload is None:
        return None
    try:
        raw = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        raw = json.dumps({'repr': repr(payload)[:MAX_JSON]})
    if len(raw) > MAX_JSON:
        raw = raw[:MAX_JSON] + '…'
    return raw


def _now_ms():
    return int(time.time() * 1000)


def _num(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timestamp_ms(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return int(number)


def _clean_int(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return int(number)


def _clean_float(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _open_game(con, session_id=None):
    if session_id:
        cur = con.execute(
            'SELECT * FROM games WHERE session_id=? AND ended_at IS NULL LIMIT 1',
            (session_id,))
    else:
        cur = con.execute('SELECT * FROM games WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1')
    row = cur.fetchone()
    return dict(row) if row else None


def _game_for_session(con, session_id):
    if not session_id:
        return None
    row = con.execute(
        'SELECT * FROM games WHERE session_id=?'
        ' ORDER BY (ended_at IS NULL) DESC, id DESC LIMIT 1',
        (session_id,)).fetchone()
    return dict(row) if row else None


def _close_open_games(con, except_session=None):
    if except_session:
        con.execute('UPDATE games SET ended_at=? WHERE ended_at IS NULL AND session_id IS NOT ?',
                    (_now_ms(), except_session))
    else:
        con.execute('UPDATE games SET ended_at=? WHERE ended_at IS NULL', (_now_ms(),))


def _backfill_intervals(con, game_id, session_id):
    if game_id and session_id:
        con.execute('UPDATE intervals SET game_id=? WHERE game_id IS NULL AND session_id=?',
                    (game_id, session_id))


def _quarantine(con, session_id, kind, label=None, payload=None, text=None):
    con.execute(
        'INSERT INTO quarantine (at_ts, session_id, kind, label, text, json)'
        ' VALUES (?,?,?,?,?,?)',
        (_now_ms(), _clean(session_id, 128), _clean(kind, 32),
         _clean(label, 300), _clean(text, MAX_TEXT), _payload_json(payload)))


def _quarantine_count(con):
    row = con.execute('SELECT COUNT(*) AS n FROM quarantine').fetchone()
    return int(row['n'] or 0)


def ensure_game(session_id, champ=None, mode=None, map_number=None, force_new=False):
    if not session_id:
        session_id = 'unknown'
    with _writer() as con:
        game = None if force_new else _open_game(con, session_id)
        if game is None:
            _close_open_games(con)
            con.execute(
                'INSERT INTO games (session_id, started_at, champ, mode, map)'
                ' VALUES (?,?,?,?,?)',
                (_clean(session_id, 128), _now_ms(), _clean(champ, 64),
                 _clean(mode, 32), int(map_number) if map_number else None))
            game = _open_game(con, session_id)
        elif champ or mode or map_number:
            con.execute('UPDATE games SET champ=COALESCE(?,champ), mode=COALESCE(?,mode),'
                        ' map=COALESCE(?,map) WHERE id=?',
                        (_clean(champ, 64), _clean(mode, 32),
                         int(map_number) if map_number else None, game['id']))
        if game:
            _backfill_intervals(con, game['id'], session_id)
        return dict(game) if game else None


def end_game(session_id=None):
    with _writer() as con:
        if session_id:
            con.execute('UPDATE games SET ended_at=? WHERE ended_at IS NULL AND session_id=?',
                        (_now_ms(), session_id))
        else:
            _close_open_games(con)
        return True


def _game_id_for(con, session_id, champ=None, mode=None, map_number=None):
    if session_id:
        game = _game_for_session(con, session_id)
        return game['id'] if game else None
    game = _open_game(con)
    return game['id'] if game else None


def _record_coverage_row(con, kind, game_id, session_id, at_ms, source=None, payload=None):
    if kind == 'coverage_start':
        con.execute(
            'INSERT INTO intervals (game_id, session_id, kind, started_at, ended_at, source, json)'
            ' VALUES (?,?,?,?,?,?,?)',
            (game_id, _clean(session_id, 128), 'coverage', at_ms, None,
             _clean(source, 32), _payload_json(payload)))
        return
    row = None
    if game_id is not None:
        row = con.execute(
            'SELECT id FROM intervals WHERE game_id=? AND ended_at IS NULL'
            ' ORDER BY id DESC LIMIT 1', (game_id,)).fetchone()
    if row is None and session_id:
        row = con.execute(
            'SELECT id FROM intervals WHERE session_id=? AND ended_at IS NULL'
            ' ORDER BY id DESC LIMIT 1', (session_id,)).fetchone()
    if row is not None:
        con.execute('UPDATE intervals SET ended_at=? WHERE id=?', (at_ms, row['id']))
    else:
        con.execute(
            'INSERT INTO intervals (game_id, session_id, kind, started_at, ended_at, source, json)'
            ' VALUES (?,?,?,?,?,?,?)',
            (game_id, _clean(session_id, 128), 'coverage', at_ms, at_ms,
             _clean(source, 32), _payload_json(payload)))


def record_event(kind, label=None, payload=None, t_game=None, session_id=None,
                 champ=None, mode=None, map_number=None, at=None, source=None):
    kind = _clean(kind, 32) or 'status'
    if kind not in EVENT_KINDS:
        kind = 'status'
    at_ms = _timestamp_ms(at) or _now_ms()
    with _writer() as con:
        session_id = _clean(session_id, 128)
        if kind == 'game_start' and session_id:
            _close_open_games(con)
            con.execute(
                'INSERT INTO games (session_id, started_at, champ, mode, map)'
                ' VALUES (?,?,?,?,?)',
                (session_id, at_ms, _clean(champ, 64),
                 _clean(mode, 32), int(map_number) if map_number else None))
            game_id = _open_game(con, session_id)['id']
            _backfill_intervals(con, game_id, session_id)
        else:
            game_id = _game_id_for(con, session_id, champ, mode, map_number)
            if kind == 'game_end' and session_id:
                con.execute('UPDATE games SET ended_at=? WHERE ended_at IS NULL AND session_id=?',
                            (at_ms, session_id))
        if kind in COVERAGE_KINDS:
            _record_coverage_row(con, kind, game_id, session_id, at_ms,
                                 source=source, payload=payload)
        elif session_id and game_id is None:
            _quarantine(con, session_id, kind, label, payload)
            return {'ok': True, 'kind': kind, 'gameId': None,
                    'unmatched': True, 'quarantined': True, 'at': at_ms}
        con.execute(
            'INSERT INTO events (game_id, t_game, at_ts, kind, label, json)'
            ' VALUES (?,?,?,?,?,?)',
            (game_id, _num(t_game), at_ms,
             kind, _clean(label, 300), _payload_json(payload)))
        return {'ok': True, 'kind': kind, 'gameId': game_id, 'at': at_ms}


def record_coverage(action, session_id=None, at=None, source='producer', payload=None):
    kind = 'coverage_start' if str(action or '').strip().lower() in (
        'start', 'coverage_start', 'begin') else 'coverage_stop'
    return record_event(kind, label=kind, payload=payload, session_id=session_id,
                        at=at, source=source)


def record_advice(kind, text, t_game=None, session_id=None, meta=None):
    kind = _clean(kind, 32) or 'coach'
    if kind not in ADVICE_KINDS:
        kind = 'note'
    session_id = _clean(session_id, 128)
    with _writer() as con:
        game_id = _game_id_for(con, session_id)
        if session_id and game_id is None:
            _quarantine(con, session_id, 'advice:' + kind, payload=meta, text=text)
            return {'ok': True, 'kind': kind, 'gameId': None,
                    'unmatched': True, 'quarantined': True}
        con.execute(
            'INSERT INTO advice (game_id, t_game, at_ts, kind, text, session_id, meta_json)'
            ' VALUES (?,?,?,?,?,?,?)',
            (game_id, _num(t_game), _now_ms(),
             kind, _clean(text, MAX_TEXT), session_id, _payload_json(meta)))
        return {'ok': True, 'kind': kind, 'gameId': game_id}


def record_inference(request_id, status='started', t_game=None, session_id=None,
                     started_at=None, finished_at=None, tokens_in=None, tokens_out=None,
                     cost=None, reason=None):
    status = _clean(status, 32) or 'started'
    if status not in INFERENCE_STATUS:
        status = 'started'
    session_id = _clean(session_id, 128)
    tokens_in = _clean_int(tokens_in)
    tokens_out = _clean_int(tokens_out)
    cost = _clean_float(cost)
    reason = _clean(reason, 500)
    with _writer() as con:
        game_id = _game_id_for(con, session_id)
        if session_id and game_id is None:
            _quarantine(con, session_id, 'inference',
                        payload={'status': status, 'reason': reason})
            return {'ok': True, 'requestId': _clean(request_id, 128),
                    'status': status, 'gameId': None,
                    'unmatched': True, 'quarantined': True}
        request_id = _clean(request_id, 128) or ('req-%d' % _now_ms())
        existing = con.execute(
            'SELECT id FROM inference WHERE request_id=? ORDER BY id DESC LIMIT 1',
            (request_id,)).fetchone()
        if existing and status != 'started':
            con.execute(
                'UPDATE inference SET finished_at=?, status=?,'
                ' tokens_in=COALESCE(?,tokens_in), tokens_out=COALESCE(?,tokens_out),'
                ' cost=COALESCE(?,cost), reason=COALESCE(?,reason) WHERE id=?',
                (finished_at or _now_ms(), status, tokens_in, tokens_out, cost,
                 reason, existing['id']))
        else:
            con.execute(
                'INSERT INTO inference (game_id, request_id, started_at, finished_at, status,'
                ' tokens_in, tokens_out, cost, reason) VALUES (?,?,?,?,?,?,?,?,?)',
                (game_id, request_id, started_at or _now_ms(),
                 finished_at if status != 'started' else None, status,
                 tokens_in, tokens_out, cost, reason))
        return {'ok': True, 'requestId': request_id, 'status': status, 'gameId': game_id}


def _budget(con, game_id):
    limit = DEFAULT_BUDGET
    used = 0
    if game_id:
        placeholders = ','.join('?' * len(BUDGET_EXCLUDED))
        row = con.execute(
            'SELECT COUNT(*) AS n FROM inference WHERE game_id=?'
            ' AND status NOT IN (%s)' % placeholders,
            (game_id,) + BUDGET_EXCLUDED).fetchone()
        used = int(row['n'] or 0)
    return {'used': used, 'limit': limit, 'remaining': max(0, limit - used)}


def _empty_coverage(con, session_id=None):
    return {
        'ok': True,
        'gameId': None,
        'sessionId': session_id,
        'startedAt': None,
        'endedAt': None,
        'totalSeconds': 0.0,
        'observedSeconds': 0.0,
        'unobservedSeconds': 0.0,
        'observedRatio': None,
        'gaps': [],
        'intervals': [],
        'partial': False,
        'quarantineCount': _quarantine_count(con),
    }


def coverage(session_id=None, game_id=None, now=None):
    now_ms = _timestamp_ms(now) or _now_ms()
    con = _read_connect()
    try:
        game = None
        if game_id is not None:
            row = con.execute('SELECT * FROM games WHERE id=?', (game_id,)).fetchone()
            game = dict(row) if row else None
        elif session_id:
            game = _game_for_session(con, session_id)
        else:
            game = _open_game(con)
        if not game:
            return _empty_coverage(con, session_id=session_id)
        start = _timestamp_ms(game.get('started_at')) or now_ms
        end = _timestamp_ms(game.get('ended_at')) or now_ms
        if end < start:
            end = start
        rows = con.execute(
            'SELECT started_at, ended_at, source FROM intervals'
            ' WHERE kind=? AND (game_id=? OR (game_id IS NULL AND session_id=?))'
            ' ORDER BY started_at',
            ('coverage', game['id'], game.get('session_id'))).fetchall()
        merged = []
        for row in rows:
            raw_start = _timestamp_ms(row['started_at'])
            if raw_start is None:
                continue
            raw_end = _timestamp_ms(row['ended_at'])
            interval_start = max(start, min(raw_start, end))
            interval_end = min(end, raw_end if raw_end is not None else end)
            if interval_end < interval_start:
                continue
            if merged and interval_start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], interval_end)
            else:
                merged.append([interval_start, interval_end])
        observed_ms = sum(item[1] - item[0] for item in merged)
        gaps = []
        cursor = start
        for item in merged:
            if item[0] > cursor:
                gaps.append({'start': cursor, 'end': item[0],
                             'seconds': round((item[0] - cursor) / 1000.0, 3)})
            cursor = max(cursor, item[1])
        if cursor < end:
            gaps.append({'start': cursor, 'end': end,
                         'seconds': round((end - cursor) / 1000.0, 3)})
        total_ms = max(0, end - start)
        return {
            'ok': True,
            'gameId': game['id'],
            'sessionId': game.get('session_id'),
            'startedAt': start,
            'endedAt': end,
            'totalSeconds': round(total_ms / 1000.0, 3),
            'observedSeconds': round(observed_ms / 1000.0, 3),
            'unobservedSeconds': round((total_ms - observed_ms) / 1000.0, 3),
            'observedRatio': round(observed_ms / total_ms, 4) if total_ms else None,
            'gaps': gaps,
            'intervals': [{'start': item[0], 'end': item[1],
                           'seconds': round((item[1] - item[0]) / 1000.0, 3)}
                          for item in merged],
            'partial': bool(gaps),
            'quarantineCount': _quarantine_count(con),
        }
    finally:
        con.close()


def latest_events(kinds, game_id=None, limit=5):
    kinds = [kind for kind in kinds if kind in EVENT_KINDS]
    if not kinds:
        return []
    con = _read_connect()
    try:
        if game_id is None:
            game = _open_game(con)
            game_id = game['id'] if game else None
        if game_id is None:
            return []
        placeholders = ','.join('?' * len(kinds))
        rows = con.execute(
            'SELECT id, game_id, t_game, at_ts, kind, label, json FROM events'
            ' WHERE game_id=? AND kind IN (%s) ORDER BY id DESC LIMIT ?' % placeholders,
            [game_id] + kinds + [max(1, min(int(limit or 5), 50))]).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


def recent(limit=50):
    limit = max(1, min(int(limit or 50), 200))
    con = _read_connect()
    try:
        game = _open_game(con)
        game_id = game['id'] if game else None
        events = []
        advice = []
        if game_id:
            events = [dict(r) for r in con.execute(
                'SELECT id, t_game, at_ts, kind, label, json FROM events'
                ' WHERE game_id=? ORDER BY id DESC LIMIT ?', (game_id, limit)).fetchall()]
            advice = [dict(r) for r in con.execute(
                'SELECT id, t_game, at_ts, kind, text, session_id FROM advice'
                ' WHERE game_id=? ORDER BY id DESC LIMIT ?', (game_id, limit)).fetchall()]
        events.reverse()
        advice.reverse()
        return {
            'ok': True,
            'game': game,
            'events': events,
            'advice': advice,
            'inference': _budget(con, game_id),
            'coverage': coverage(game_id=game_id) if game_id else _empty_coverage(con),
            'quarantine': {'count': _quarantine_count(con)},
        }
    finally:
        con.close()


def current():
    con = _read_connect()
    try:
        game = _open_game(con)
        if not game:
            return {'ok': True, 'game': None,
                    'inference': {'used': 0, 'limit': DEFAULT_BUDGET,
                                  'remaining': DEFAULT_BUDGET},
                    'quarantine': {'count': _quarantine_count(con)},
                    'coverage': _empty_coverage(con)}
        counts = {}
        for table in ('events', 'advice', 'inference'):
            row = con.execute('SELECT COUNT(*) AS n FROM %s WHERE game_id=?' % table,
                              (game['id'],)).fetchone()
            counts[table] = int(row['n'] or 0)
        return {'ok': True, 'game': game, 'counts': counts,
                'inference': _budget(con, game['id']),
                'quarantine': {'count': _quarantine_count(con)},
                'coverage': coverage(game_id=game['id'])}
    finally:
        con.close()


def game_summary(game_id):
    con = _read_connect()
    try:
        game = con.execute('SELECT * FROM games WHERE id=?', (game_id,)).fetchone()
        if not game:
            return None
        summary = dict(game)
        summary['events'] = [dict(r) for r in con.execute(
            'SELECT t_game, at_ts, kind, label FROM events WHERE game_id=? ORDER BY id', (game_id,))]
        summary['advice'] = [dict(r) for r in con.execute(
            'SELECT t_game, at_ts, kind, text FROM advice WHERE game_id=? ORDER BY id', (game_id,))]
        summary['inference'] = _budget(con, game_id)
        return summary
    finally:
        con.close()


try:
    init_db()
except (sqlite3.Error, OSError):
    pass
