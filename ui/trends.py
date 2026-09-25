"""Local trend aggregation over the observed timeline (stdlib only).

Reads the SQLite database written by ``ui/timeline.py`` and aggregates only
*observed* records from games that have ended. Every aggregate exposes its
sample size. The module never infers causes, never fills in missing data and
never fabricates metrics that were not recorded.

Objective counts are *objective-window reminders* produced by the collector,
not objective outcomes; actual outcomes are aggregated separately when the
producer records them. Unknown token/cost totals render as ``null`` and are
listed in ``unavailable`` instead of pretending to be zero.
"""

import json
import re
import sqlite3
import time

try:
    import timeline
except ImportError:  # package-style import (python3 -m ui.trends)
    from . import timeline

DEATH_BUCKETS = ((0.0, 5.0, '0-5'), (5.0, 10.0, '5-10'), (10.0, 15.0, '10-15'),
                 (15.0, 20.0, '15-20'), (20.0, None, '20+'))
OBJECTIVE_LABELS = ('dragon', 'baron', 'herald', 'voidgrubs', 'turret', 'inhibitor')
OBJECTIVE_KINDS = ('objective', 'objective_kill', 'objective_outcome')
MAX_CHAMPS = 200
MAX_COVERAGE_GAMES = 50
OUTCOME_RE = re.compile(r'killed|slain|secured|taken|destroyed|claimed|outcome',
                        re.IGNORECASE)
NOTE = ('Observed counts from ended games only. Sample sizes and coverage are shown; '
        'objective figures are collector window reminders, not objective outcomes. '
        'These are descriptions, not causal claims.')


def _empty_report(now=None, status='ok', unavailable=None, ok=True):
    empty_objectives = {
        'total': 0,
        'counts': [{'label': label, 'count': 0} for label in OBJECTIVE_LABELS],
    }
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
        'objectives': empty_objectives,
        'objectiveReminders': dict(empty_objectives, kind='reminder'),
        'objectiveOutcomes': {
            'total': 0,
            'counts': [{'label': label, 'count': 0} for label in OBJECTIVE_LABELS],
            'kind': 'outcome',
        },
        'inference': {'requests': 0, 'ok': 0, 'failed': 0, 'started': 0,
                      'tokensIn': None, 'tokensOut': None, 'cost': None,
                      'unavailable': []},
        'coverage': {'status': 'no_games', 'gamesWithCoverage': 0, 'adequateGames': 0,
                     'endedGames': 0, 'sampleSize': 0,
                     'note': 'No ended games to cover.'},
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


def _load_json(raw):
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


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


def _objective_is_outcome(kind, label, payload):
    if kind in ('objective_kill', 'objective_outcome'):
        return True
    if isinstance(payload, dict):
        for key in ('killed', 'slain', 'secured', 'taken', 'outcome', 'objectiveKill'):
            if payload.get(key) is True:
                return True
        if payload.get('secondsLeft') is not None:
            return False
    if isinstance(label, str) and OUTCOME_RE.search(label):
        return True
    return False


def _objective_totals(con):
    """Split objective rows into window reminders and observed outcomes."""
    rows = con.execute(
        'SELECT e.label AS label, e.kind AS kind, e.json AS json FROM events e'
        ' JOIN games g ON g.id = e.game_id'
        ' WHERE g.ended_at IS NOT NULL AND e.kind IN (?,?,?)',
        OBJECTIVE_KINDS).fetchall()
    reminders = {}
    outcomes = {}
    reminder_total = 0
    outcome_total = 0
    for row in rows:
        label = str(row['label'] or '').strip().lower()[:120]
        target = outcomes if _objective_is_outcome(
            str(row['kind'] or ''), label, _load_json(row['json'])) else reminders
        if target is outcomes:
            outcome_total += 1
        else:
            reminder_total += 1
        matched = None
        for canonical in OBJECTIVE_LABELS:
            if canonical in label:
                matched = canonical
                break
        key = matched or 'other'
        target[key] = target.get(key, 0) + 1
    return {
        'reminders': _objective_payload(reminders, reminder_total, 'reminder'),
        'outcomes': _objective_payload(outcomes, outcome_total, 'outcome'),
    }


def _objective_payload(counts, total, kind):
    ordered = [{'label': label, 'count': counts.get(label, 0)} for label in OBJECTIVE_LABELS]
    if counts.get('other'):
        ordered.append({'label': 'other', 'count': counts['other']})
    return {'total': total, 'counts': ordered, 'kind': kind}


