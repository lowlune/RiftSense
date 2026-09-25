import json
import os
import sqlite3
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, 'data')
DB_PATH = os.environ.get('RIFTSENSE_TIMELINE_DB') or os.path.join(DATA_DIR, 'timeline.db')
DEFAULT_BUDGET = int(os.environ.get('RIFTSENSE_INFERENCE_BUDGET', '40'))
MAX_TEXT = 4000
MAX_JSON = 8000

_lock = threading.Lock()

EVENT_KINDS = (
    'game_start', 'game_end', 'death', 'item', 'objective',
    'heartbeat', 'error', 'status',
)
ADVICE_KINDS = ('coach', 'death', 'note')
INFERENCE_STATUS = ('started', 'ok', 'failed', 'timeout', 'skipped')


def configure(path):
    global DB_PATH
    DB_PATH = path


def _connect():
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA synchronous=NORMAL')
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
        ' tokens_in INTEGER, tokens_out INTEGER, cost REAL);'
        'CREATE INDEX IF NOT EXISTS idx_games_session ON games(session_id, ended_at);'
        'CREATE INDEX IF NOT EXISTS idx_events_game ON events(game_id, at_ts);'
        'CREATE INDEX IF NOT EXISTS idx_advice_game ON advice(game_id, at_ts);'
        'CREATE INDEX IF NOT EXISTS idx_inference_game ON inference(game_id, request_id);'
    )
    con.commit()


def init_db():
    with _lock:
        con = _connect()
        try:
            _init(con)
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
        return float(value)
    except (TypeError, ValueError):
        return None


def _open_game(con, session_id=None):
    if session_id:
        cur = con.execute(
            'SELECT * FROM games WHERE session_id=? AND ended_at IS NULL LIMIT 1',
            (session_id,))
    else:
        cur = con.execute('SELECT * FROM games WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1')
    row = cur.fetchone()
    return dict(row) if row else None


def _close_open_games(con, except_session=None):
    if except_session:
        con.execute('UPDATE games SET ended_at=? WHERE ended_at IS NULL AND session_id IS NOT ?',
                    (_now_ms(), except_session))
    else:
        con.execute('UPDATE games SET ended_at=? WHERE ended_at IS NULL', (_now_ms(),))


def ensure_game(session_id, champ=None, mode=None, map_number=None, force_new=False):
    if not session_id:
        session_id = 'unknown'
    with _lock:
        con = _connect()
        try:
            _init(con)
            game = None if force_new else _open_game(con, session_id)
            if game is None:
                _close_open_games(con)
                con.execute(
                    'INSERT INTO games (session_id, started_at, champ, mode, map)'
                    ' VALUES (?,?,?,?,?)',
                    (_clean(session_id, 128), _now_ms(), _clean(champ, 64),
                     _clean(mode, 32), int(map_number) if map_number else None))
                game = _open_game(con, session_id)
            else:
                if champ or mode or map_number:
                    con.execute('UPDATE games SET champ=COALESCE(?,champ), mode=COALESCE(?,mode),'
                                ' map=COALESCE(?,map) WHERE id=?',
                                (_clean(champ, 64), _clean(mode, 32),
                                 int(map_number) if map_number else None, game['id']))
            con.commit()
            return dict(game) if game else None
        finally:
            con.close()


def end_game(session_id=None):
    with _lock:
        con = _connect()
        try:
            _init(con)
            if session_id:
                con.execute('UPDATE games SET ended_at=? WHERE ended_at IS NULL AND session_id=?',
                            (_now_ms(), session_id))
            else:
                _close_open_games(con)
            con.commit()
            return True
        finally:
            con.close()


def _game_id_for(con, session_id, champ, mode, map_number):
    if session_id:
        game = _open_game(con, session_id)
        if game:
            return game['id']
    game = _open_game(con)
    return game['id'] if game else None


