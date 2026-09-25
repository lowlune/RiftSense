"""Deterministic post-game review built from the RiftSense timeline database.

Every statement in the review is derived from recorded rows (games, events,
advice, inference). Fields that were never observed are reported in
``unavailable`` instead of being guessed. No model/LLM call is made.
"""

import json
import os
import re
import sqlite3

try:
    from . import timeline
except ImportError:  # direct-script import (python3 ui/review.py)
    import timeline

EARLY_DEATH_SECONDS = 600
VERY_EARLY_DEATH_SECONDS = 480
DEATH_CLUSTER_SECONDS = 180
LATE_GAME_SECONDS = 1200
HISTORY_GAMES = 5
MAX_LISTED = 8
COMPLETED_ITEM_MIN_COST = 2000
MAX_TEXT = 400

DEATH_LABEL_RE = re.compile(r'^died\s+(\d+:\d+)\s+to\s+(.+)$', re.IGNORECASE)
SIGNATURE_RE = re.compile(r'inventory changed:\s*(.*)$', re.IGNORECASE)
SIGNATURE_ITEM_RE = re.compile(r'^(\d+)x(\d+)$')

UNAVAILABLE_BASE = (
    'Damage dealt and taken was not recorded.',
    'Vision score and ward coverage were not recorded.',
    'Earned gold and CS progression were not recorded.',
    'Positioning, wave state, and ability cooldowns were not recorded.',
    'Actual dragon/Baron kills and participation were not recorded;'
    ' only objective-window reminders may appear.',
)


def _no_review(reason):
    return {
        'ok': False,
        'status': 'no_review',
        'game': None,
        'observations': [],
        'priorities': [],
        'unavailable': [reason] if reason else [],
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


def _fetch_history(con, limit):
    try:
        rows = con.execute(
            'SELECT id, champ, started_at, ended_at FROM games WHERE ended_at IS NOT NULL'
            ' ORDER BY ended_at DESC, id DESC LIMIT ?', (limit,)).fetchall()
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

    objectives = []
    for event in events:
        if event.get('kind') != 'objective':
            continue
        t = _num(event.get('t_game'))
        objectives.append({
            't': t,
            'clock': _fmt(t) if t is not None else None,
            'label': _text(event.get('label'), 120) or 'objective window',
        })

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
        'objectives': objectives,
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
    }


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
    if s['objectives']:
        return None
    return _prio(
        'objective_coverage',
        'Review objective setup',
        'Next game, note every dragon and Baron timer: path toward the objective before it'
        ' opens and reset beforehand instead of reacting after it spawns.',
        ['0 objective-window events recorded for this game',
         '%d timeline events recorded in total' % s['event_count']],
        'this game')


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
    return _prio(
        'no_deaths',
        'Keep the clean death record',
        'No deaths were recorded. Identify which decisions kept the game safe and repeat them'
        ' in the next game.',
        ['0 deaths recorded'],
        'this game')


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
         % (s['event_count'], s['advice_count'])],
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

    if stats['objectives']:
        listed = '; '.join('%s at %s' % (o['label'], o['clock'] or 'unknown time')
                           for o in stats['objectives'][:MAX_LISTED])
        observations.append(_obs(
            'objectives', 'Objective windows recorded: %s.' % listed))
    else:
        observations.append(_obs(
            'objectives', 'Objective windows: none recorded.'))

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
    return observations


def _build_unavailable(stats, duration):
    unavailable = list(UNAVAILABLE_BASE)
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
            'Cross-game death timing needs at least two ended games with recorded death clocks.')
    return unavailable


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
        events = _fetch_events(con, game['id'])
        advice = _fetch_advice(con, game['id'])
        inference = _fetch_inference(con, game['id'])
        history = _fetch_history(con, HISTORY_GAMES)
    finally:
        con.close()

    started_at = _num(game.get('started_at'))
    ended_at = _num(game.get('ended_at'))
    duration = None
    if started_at is not None and ended_at is not None and ended_at > started_at:
        duration = (ended_at - started_at) / 1000.0

    stats = _collect_stats(events, advice, inference, history,
                           item_names, item_into, item_costs)
    observations = _build_observations(game, stats, duration, item_names)
    priorities = _build_priorities(stats)
    unavailable = _build_unavailable(stats, duration)

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
    }
