"""Deterministic post-game review built from the RiftSense timeline database.

Every statement in the review is derived from recorded rows (games, events,
advice, inference). Fields that were never observed are reported in
``unavailable`` instead of being guessed. No model/LLM call is made.

Performance conclusions are gated on collection coverage. When coverage was
never recorded (or was incomplete), zero recorded deaths are reported as
"none recorded" plus a data-quality note, never as proof that the player
"played safely". Objective-window reminders and actual objective outcomes are
kept separate so a reminder can never read as an achievement.
"""

import json
import math
import os
import re
import sqlite3

try:
    from . import timeline
except ImportError:  # direct-script import (python3 ui/review.py)
    import timeline  # noqa: F401

EARLY_DEATH_SECONDS = 600
VERY_EARLY_DEATH_SECONDS = 480
DEATH_CLUSTER_SECONDS = 180
LATE_GAME_SECONDS = 1200
HISTORY_GAMES = 5
MAX_LISTED = 8
COMPLETED_ITEM_MIN_COST = 2000
MAX_TEXT = 400
COVERAGE_ADEQUATE_RATIO = 0.7
COVERAGE_MIN_SECONDS = 300

DEATH_LABEL_RE = re.compile(r'^died\s+(\d+:\d+)\s+to\s+(.+)$', re.IGNORECASE)
SIGNATURE_RE = re.compile(r'inventory changed:\s*(.*)$', re.IGNORECASE)
SIGNATURE_ITEM_RE = re.compile(r'^(\d+)x(\d+)$')
OUTCOME_RE = re.compile(
    r'killed|slain|secured|taken|destroyed|claimed|outcome', re.IGNORECASE)

UNAVAILABLE_BASE = (
    'Damage dealt and taken was not recorded.',
    'Vision score and ward coverage were not recorded.',
    'Earned gold and CS progression were not recorded.',
    'Positioning, wave state, and ability cooldowns were not recorded.',
    'Actual dragon/Baron kills and participation were not recorded;'
    ' only objective-window reminders may appear.',
)

_COVERAGE_TABLES = ('intervals', 'coverage', 'collection_intervals', 'coverage_intervals')
_START_COLUMNS = ('start_ms', 'started_at', 'start_ts', 'start', 'from_ms', 'from')
_END_COLUMNS = ('end_ms', 'ended_at', 'end_ts', 'end', 'to_ms', 'to')
_OBJECTIVE_KINDS = ('objective', 'objective_kill', 'objective_outcome')


def _no_review(reason):
    return {
        'ok': False,
        'status': 'no_review',
        'game': None,
        'observations': [],
        'priorities': [],
        'unavailable': [reason] if reason else [],
        'coverage': None,
        'versions': {'catalog': None, 'rules': None, 'patch': None, 'available': False},
        'dataQuality': {'level': 'unavailable', 'coverage': None, 'notes': []},
    }