def record_event(kind, label=None, payload=None, t_game=None, session_id=None,
                 champ=None, mode=None, map_number=None):
    kind = _clean(kind, 32) or 'status'
    if kind not in EVENT_KINDS:
        kind = 'status'
    with _lock:
        con = _connect()
        try:
            _init(con)
            game_id = _game_id_for(con, session_id, champ, mode, map_number)
            if kind == 'game_start' and session_id:
                _close_open_games(con)
                con.execute('INSERT INTO games (session_id, started_at, champ, mode, map)'
                            ' VALUES (?,?,?,?,?)',
                            (_clean(session_id, 128), _now_ms(), _clean(champ, 64),
                             _clean(mode, 32), int(map_number) if map_number else None))
                game_id = _open_game(con, session_id)['id']
            if kind == 'game_end' and session_id:
                con.execute('UPDATE games SET ended_at=? WHERE ended_at IS NULL AND session_id=?',
                            (_now_ms(), session_id))
            con.execute(
                'INSERT INTO events (game_id, t_game, at_ts, kind, label, json)'
                ' VALUES (?,?,?,?,?,?)',
                (game_id, _num(t_game), _now_ms(),
                 kind, _clean(label, 300), _payload_json(payload)))
            con.commit()
            return {'ok': True, 'kind': kind, 'gameId': game_id}
        finally:
            con.close()


def record_advice(kind, text, t_game=None, session_id=None, meta=None):
    kind = _clean(kind, 32) or 'coach'
    if kind not in ADVICE_KINDS:
        kind = 'note'
    with _lock:
        con = _connect()
        try:
            _init(con)
            game_id = _game_id_for(con, session_id, None, None, None)
            con.execute(
                'INSERT INTO advice (game_id, t_game, at_ts, kind, text, session_id, meta_json)'
                ' VALUES (?,?,?,?,?,?,?)',
                (game_id, _num(t_game), _now_ms(),
                 kind, _clean(text, MAX_TEXT), _clean(session_id, 128), _payload_json(meta)))
            con.commit()
            return {'ok': True, 'kind': kind, 'gameId': game_id}
        finally:
            con.close()


def record_inference(request_id, status='started', t_game=None, session_id=None,
                     started_at=None, finished_at=None, tokens_in=0, tokens_out=0, cost=0.0):
    status = _clean(status, 32) or 'started'
    if status not in INFERENCE_STATUS:
        status = 'started'
    with _lock:
        con = _connect()
        try:
            _init(con)
            game_id = _game_id_for(con, session_id, None, None, None)
            request_id = _clean(request_id, 128) or ('req-%d' % _now_ms())
            existing = con.execute(
                'SELECT id FROM inference WHERE request_id=? ORDER BY id DESC LIMIT 1',
                (request_id,)).fetchone()
            if existing and status != 'started':
                con.execute(
                    'UPDATE inference SET finished_at=?, status=?, tokens_in=?, tokens_out=?, cost=?'
                    ' WHERE id=?',
                    (finished_at or _now_ms(), status, int(tokens_in or 0),
                     int(tokens_out or 0), float(cost or 0.0), existing['id']))
            else:
                con.execute(
                    'INSERT INTO inference (game_id, request_id, started_at, finished_at, status,'
                    ' tokens_in, tokens_out, cost) VALUES (?,?,?,?,?,?,?,?)',
                    (game_id, request_id, started_at or _now_ms(),
                     finished_at if status != 'started' else None, status,
                     int(tokens_in or 0), int(tokens_out or 0), float(cost or 0.0)))
            con.commit()
            return {'ok': True, 'requestId': request_id, 'status': status}
        finally:
            con.close()


def _budget(con, game_id):
    limit = DEFAULT_BUDGET
    used = 0
    if game_id:
        row = con.execute(
            "SELECT COUNT(*) AS n FROM inference WHERE game_id=? AND status != 'skipped'",
            (game_id,)).fetchone()
        used = int(row['n'] or 0)
    return {'used': used, 'limit': limit, 'remaining': max(0, limit - used)}


def recent(limit=50):
    limit = max(1, min(int(limit or 50), 200))
    con = _connect()
    try:
        _init(con)
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
        }
    finally:
        con.close()


def current():
    con = _connect()
    try:
        _init(con)
        game = _open_game(con)
        if not game:
            return {'ok': True, 'game': None, 'inference': {'used': 0, 'limit': DEFAULT_BUDGET,
                                                            'remaining': DEFAULT_BUDGET}}
        counts = {}
        for table in ('events', 'advice', 'inference'):
            row = con.execute('SELECT COUNT(*) AS n FROM %s WHERE game_id=?' % table,
                              (game['id'],)).fetchone()
            counts[table] = int(row['n'] or 0)
        return {'ok': True, 'game': game, 'counts': counts, 'inference': _budget(con, game['id'])}
    finally:
        con.close()


def game_summary(game_id):
    con = _connect()
    try:
        _init(con)
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


init_db()
