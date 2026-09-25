"""Local trend aggregation over the observed timeline (stdlib only).

Reads the SQLite database written by ``ui/timeline.py`` and aggregates only
*observed* records from games that have ended. Every aggregate exposes its
sample size. The module never infers causes, never fills in missing data and
never fabricates metrics that were not recorded.
"""

import sqlite3
import time

try:
    import timeline
except ImportError:  # package-style import (python3 -m ui.trends)
    from . import timeline

DEATH_BUCKETS = ((0.0, 5.0, '0-5'), (5.0, 10.0, '5-10'), (10.0, 15.0, '10-15'),
                 (15.0, 20.0, '15-20'), (20.0, None, '20+'))
OBJECTIVE_LABELS = ('dragon', 'baron', 'herald', 'voidgrubs', 'turret', 'inhibitor')
MAX_CHAMPS = 200
NOTE = ('Observed counts from ended games only. Sample sizes are shown; '
        'these are descriptions, not causal claims.')


def _empty_report(now=None, status='ok', unavailable=None, ok=True):
    return {
        'ok': ok,
        'status': status,
        'generatedAt': time.time() if now is None else now,
        'games': {'played': 0, 'open': 0, 'withChamp': 0},
        'champs': [],
        'deathBuckets': {
            'total': 0,
            'unknown': 0,
            'buckets': [{'label': label, 'count': 0} for _lo, _hi, label in DEATH_BUCKETS],
        },
        'objectives': {'total': 0, 'counts': []},
        'inference': {'requests': 0, 'ok': 0, 'failed': 0, 'started': 0,
                      'tokensIn': 0, 'tokensOut': 0, 'cost': 0.0},
        'unavailable': list(unavailable or []),
        'note': NOTE,
    }


def _clean_champ(value):
    if not isinstance(value, str):
        return '(unknown)'
    text = value.strip()[:64]
    return text or '(unknown)'


def _int(value):
    if isinstance(value, bool) or not isinstance(value, int):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return value


def _open():
    con = timeline._connect()
    timeline._init(con)
    return con


def _game_counts(con):
    played = _int(con.execute(
        'SELECT COUNT(*) FROM games WHERE ended_at IS NOT NULL').fetchone()[0])
    open_games = _int(con.execute(
        'SELECT COUNT(*) FROM games WHERE ended_at IS NULL').fetchone()[0])
    return played, open_games


def _champ_counts(con):
    rows = con.execute(
        'SELECT g.champ AS champ,'
        ' SUM(CASE WHEN e.kind = \'death\' THEN 1 ELSE 0 END) AS deaths'
        ' FROM games g LEFT JOIN events e ON e.game_id = g.id'
        ' WHERE g.ended_at IS NOT NULL GROUP BY g.id').fetchall()
    counts = {}
    with_champ = 0
    for row in rows:
        champ = _clean_champ(row['champ'])
        if champ != '(unknown)':
            with_champ += 1
        entry = counts.setdefault(champ, {'champ': champ, 'games': 0, 'deaths': 0})
        entry['games'] += 1
        entry['deaths'] += _int(row['deaths'])
    champs = []
    for entry in counts.values():
        games = entry['games']
        champs.append({
            'champ': entry['champ'],
            'games': games,
            'deaths': entry['deaths'],
            'avgDeaths': round(entry['deaths'] / games, 2) if games else 0.0,
        })
    champs.sort(key=lambda item: (-item['games'], item['champ'].lower()))
    return champs[:MAX_CHAMPS], with_champ


def _death_buckets(con):
    rows = con.execute(
        'SELECT e.t_game AS t_game FROM events e'
        ' JOIN games g ON g.id = e.game_id'
        " WHERE g.ended_at IS NOT NULL AND e.kind = 'death'").fetchall()
    counts = [0] * len(DEATH_BUCKETS)
    unknown = 0
    for row in rows:
        try:
            t_game = float(row['t_game'])
        except (TypeError, ValueError):
            unknown += 1
            continue
        if t_game < 0:
            unknown += 1
            continue
        minutes = t_game / 60.0
        for idx, (low, high, _label) in enumerate(DEATH_BUCKETS):
            if minutes >= low and (high is None or minutes < high):
                counts[idx] += 1
                break
        else:  # pragma: no cover - every non-negative value matches a bucket
            unknown += 1
    return {
        'total': sum(counts) + unknown,
        'unknown': unknown,
        'buckets': [{'label': label, 'count': counts[idx]}
                    for idx, (_lo, _hi, label) in enumerate(DEATH_BUCKETS)],
    }


def _objectives(con):
    rows = con.execute(
        'SELECT e.label AS label, COUNT(*) AS n FROM events e'
        ' JOIN games g ON g.id = e.game_id'
        " WHERE g.ended_at IS NOT NULL AND e.kind = 'objective'"
        ' GROUP BY e.label').fetchall()
    counts = {}
    other = 0
    total = 0
    for row in rows:
        n = _int(row['n'])
        total += n
        label = str(row['label'] or '').strip().lower()[:120]
        matched = None
        for canonical in OBJECTIVE_LABELS:
            if canonical in label:
                matched = canonical
                break
        if matched:
            counts[matched] = counts.get(matched, 0) + n
        else:
            other += n
    ordered = [{'label': label, 'count': counts.get(label, 0)} for label in OBJECTIVE_LABELS]
    if other:
        ordered.append({'label': 'other', 'count': other})
    return {'total': total, 'counts': ordered}


def _inference(con):
    rows = con.execute(
        'SELECT i.status AS status, COUNT(*) AS n,'
        ' COALESCE(SUM(i.tokens_in), 0) AS tokens_in,'
        ' COALESCE(SUM(i.tokens_out), 0) AS tokens_out,'
        ' COALESCE(SUM(i.cost), 0) AS cost FROM inference i'
        ' JOIN games g ON g.id = i.game_id'
        ' WHERE g.ended_at IS NOT NULL GROUP BY i.status').fetchall()
    result = {'requests': 0, 'ok': 0, 'failed': 0, 'started': 0,
              'tokensIn': 0, 'tokensOut': 0, 'cost': 0.0}
    for row in rows:
        n = _int(row['n'])
        status = str(row['status'] or '')
        result['requests'] += n
        result['tokensIn'] += _int(row['tokens_in'])
        result['tokensOut'] += _int(row['tokens_out'])
        try:
            result['cost'] += float(row['cost'] or 0.0)
        except (TypeError, ValueError):
            pass
        if status == 'ok':
            result['ok'] += n
        elif status in ('failed', 'timeout'):
            result['failed'] += n
        else:
            result['started'] += n
    result['cost'] = round(result['cost'], 6)
    return result


def build_trends(now=None):
    """Aggregate observed timeline data for games that have ended."""
    report = _empty_report(now)
    try:
        con = _open()
    except (sqlite3.Error, OSError) as ex:
        report['ok'] = False
        report['status'] = 'db_error'
        report['error'] = str(ex)
        report['unavailable'] = ['timeline_db']
        return report
    try:
        played, open_games = _game_counts(con)
        report['games'] = {'played': played, 'open': open_games, 'withChamp': 0}
        sections = (
            ('champs', _champ_counts),
            ('deathBuckets', _death_buckets),
            ('objectives', _objectives),
            ('inference', _inference),
        )
        for name, fn in sections:
            try:
                value = fn(con)
            except sqlite3.Error:
                report['unavailable'].append(name)
                continue
            if name == 'champs':
                report['champs'], report['games']['withChamp'] = value
            else:
                report[name] = value
        if report['unavailable']:
            report['status'] = 'partial'
        return report
    finally:
        con.close()