def _inference(con):
    rows = con.execute(
        'SELECT i.status AS status, COUNT(*) AS n,'
        ' SUM(i.tokens_in) AS tokens_in, SUM(i.tokens_out) AS tokens_out,'
        ' SUM(i.cost) AS cost,'
        ' SUM(CASE WHEN i.tokens_in IS NULL THEN 1 ELSE 0 END) AS tokens_in_unknown,'
        ' SUM(CASE WHEN i.tokens_out IS NULL THEN 1 ELSE 0 END) AS tokens_out_unknown,'
        ' SUM(CASE WHEN i.cost IS NULL THEN 1 ELSE 0 END) AS cost_unknown'
        ' FROM inference i'
        ' JOIN games g ON g.id = i.game_id'
        ' WHERE g.ended_at IS NOT NULL GROUP BY i.status').fetchall()
    result = {'requests': 0, 'ok': 0, 'failed': 0, 'started': 0,
              'tokensIn': None, 'tokensOut': None, 'cost': None,
              'unavailable': []}
    tokens_in_total = 0
    tokens_out_total = 0
    cost_total = 0.0
    tokens_in_known = True
    tokens_out_known = True
    cost_known = True
    for row in rows:
        n = _int(row['n'])
        status = str(row['status'] or '')
        result['requests'] += n
        if _int(row['tokens_in_unknown']):
            tokens_in_known = False
        else:
            tokens_in_total += _int(row['tokens_in'])
        if _int(row['tokens_out_unknown']):
            tokens_out_known = False
        else:
            tokens_out_total += _int(row['tokens_out'])
        if _int(row['cost_unknown']):
            cost_known = False
        else:
            try:
                cost_total += float(row['cost'] or 0.0)
            except (TypeError, ValueError):
                cost_known = False
        if status == 'ok':
            result['ok'] += n
        elif status in ('failed', 'timeout'):
            result['failed'] += n
        else:
            result['started'] += n
    if result['requests']:
        if tokens_in_known:
            result['tokensIn'] = tokens_in_total
        else:
            result['unavailable'].append('tokensIn')
        if tokens_out_known:
            result['tokensOut'] = tokens_out_total
        else:
            result['unavailable'].append('tokensOut')
        if cost_known:
            result['cost'] = round(cost_total, 6)
        else:
            result['unavailable'].append('cost')
    return result


def _coverage(con):
    """Aggregate per-game collection coverage for ended games.

    Uses ``review.game_coverage`` (which prefers an optional
    ``timeline.coverage`` API and falls back to recorded intervals). Unknown
    coverage is reported as ``status='unavailable'`` with ``None`` adequacy,
    never as full coverage.
    """
    try:
        from . import review as review_mod
    except ImportError:
        import review as review_mod
    rows = con.execute(
        'SELECT id, started_at, ended_at FROM games WHERE ended_at IS NOT NULL'
        ' ORDER BY ended_at DESC, id DESC LIMIT ?', (MAX_COVERAGE_GAMES,)).fetchall()
    games = []
    for row in rows:
        game = dict(row)
        duration = None
        try:
            started = float(game.get('started_at'))
            ended = float(game.get('ended_at'))
            if ended > started:
                duration = (ended - started) / 1000.0
        except (TypeError, ValueError):
            duration = None
        try:
            coverage = review_mod.game_coverage(con, game['id'], duration, timeline)
        except Exception:
            coverage = None
        games.append(coverage)
    if not games:
        return {'status': 'no_games', 'gamesWithCoverage': 0, 'adequateGames': 0,
                'endedGames': 0, 'sampleSize': 0,
                'note': 'No ended games to cover.'}
    with_coverage = sum(1 for item in games if item)
    adequate = sum(1 for item in games if item and item.get('adequate'))
    if not with_coverage:
        status = 'unavailable'
    elif with_coverage < len(games):
        status = 'partial'
    else:
        status = 'ok'
    return {
        'status': status,
        'gamesWithCoverage': with_coverage,
        'adequateGames': adequate,
        'endedGames': len(games),
        'sampleSize': len(games),
        'note': 'Coverage is per ended game; zero-death and objective conclusions require'
                ' adequate coverage in the observed sample.',
    }


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
            ('objectives', _objective_totals),
            ('inference', _inference),
            ('coverage', _coverage),
        )
        for name, fn in sections:
            try:
                value = fn(con)
            except sqlite3.Error:
                report['unavailable'].append(name)
                continue
            if name == 'champs':
                report['champs'], report['games']['withChamp'] = value
            elif name == 'objectives':
                report['objectives'] = value['reminders']
                report['objectiveReminders'] = value['reminders']
                report['objectiveOutcomes'] = value['outcomes']
            elif name == 'inference':
                report['inference'] = value
                for field in value.get('unavailable') or []:
                    marker = 'inference:%s' % field
                    if marker not in report['unavailable']:
                        report['unavailable'].append(marker)
            else:
                report[name] = value
        if report['unavailable']:
            report['status'] = 'partial'
        return report
    finally:
        con.close()