def _num(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _fmt(seconds):
    if seconds is None:
        return None
    total = int(seconds)
    if total < 0:
        total = 0
    return '%d:%02d' % (total // 60, total % 60)


def _text(value, limit=MAX_TEXT):
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _load_json(raw):
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _connect():
    path = timeline.DB_PATH
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    con = sqlite3.connect(path, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _obs(kind, text):
    return {'kind': kind, 'text': text}


def _prio(pid, title, focus, evidence, scope):
    return {'id': pid, 'title': title, 'focus': focus, 'evidence': list(evidence), 'scope': scope}


def _table_columns(con, table):
    try:
        rows = con.execute('PRAGMA table_info(%s)' % table).fetchall()
    except sqlite3.Error:
        return []
    columns = []
    for row in rows:
        try:
            columns.append(row['name'])
        except (IndexError, KeyError, TypeError):
            continue
    return columns


def _interval_pair(item):
    if isinstance(item, (list, tuple)) and len(item) >= 2:
        return item[0], item[1]
    if not isinstance(item, dict):
        return None
    start = None
    for key in ('start', 'startMs', 'start_ms', 'started', 'startedAt', 'started_at',
                'from', 'fromMs'):
        if item.get(key) is not None:
            start = item.get(key)
            break
    end = None
    for key in ('end', 'endMs', 'end_ms', 'ended', 'endedAt', 'ended_at', 'to', 'toMs'):
        if item.get(key) is not None:
            end = item.get(key)
            break
    if start is None or end is None:
        return None
    return start, end


def _intervals(raw):
    """Normalize any supported coverage payload into ``[(start, end)]``."""
    if isinstance(raw, dict):
        found = None
        for key in ('intervals', 'coverage', 'periods', 'windows'):
            value = raw.get(key)
            if isinstance(value, (list, tuple)):
                found = value
                break
        if found is None:
            pair = _interval_pair(raw)
            raw = [pair] if pair is not None else None
        else:
            raw = found
    if not isinstance(raw, (list, tuple)):
        return None
    pairs = []
    for item in raw:
        pair = _interval_pair(item)
        if pair is None:
            continue
        try:
            start = float(pair[0])
            end = float(pair[1])
        except (TypeError, ValueError):
            continue
        if math.isfinite(start) and math.isfinite(end) and end >= start:
            pairs.append((start, end))
    return pairs or None


def _merge_intervals(pairs):
    merged = []
    for start, end in sorted(pairs):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return merged


def _coverage_api_raw(game_id, module=None):
    module = module if module is not None else timeline
    for name in ('coverage', 'game_coverage'):
        api = getattr(module, name, None)
        if not callable(api):
            continue
        try:
            return api(game_id=game_id)
        except TypeError:
            pass
        except Exception:
            return None
        try:
            raw = api(game_id)
        except TypeError:
            try:
                raw = api()
            except Exception:
                return None
            if isinstance(raw, dict) and game_id in raw:
                return raw[game_id]
            return raw
        except Exception:
            return None
    return None


def _coverage_table(con, game_id):
    for table in _COVERAGE_TABLES:
        columns = _table_columns(con, table)
        if not columns or 'game_id' not in columns:
            continue
        start_col = next((name for name in _START_COLUMNS if name in columns), None)
        end_col = next((name for name in _END_COLUMNS if name in columns), None)
        if start_col is None or end_col is None:
            continue
        try:
            rows = con.execute(
                'SELECT %s AS s, %s AS e FROM %s WHERE game_id=?'
                % (start_col, end_col, table), (game_id,)).fetchall()
        except sqlite3.Error:
            continue
        pairs = []
        for row in rows:
            try:
                start = float(row['s'])
                end = float(row['e'])
            except (TypeError, ValueError):
                continue
            if math.isfinite(start) and math.isfinite(end) and end >= start:
                pairs.append((start, end))
        if pairs:
            scale = 1000.0 if ('ms' in start_col.lower() or 'ms' in end_col.lower()) else 1.0
            return pairs, table, scale
    return None, None, None


def _coverage_events(con, game_id):
    try:
        rows = con.execute(
            "SELECT json FROM events WHERE game_id=?"
            " AND kind IN ('coverage', 'intervals', 'heartbeat')",
            (game_id,)).fetchall()
    except sqlite3.Error:
        return None
    pairs = []
    for row in rows:
        found = _intervals(_load_json(row['json']))
        if found:
            pairs.extend(found)
    return pairs or None


def _coverage_result(pairs, source, meta, duration, scale=None):
    if not pairs:
        return None
    merged = _merge_intervals(pairs)
    if scale is None:
        max_abs = max(abs(value) for pair in merged for value in pair)
        scale = 1000.0 if max_abs > 1e11 else 1.0
    intervals = [{'start': round(start / scale, 3), 'end': round(end / scale, 3)}
                 for start, end in merged]
    covered = sum(end - start for start, end in merged) / scale
    ratio = None
    if isinstance(duration, (int, float)) and duration > 0:
        ratio = max(0.0, min(1.0, covered / float(duration)))
    adequate = None
    if isinstance(meta, dict):
        for key in ('observedRatio', 'ratio', 'coverage'):
            value = meta.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                ratio = max(0.0, min(1.0, float(value)))
                break
        value = meta.get('coveredSeconds', meta.get('covered_seconds'))
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            covered = float(value)
        flag = meta.get('adequate')
        if isinstance(flag, bool):
            adequate = flag
    if adequate is None:
        if ratio is not None:
            adequate = ratio >= COVERAGE_ADEQUATE_RATIO
        else:
            adequate = covered >= COVERAGE_MIN_SECONDS
    return {
        'available': True,
        'source': source,
        'intervals': intervals,
        'coveredSeconds': round(covered, 3),
        'observedRatio': None if ratio is None else round(ratio, 3),
        'adequate': bool(adequate),
    }


def _meta_scale(raw):
    """Detect millisecond intervals from explicit ``*Ms``/``*_ms`` keys."""
    if not isinstance(raw, dict):
        return None
    if any(key in raw for key in ('startMs', 'start_ms', 'endMs', 'end_ms')):
        return 1000.0
    for key in ('intervals', 'coverage', 'periods', 'windows'):
        value = raw.get(key)
        if isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, dict) and any(
                        name in item for name in ('startMs', 'start_ms', 'endMs', 'end_ms')):
                    return 1000.0
            break
    return None


def game_coverage(con, game_id, duration=None, timeline_module=None):
    """Best-effort collection coverage for one game.

    Tries, in order: an optional ``timeline.coverage(game_id)`` API (from
    ``timeline_module`` when supplied), an intervals/coverage table, then
    coverage events carrying interval payloads. Returns ``None`` when nothing
    was recorded; never invents coverage.
    """
    raw = _coverage_api_raw(game_id, timeline_module)
    pairs = None
    source = 'timeline.coverage'
    scale = _meta_scale(raw)
    meta = raw if isinstance(raw, dict) else None
    pairs = _intervals(raw)
    if not pairs:
        pairs, table, table_scale = _coverage_table(con, game_id)
        if pairs:
            source = 'intervals:%s' % table
            scale = table_scale
        else:
            pairs = _coverage_events(con, game_id)
            scale = None
            if pairs:
                source = 'coverage_events'
    if not pairs:
        return None
    return _coverage_result(pairs, source, meta, duration, scale)


def _game_duration(game):
    started_at = _num(game.get('started_at'))
    ended_at = _num(game.get('ended_at'))
    if started_at is None or ended_at is None or ended_at <= started_at:
        return None
    return (ended_at - started_at) / 1000.0


def _game_versions(game):
    catalog = None
    for key in ('catalog_version', 'catalogVersion', 'items_version', 'itemVersion'):
        if game.get(key):
            catalog = _text(game.get(key), 64)
            break
    rules = None
    for key in ('rules_version', 'rulesVersion', 'rule_version', 'ruleVersion'):
        if game.get(key):
            rules = _text(game.get(key), 64)
            break
    patch = None
    for key in ('patch', 'game_version', 'gameVersion'):
        if game.get(key):
            patch = _text(game.get(key), 64)
            break
    return {
        'catalog': catalog,
        'rules': rules,
        'patch': patch,
        'available': bool(catalog or rules),
    }


def _objective_is_outcome(event):
    if event.get('kind') in ('objective_kill', 'objective_outcome'):
        return True
    data = _load_json(event.get('json'))
    if isinstance(data, dict):
        for key in ('killed', 'slain', 'secured', 'taken', 'outcome', 'objectiveKill'):
            if data.get(key) is True:
                return True
        if data.get('secondsLeft') is not None:
            return False
    label = event.get('label')
    if isinstance(label, str) and OUTCOME_RE.search(label):
        return True
    return False


def _fetch_events(con, game_id):
    try:
        rows = con.execute(
            'SELECT id, t_game, at_ts, kind, label, json FROM events'
            ' WHERE game_id=? ORDER BY id', (game_id,)).fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def _fetch_advice(con, game_id):
    try:
        rows = con.execute(
            'SELECT id, t_game, at_ts, kind, text FROM advice'
            ' WHERE game_id=? ORDER BY id', (game_id,)).fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def _fetch_inference(con, game_id):
    try:
        rows = con.execute(
            'SELECT status FROM inference WHERE game_id=?', (game_id,)).fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def _fetch_history(con, limit, before_ended_at=None, before_id=None):
    """Ended games strictly older than the reviewed game.

    History is anchored to the reviewed game: games played afterwards are
    never mixed in, whatever order they were reviewed in.
    """
    try:
        if before_ended_at is None:
            rows = con.execute(
                'SELECT id, champ, started_at, ended_at FROM games'
                ' WHERE ended_at IS NOT NULL ORDER BY ended_at DESC, id DESC LIMIT ?',
                (limit,)).fetchall()
        else:
            rows = con.execute(
                'SELECT id, champ, started_at, ended_at FROM games'
                ' WHERE ended_at IS NOT NULL'
                ' AND (ended_at < ? OR (ended_at = ? AND id < ?))'
                ' ORDER BY ended_at DESC, id DESC LIMIT ?',
                (before_ended_at, before_ended_at, before_id or 0, limit)).fetchall()
    except sqlite3.Error:
        return []
    games = []
    for row in rows:
        game_id = row['id']
        try:
            deaths = con.execute(
                'SELECT t_game FROM events WHERE game_id=? AND kind=?',
                (game_id, 'death')).fetchall()
        except sqlite3.Error:
            deaths = []
        times = [t for t in (_num(d['t_game']) for d in deaths) if t is not None]
        games.append({
            'id': game_id,
            'champ': row['champ'],
            'deaths': len(deaths),
            'clocks': len(times),
            'early8': sum(1 for t in times if t < VERY_EARLY_DEATH_SECONDS),
        })
    return games


def _parse_death(event):
    t = _num(event.get('t_game'))
    label = event.get('label') if isinstance(event.get('label'), str) else ''
    data = _load_json(event.get('json'))
    clock = None
    killer = None
    if isinstance(data, dict):
        raw_clock = _text(data.get('clock'), 16)
        raw_killer = _text(data.get('killer'), 120)
        if raw_clock:
            clock = raw_clock
        if raw_killer:
            killer = raw_killer
    if not clock:
        match = DEATH_LABEL_RE.match(label.strip())
        if match:
            clock = match.group(1)
            if not killer:
                killer = match.group(2).strip() or None
    if not clock and t is not None:
        clock = _fmt(t)
    return {'t': t, 'clock': clock, 'killer': killer or 'unknown killer'}


def _parse_signature(label):
    if not isinstance(label, str):
        return None
    match = SIGNATURE_RE.search(label)
    if not match:
        return None
    signature = {}
    for token in match.group(1).split(','):
        token = token.strip()
        if not token:
            continue
        item = SIGNATURE_ITEM_RE.match(token)
        if item:
            signature[int(item.group(1))] = int(item.group(2))
    return signature


def _diff_signatures(previous, current):
    added = {iid: count for iid, count in current.items() if count > previous.get(iid, 0)}
    removed = {iid: count - current.get(iid, 0)
               for iid, count in previous.items() if count > current.get(iid, 0)}
    return added, removed


def _item_name(iid, item_names):
    if item_names:
        name = item_names.get(iid)
        if isinstance(name, str) and name.strip():
            return name.strip()
    return '#%d' % iid


def _is_completed(iid, item_into, item_costs):
    if not item_into or iid not in item_into:
        return False
    if item_into.get(iid):
        return False
    if item_costs is not None:
        cost = item_costs.get(iid)
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            return False
        if cost < COMPLETED_ITEM_MIN_COST:
            return False
    return True


def _collect_stats(events, advice, inference, history, item_names, item_into, item_costs):
    deaths = [_parse_death(event) for event in events if event.get('kind') == 'death']
    death_clocks = [d['clock'] for d in deaths if d['clock'] is not None]
    early10_clocks = [d['clock'] for d in deaths
                      if d['t'] is not None and d['t'] < EARLY_DEATH_SECONDS and d['clock']]
    late_clocks = [d['clock'] for d in deaths
                   if d['t'] is not None and d['t'] >= LATE_GAME_SECONDS and d['clock']]
    cluster = None
    timed = sorted((d for d in deaths if d['t'] is not None), key=lambda d: d['t'])
    for index in range(1, len(timed)):
        gap = timed[index]['t'] - timed[index - 1]['t']
        if gap <= DEATH_CLUSTER_SECONDS:
            cluster = {'a': timed[index - 1], 'b': timed[index], 'gap': gap}
            break

    item_events = []
    for event in events:
        if event.get('kind') != 'item':
            continue
        t = _num(event.get('t_game'))
        item_events.append({
            't': t,
            'clock': _fmt(t) if t is not None else None,
            'sig': _parse_signature(event.get('label')),
        })

    baseline = None
    additions = []
    removals = []
    previous = None
    for event in item_events:
        signature = event['sig']
        if signature is None:
            continue
        if previous is None:
            baseline = {'sig': signature, 'clock': event['clock']}
        else:
            added, removed = _diff_signatures(previous, signature)
            for iid, count in sorted(added.items()):
                additions.append({
                    'iid': iid, 'count': count, 'clock': event['clock'],
                    'name': _item_name(iid, item_names),
                    'completed': _is_completed(iid, item_into, item_costs),
                })
            for iid, count in sorted(removed.items()):
                removals.append({'iid': iid, 'count': count, 'clock': event['clock']})
        previous = signature

    reminders = []
    outcomes = []
    for event in events:
        if event.get('kind') not in _OBJECTIVE_KINDS:
            continue
        t = _num(event.get('t_game'))
        entry = {
            't': t,
            'clock': _fmt(t) if t is not None else None,
            'label': _text(event.get('label'), 120) or 'objective window',
        }
        if _objective_is_outcome(event):
            outcomes.append(entry)
        else:
            reminders.append(entry)

    event_kinds = {}
    for event in events:
        kind = event.get('kind') or 'unknown'
        event_kinds[kind] = event_kinds.get(kind, 0) + 1

    advice_kinds = {}
    for entry in advice:
        kind = entry.get('kind') or 'note'
        advice_kinds[kind] = advice_kinds.get(kind, 0) + 1

    inference_statuses = {}
    for entry in inference:
        status = entry.get('status') or 'unknown'
        inference_statuses[status] = inference_statuses.get(status, 0) + 1

    history_with_clocks = [g for g in history if g['clocks']]
    history_two_early = [g for g in history if g['early8'] >= 2]

    return {
        'deaths': deaths,
        'death_clocks': death_clocks,
        'deaths_without_clock': len(deaths) - len(death_clocks),
        'early10': len(early10_clocks),
        'early10_clocks': early10_clocks,
        'late': len(late_clocks),
        'late_clocks': late_clocks,
        'cluster': cluster,
        'item_events': len(item_events),
        'baseline': baseline,
        'additions': additions,
        'removals': removals,
        'objectives': reminders,
        'objective_reminders': reminders,
        'objective_outcomes': outcomes,
        'event_kinds': event_kinds,
        'event_count': len(events),
        'advice_count': len(advice),
        'advice_kinds': advice_kinds,
        'inference_count': len(inference),
        'inference_statuses': inference_statuses,
        'inference_used': sum(c for k, c in inference_statuses.items() if k != 'skipped'),
        'inference_limit': timeline.DEFAULT_BUDGET,
        'history': history,
        'history_with_clocks': len(history_with_clocks),
        'history_two_early': len(history_two_early),
        'history_two_early_ids': [g['id'] for g in history_two_early],
        'coverage': None,
        'coverage_adequate': False,
        'versions': {'catalog': None, 'rules': None, 'patch': None, 'available': False},
    }


def _coverage_evidence(coverage):
    if not coverage:
        return 'coverage: not recorded'
    ratio = coverage.get('observedRatio')
    if ratio is not None:
        return 'coverage %d%% of the recorded session' % round(ratio * 100)
    return 'coverage: %gs recorded' % round(coverage.get('coveredSeconds') or 0)


def _p_early_deaths(s):
    if s['early10'] < 2:
        return None
    evidence = ['%d of %d recorded deaths before 10:00' % (s['early10'], len(s['deaths']))]
    if s['early10_clocks']:
        evidence.append('clocks: ' + ', '.join(s['early10_clocks'][:MAX_LISTED]))
    return _prio(
        'early_deaths',
        'Stop the early deaths',
        'Play the first 10 minutes to survive: track the enemy jungler before committing'
        ' and value a safe reset over a contested camp.',
        evidence, 'this game')


def _p_recurring_early_deaths(s):
    if s['history_two_early'] < 2 or s['history_with_clocks'] < 2:
        return None
    games = [g for g in s['history'] if g['early8'] >= 2]
    labelled = ['#%s%s' % (g['id'], ' (%s)' % g['champ'] if g['champ'] else '') for g in games]
    evidence = [
        '2 or more deaths before 8:00 in %d of the last %d games with recorded death clocks'
        % (s['history_two_early'], s['history_with_clocks']),
        'games: ' + ', '.join(labelled[:MAX_LISTED]),
    ]
    return _prio(
        'recurring_early_deaths',
        'Fix the recurring slow start',
        'Make the first 8 minutes a fixed script: ward the river entrance before the first'
        ' objective window and refuse 50/50 early fights.',
        evidence, 'recent games')


def _p_death_cluster(s):
    cluster = s['cluster']
    if not cluster:
        return None
    gap = _fmt(cluster['gap']) or str(int(cluster['gap']))
    evidence = [
        'deaths at %s to %s and %s to %s (%s apart)'
        % (cluster['a']['clock'] or 'unknown clock', cluster['a']['killer'],
           cluster['b']['clock'] or 'unknown clock', cluster['b']['killer'], gap),
        '%d deaths in total' % len(s['deaths']),
    ]
    return _prio(
        'death_cluster',
        'Reset safely after dying',
        'After a death, take the safe respawn path: clear the nearest safe camp or hold a'
        ' lane until the next objective timer is known instead of walking into the same fight.',
        evidence, 'this game')


def _p_late_deaths(s):
    if s['late'] < 2:
        return None
    evidence = ['%d recorded deaths after 20:00' % s['late']]
    if s['late_clocks']:
        evidence.append('clocks: ' + ', '.join(s['late_clocks'][:MAX_LISTED]))
    return _prio(
        'late_deaths',
        'Close out the late game',
        'From 20:00 onward, move with the team and avoid side-lane fights without vision;'
        ' one late death can hand over Baron or end the game.',
        evidence, 'this game')


def _p_objective_coverage(s):
    if s['objective_reminders'] or s['objective_outcomes']:
        return None
    evidence = [
        '0 objective-window reminders recorded for this game',
        '%d timeline events recorded in total' % s['event_count'],
    ]
    if not s['coverage_adequate']:
        evidence.append(_coverage_evidence(s.get('coverage')))
        return _prio(
            'objective_coverage',
            'Objective data was incomplete',
            'No objective-window reminders or outcomes were recorded, and collection coverage'
            ' was not adequate, so objective setup cannot be reviewed. This is a data-quality'
            ' finding, not a performance result: keep the collector running through the whole'
            ' game before drawing conclusions.',
            evidence, 'data quality')
    return _prio(
        'objective_coverage',
        'Review objective setup',
        'Next game, note every dragon and Baron timer: path toward the objective before it'
        ' opens and reset beforehand instead of reacting after it spawns.',
        evidence, 'this game')


def _p_item_timing(s):
    if not s['item_events']:
        return None
    completions = [a for a in s['additions'] if a.get('completed')]
    evidence = ['%d inventory-change events recorded' % s['item_events']]
    if completions:
        first = completions[0]
        evidence.append('first completed item at %s: %s'
                        % (first['clock'] or 'unknown time', first['name']))
    else:
        evidence.append('no completed item identified from the recorded inventory changes')
    return _prio(
        'item_timing',
        'Time recalls around item breakpoints',
        'Plan the first recall around the next completed item or component instead of'
        ' staying on the map with unspent gold.',
        evidence, 'this game')


def _p_item_missing(s):
    if s['item_events']:
        return None
    return _prio(
        'item_missing',
        'Track item timing next game',
        'No inventory-change events were recorded, so purchase timing could not be reviewed.'
        ' Keep the collector running through the whole game.',
        ['0 inventory-change events recorded'],
        'this game')


def _p_first_death(s):
    if not s['deaths']:
        return None
    first = s['deaths'][0]
    return _prio(
        'first_death',
        'Review the first death',
        'Walk back the first death from the timeline: what information was missing before the'
        ' fight, and what safe alternative was available?',
        ['first recorded death at %s to %s'
         % (first['clock'] or 'unknown clock', first['killer']),
         '%d deaths recorded in total' % len(s['deaths'])],
        'this game')


def _p_no_deaths(s):
    if s['deaths']:
        return None
    if not s['coverage_adequate']:
        return _prio(
            'no_deaths',
            'No deaths recorded — coverage incomplete',
            'No deaths were recorded, but collection coverage for this game was not adequate,'
            ' so a clean death record cannot be concluded. This is a data-quality finding, not'
            ' a performance result: keep the collector running and re-check coverage before'
            ' drawing conclusions.',
            ['0 deaths recorded', _coverage_evidence(s.get('coverage'))],
            'data quality')
    evidence = ['0 deaths recorded']
    coverage = s.get('coverage') or {}
    if coverage.get('observedRatio') is not None:
        evidence.append('observed across %d%% of the recorded session'
                        % round(coverage['observedRatio'] * 100))
    return _prio(
        'no_deaths',
        'Keep the clean death record',
        'No deaths were observed while collection coverage was adequate. Identify which'
        ' decisions kept the game safe and repeat them in the next game.',
        evidence, 'this game')


def _p_advice_review(s):
    if not s['advice_count']:
        return None
    breakdown = ', '.join('%s %d' % (kind, count)
                          for kind, count in sorted(s['advice_kinds'].items()))
    return _prio(
        'advice_review',
        'Compare advice with what happened',
        'Read the recorded advice against the timeline and mark which calls were followed and'
        ' which were not, then pick one habit to carry into the next game.',
        ['%d advice entries recorded (%s)' % (s['advice_count'], breakdown)],
        'this game')


def _p_tracking(s):
    return _prio(
        'tracking_coverage',
        'Log the missing metrics manually',
        'This review can only cite recorded events. Note damage, vision, and objective'
        ' participation during the next game so later reviews can use them.',
        ['%d timeline events and %d advice entries recorded'
         % (s['event_count'], s['advice_count']),
         _coverage_evidence(s.get('coverage'))],
        'next game')


def _p_timeline_review(s):
    return _prio(
        'timeline_review',
        'Walk the recorded timeline',
        'Review the timeline entries in order and tag the decisions that changed the game.',
        ['%d timeline events recorded' % s['event_count']],
        'this game')


RULES = (
    _p_early_deaths,
    _p_recurring_early_deaths,
    _p_death_cluster,
    _p_late_deaths,
    _p_objective_coverage,
    _p_item_timing,
    _p_item_missing,
    _p_first_death,
    _p_no_deaths,
)
FALLBACKS = (
    _p_advice_review,
    _p_tracking,
    _p_timeline_review,
)


def _build_priorities(stats):
    picked = []
    for rule in RULES + FALLBACKS:
        if len(picked) >= 3:
            break
        priority = rule(stats)
        if priority and all(existing['id'] != priority['id'] for existing in picked):
            picked.append(priority)
    return picked[:3]


def _death_line(death):
    return '%s to %s' % (death['clock'] or 'unknown clock', death['killer'])


def _build_observations(game, stats, duration, item_names):
    observations = []
    champ = _text(game.get('champ'), 64) or 'unknown champion'
    mode = _text(game.get('mode'), 32) or 'unknown mode'
    map_number = game.get('map')
    map_txt = ''
    if isinstance(map_number, int) and not isinstance(map_number, bool) and map_number > 0:
        map_txt = ', map %d' % map_number
    observations.append(_obs('game', 'Ended game #%s: %s, %s%s.'
                             % (game.get('id'), champ, mode, map_txt)))

    if duration is not None:
        observations.append(_obs(
            'duration',
            'Observed session length %s (first timeline observation to game end).' % _fmt(duration)))

    coverage = stats.get('coverage')
    if coverage is None:
        observations.append(_obs(
            'coverage', 'Collection coverage: not recorded for this game.'))
    else:
        ratio = coverage.get('observedRatio')
        bits = []
        if ratio is not None:
            bits.append('%d%% of the recorded session' % round(ratio * 100))
        bits.append('%gs covered' % round(coverage.get('coveredSeconds') or 0))
        bits.append('source: %s' % coverage.get('source'))
        observations.append(_obs(
            'coverage', 'Collection coverage: %s.' % ', '.join(bits)))

    if stats['event_count']:
        breakdown = ', '.join('%s %d' % (kind, count)
                              for kind, count in sorted(stats['event_kinds'].items()))
        observations.append(_obs(
            'events', 'Timeline events: %d recorded (%s).' % (stats['event_count'], breakdown)))
    else:
        observations.append(_obs('events', 'Timeline events: none recorded for this game.'))

    if stats['deaths']:
        listed = '; '.join(_death_line(d) for d in stats['deaths'][:MAX_LISTED])
        suffix = ' (+%d more)' % (len(stats['deaths']) - MAX_LISTED) \
            if len(stats['deaths']) > MAX_LISTED else ''
        observations.append(_obs(
            'deaths', 'Deaths: %d recorded — %s%s.' % (len(stats['deaths']), listed, suffix)))
    else:
        observations.append(_obs('deaths', 'Deaths: none recorded.'))

    if not stats['item_events']:
        observations.append(_obs(
            'items', 'Item timing: no inventory-change events recorded.'))
    else:
        if stats['baseline'] is not None and stats['baseline']['sig']:
            names = ', '.join(_item_name(iid, item_names)
                              for iid in sorted(stats['baseline']['sig']))
            observations.append(_obs(
                'items_baseline',
                'Inventory baseline at %s: %s.'
                % (stats['baseline']['clock'] or 'unknown time', names)))
        completions = [a for a in stats['additions'] if a['completed']]
        additions = [a for a in stats['additions'] if not a['completed']]
        if completions:
            listed = '; '.join('%s %s' % (c['clock'] or 'unknown time', c['name'])
                               for c in completions[:MAX_LISTED])
            observations.append(_obs(
                'items_completed',
                'Items completed: %s.' % listed))
        if additions:
            listed = '; '.join('%s %s' % (a['clock'] or 'unknown time', a['name'])
                               for a in additions[:MAX_LISTED])
            observations.append(_obs(
                'items_added', 'Inventory additions: %s.' % listed))
        if not completions and not additions:
            observations.append(_obs(
                'items_completed',
                'Items completed: none identified across %d inventory-change event(s).'
                % stats['item_events']))

    if stats['objective_reminders']:
        listed = '; '.join('%s at %s' % (o['label'], o['clock'] or 'unknown time')
                           for o in stats['objective_reminders'][:MAX_LISTED])
        observations.append(_obs(
            'objective_reminders',
            'Objective-window reminders recorded: %s.' % listed))
    else:
        observations.append(_obs(
            'objective_reminders', 'Objective-window reminders: none recorded.'))

    if stats['objective_outcomes']:
        listed = '; '.join('%s at %s' % (o['label'], o['clock'] or 'unknown time')
                           for o in stats['objective_outcomes'][:MAX_LISTED])
        observations.append(_obs(
            'objective_outcomes', 'Objective outcomes recorded: %s.' % listed))
    else:
        observations.append(_obs(
            'objective_outcomes', 'Objective outcomes: none recorded.'))

    if stats['advice_count']:
        breakdown = ', '.join('%s %d' % (kind, count)
                              for kind, count in sorted(stats['advice_kinds'].items()))
        observations.append(_obs(
            'advice', 'Advice: %d entries recorded (%s).' % (stats['advice_count'], breakdown)))
    else:
        observations.append(_obs('advice', 'Advice: none recorded.'))

    if stats['inference_count']:
        breakdown = ', '.join('%s %d' % (status, count)
                              for status, count in sorted(stats['inference_statuses'].items()))
        observations.append(_obs(
            'inference',
            'Inference: %d requests recorded (%s); budget %d/%d.'
            % (stats['inference_count'], breakdown,
               stats['inference_used'], stats['inference_limit'])))
    else:
        observations.append(_obs('inference', 'Inference: no requests recorded.'))

    versions = stats.get('versions') or {}
    if versions.get('available'):
        bits = []
        if versions.get('catalog'):
            bits.append('catalog %s' % versions['catalog'])
        if versions.get('rules'):
            bits.append('rules %s' % versions['rules'])
        if versions.get('patch'):
            bits.append('patch %s' % versions['patch'])
        observations.append(_obs('versions', 'Catalog/rules version: %s.' % ', '.join(bits)))
    else:
        observations.append(_obs(
            'versions',
            'Catalog/rules version: not recorded for this game (current catalog used).'))
    return observations


def _build_unavailable(stats, duration):
    unavailable = list(UNAVAILABLE_BASE)
    coverage = stats.get('coverage')
    if coverage is None:
        unavailable.append(
            'Collection coverage was not recorded, so missing observations cannot be'
            ' distinguished from clean play.')
    elif not coverage.get('adequate'):
        unavailable.append(
            'Collection coverage was incomplete (%s); performance conclusions including'
            ' zero-death praise were withheld.' % _coverage_evidence(coverage))
    if not (stats.get('versions') or {}).get('available'):
        unavailable.append(
            'Item catalog and game-rules version were not recorded for this game; item names'
            ' and completion classification may come from the current catalog.')
    if not stats['item_events']:
        unavailable.append(
            'No inventory-change events were recorded, so item timing could not be reviewed.')
    if stats['deaths_without_clock']:
        unavailable.append(
            '%d death event(s) had no game clock recorded.' % stats['deaths_without_clock'])
    if duration is None:
        unavailable.append(
            'Session duration could not be computed (missing start or end timestamp).')
    if stats['history_with_clocks'] < 2:
        unavailable.append(
            'Cross-game death timing needs at least two ended games with recorded death clocks'
            ' before the reviewed game.')
    return unavailable


def _build_data_quality(stats, duration):
    coverage = stats.get('coverage')
    notes = []
    if coverage is None:
        level = 'unavailable'
        notes.append('Collection coverage was not recorded; missing deaths, items, and'
                     ' objective windows cannot be distinguished from clean play.')
    elif coverage.get('adequate'):
        level = 'ok'
        ratio = coverage.get('observedRatio')
        notes.append('Collection coverage was adequate%s.'
                     % ('' if ratio is None else ' (%d%%)' % round(ratio * 100)))
    else:
        level = 'partial'
        ratio = coverage.get('observedRatio')
        notes.append('Collection coverage was incomplete%s; performance conclusions'
                     ' (including zero-death praise) were withheld.'
                     % ('' if ratio is None else ' (%d%%)' % round(ratio * 100)))
    if not stats['item_events']:
        notes.append('No inventory-change events were recorded, so purchase timing was not'
                     ' reviewed.')
    if duration is None:
        notes.append('Session duration could not be computed.')
    if not (stats.get('versions') or {}).get('available'):
        notes.append('Catalog/rules version was not recorded; item names and completion'
                     ' classification come from the current catalog.')
    return {'level': level, 'coverage': coverage, 'notes': notes}


def build_review(game_id=None, item_names=None, item_into=None, item_costs=None):
    if game_id is not None:
        try:
            game_id = int(game_id)
        except (TypeError, ValueError):
            return _no_review('invalid game id')
    try:
        con = _connect()
    except (sqlite3.Error, OSError) as ex:
        return _no_review('timeline database unavailable (%s)' % type(ex).__name__)
    try:
        try:
            if game_id is None:
                row = con.execute(
                    'SELECT * FROM games WHERE ended_at IS NOT NULL'
                    ' ORDER BY ended_at DESC, id DESC LIMIT 1').fetchone()
            else:
                row = con.execute('SELECT * FROM games WHERE id=?', (game_id,)).fetchone()
        except sqlite3.Error:
            return _no_review('timeline tables are unavailable')
        if row is None:
            return _no_review('no ended game recorded' if game_id is None
                              else 'game %s was not found' % game_id)
        game = dict(row)
        if game.get('ended_at') is None:
            return _no_review('the selected game has not ended yet')
        duration = _game_duration(game)
        events = _fetch_events(con, game['id'])
        advice = _fetch_advice(con, game['id'])
        inference = _fetch_inference(con, game['id'])
        history = _fetch_history(con, HISTORY_GAMES,
                                 before_ended_at=game.get('ended_at'),
                                 before_id=game.get('id'))
        coverage = game_coverage(con, game['id'], duration)
    finally:
        con.close()

    versions = _game_versions(game)
    stats = _collect_stats(events, advice, inference, history,
                           item_names, item_into, item_costs)
    stats['coverage'] = coverage
    stats['coverage_adequate'] = bool(coverage and coverage.get('adequate'))
    stats['versions'] = versions
    observations = _build_observations(game, stats, duration, item_names)
    priorities = _build_priorities(stats)
    unavailable = _build_unavailable(stats, duration)
    data_quality = _build_data_quality(stats, duration)

    return {
        'ok': True,
        'status': 'ok',
        'game': {
            'id': game.get('id'),
            'sessionId': game.get('session_id'),
            'champ': game.get('champ'),
            'mode': game.get('mode'),
            'map': game.get('map'),
            'startedAt': game.get('started_at'),
            'endedAt': game.get('ended_at'),
            'durationSec': duration,
        },
        'observations': observations,
        'priorities': priorities,
        'unavailable': unavailable,
        'coverage': coverage,
        'versions': versions,
        'dataQuality': data_quality,
    }
