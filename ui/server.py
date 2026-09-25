import contextlib
import http.server
import json
import os
import re
import sqlite3
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

try:
    import fcntl
except ImportError:
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None
try:
    import purchase
except ImportError:  # package-style import (python3 -m ui.server)
    from . import purchase
try:
    import timeline
except ImportError:  # package-style import (python3 -m ui.server)
    from . import timeline

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
CHAMP_FILE = os.path.join(ROOT, 'champion.json')
ITEM_FILE = os.path.join(ROOT, 'items.json')
BUILD_FILE = os.path.join(ROOT, 'build_intent.txt')
COACH_FILE = os.path.join(ROOT, 'coach_latest.txt')
DEATH_FILE = os.path.join(ROOT, 'death_latest.txt')
DEATH_META_FILE = os.path.join(ROOT, 'death_latest.json')
EPOCH_FILE = os.path.join(ROOT, 'game_epoch.json')
DB_PATH = os.path.join(os.path.expanduser('~'), '.local', 'share', 'opencode', 'opencode.db')
PORT = int(os.environ.get('RIFTSENSE_PORT', '7777'))
STARTED_AT = time.time()

_LCU_STATE = {
    'reachable': None,
    'status': 'unknown',
    'error': None,
    'checkedAt': None,
    'lastSnapshotAt': None,
}
_LCU_LOCK = threading.Lock()

DEFAULT_ASSET_VERSION = '16.18.1'
VERSIONS_URL = 'https://ddragon.leagueoflegends.com/api/versions.json'
CDN_URL = 'https://ddragon.leagueoflegends.com/cdn/%s/data/en_US/%s.json'
EPOCH_DRIFT_MS = 120000
GAME_TIME_MAX = 6 * 60 * 60
NO_GAME_REASONS = ('no_active_game', 'client_unreachable')
DEATH_STRUCTURED_STATUS = ('ok',)
DEATH_STRUCTURED_TTL = 900
DEATH_STRUCTURED_MAX_BYTES = 262144
DEATH_STRUCTURED_MAX_ITEMS = 20

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

ASSETS = {
    'version': None,
    'championVersion': None,
    'itemVersion': None,
    'championCount': 0,
    'itemCount': 0,
    'mismatch': False,
}

CHAMPS = {}
ITEMS = {}
ITEM_NAMES = {}
ITEM_DISPLAY = {}
ITEM_COSTS = {}
ITEM_INTO = {}
PURCHASE_CATALOG = {}

_UNSET = object()


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    return None


def _read_json(path):
    with open(path, 'r', encoding='utf-8-sig') as f:
        return json.load(f)


def _validate_catalog(obj, kind):
    if not isinstance(obj, dict):
        raise ValueError('catalog root is not an object')
    data = obj.get('data')
    if not isinstance(data, dict) or not data:
        raise ValueError('catalog data missing or empty')
    if obj.get('type') and obj.get('type') != kind:
        raise ValueError('catalog type mismatch')
    return obj


def _try_catalog(path, kind):
    try:
        return _validate_catalog(_read_json(path), kind)
    except Exception:
        return None


def _fetch_latest_version():
    try:
        with urllib.request.urlopen(VERSIONS_URL, timeout=15) as r:
            versions = json.loads(r.read().decode('utf-8'))
        if isinstance(versions, list) and versions and isinstance(versions[0], str):
            return versions[0]
    except Exception:
        pass
    return None


def _download_asset(kind, path, version):
    url = CDN_URL % (version, kind)
    req = urllib.request.Request(url, headers={'User-Agent': 'RiftSense/1.0'})
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=60) as r:
        payload = r.read()
    obj = json.loads(payload.decode('utf-8-sig'))
    _validate_catalog(obj, kind)
    directory = os.path.dirname(path) or '.'
    fd, tmp = tempfile.mkstemp(dir=directory, prefix='.dl_%s_' % kind, suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return obj


def ensure_assets():
    latest = _UNSET
    for kind, path in (('champion', CHAMP_FILE), ('item', ITEM_FILE)):
        if _try_catalog(path, kind) is not None:
            continue
        if latest is _UNSET:
            latest = _fetch_latest_version()
        candidates = ([latest] if latest else []) + [DEFAULT_ASSET_VERSION]
        for version in candidates:
            try:
                _download_asset(kind, path, version)
                break
            except Exception:
                continue


def load_champ_map(catalog=None):
    if catalog is None:
        catalog = _try_catalog(CHAMP_FILE, 'champion')
    if not catalog:
        return {}
    return {v['name']: v['id'] for v in catalog['data'].values()
            if isinstance(v, dict) and isinstance(v.get('name'), str) and isinstance(v.get('id'), str)}


def load_item_data(catalog=None):
    if catalog is None:
        catalog = _try_catalog(ITEM_FILE, 'item')
    if not catalog:
        return {}, {}, {}, {}, {}
    data = catalog['data']
    entries = []
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        try:
            iid = int(key)
        except (TypeError, ValueError):
            continue
        maps = value.get('maps') if isinstance(value.get('maps'), dict) else {}
        if maps.get('11') is not True:
            continue
        entries.append((iid, value))
    entries.sort(key=lambda pair: pair[0])
    names = {}
    display = {}
    costs = {}
    purchasable = set()
    for iid, value in entries:
        display[iid] = str(value.get('name') or '')
        gold = value.get('gold') if isinstance(value.get('gold'), dict) else {}
        total = gold.get('total')
        if isinstance(total, bool) or not isinstance(total, (int, float)):
            total = 0
        costs[iid] = int(total)
        if gold.get('purchasable') is True:
            purchasable.add(iid)
            names.setdefault(display[iid].lower(), []).append(iid)
    into = {}
    for iid, value in entries:
        if iid not in purchasable:
            continue
        successors = []
        for raw in value.get('into') or []:
            try:
                successor = int(raw)
            except (TypeError, ValueError):
                continue
            if successor in purchasable:
                successors.append(successor)
        into[iid] = sorted(set(successors))
    legacy = {}
    for iid in sorted(display):
        name = display[iid]
        if iid in purchasable and name not in legacy:
            legacy[name] = iid
    return legacy, names, display, costs, into


def refresh_asset_state():
    global CHAMPS, ITEMS, ITEM_NAMES, ITEM_DISPLAY, ITEM_COSTS, ITEM_INTO
    global PURCHASE_CATALOG
    champ_obj = _try_catalog(CHAMP_FILE, 'champion')
    item_obj = _try_catalog(ITEM_FILE, 'item')
    if champ_obj:
        CHAMPS = load_champ_map(champ_obj)
        ASSETS['championVersion'] = champ_obj.get('version')
        ASSETS['championCount'] = len(champ_obj['data'])
    else:
        CHAMPS = {}
        ASSETS['championVersion'] = None
        ASSETS['championCount'] = 0
    if item_obj:
        ASSETS['itemVersion'] = item_obj.get('version')
        ASSETS['itemCount'] = len(item_obj['data'])
    else:
        ASSETS['itemVersion'] = None
        ASSETS['itemCount'] = 0
    ITEMS, ITEM_NAMES, ITEM_DISPLAY, ITEM_COSTS, ITEM_INTO = load_item_data(item_obj)
    try:
        PURCHASE_CATALOG = purchase.load_catalog(item_obj) if item_obj else {}
    except Exception:
        PURCHASE_CATALOG = {}
    champ_version = ASSETS['championVersion']
    item_version = ASSETS['itemVersion']
    ASSETS['mismatch'] = bool(champ_version and item_version and champ_version != item_version)
    ASSETS['version'] = champ_version or item_version


def version_info():
    return {
        'version': ASSETS.get('version'),
        'championVersion': ASSETS.get('championVersion'),
        'itemVersion': ASSETS.get('itemVersion'),
        'championCount': ASSETS.get('championCount', 0),
        'itemCount': ASSETS.get('itemCount', 0),
        'mismatch': bool(ASSETS.get('mismatch')),
    }


def db_health():
    path = DB_PATH
    try:
        con = _db_connect()
    except sqlite3.Error as ex:
        return {'ok': False, 'status': 'db_error', 'error': str(ex), 'path': path}
    try:
        columns = {row[1] for row in con.execute('PRAGMA table_info(session)')}
        if not columns:
            return {'ok': False, 'status': 'schema_error', 'error': 'session table not found',
                    'path': path}
        required = {'agent', 'time_created', 'time_updated', 'cost', 'tokens_input'}
        missing = sorted(required - columns)
        if missing:
            return {'ok': False, 'status': 'schema_error',
                    'error': 'missing session columns: %s' % ','.join(missing), 'path': path}
        return {'ok': True, 'status': 'ok', 'path': path}
    except sqlite3.Error as ex:
        return {'ok': False, 'status': 'db_error', 'error': str(ex), 'path': path}
    finally:
        con.close()


def stored_session_id():
    stored = _read_stored_epoch()
    session = stored.get('session')
    return session if isinstance(session, str) and session else None


def lcu_health(seed=True):
    if seed and _LCU_STATE.get('checkedAt') is None:
        fetch_game()
    now = time.time()
    with _LCU_LOCK:
        state = dict(_LCU_STATE)
    last = _num(state.get('lastSnapshotAt'))
    return {
        'reachable': state.get('reachable'),
        'status': state.get('status') or 'unknown',
        'lastSnapshotAgeSec': round(max(0.0, now - last), 3) if last is not None else None,
        'lastSnapshotAt': last,
        'checkedAt': state.get('checkedAt'),
        'error': state.get('error'),
    }


def build_health():
    db = db_health()
    lcu = lcu_health()
    now = time.time()
    champ_count = int(ASSETS.get('championCount') or 0)
    item_count = int(ASSETS.get('itemCount') or 0)
    problems = []
    if not (champ_count and item_count):
        problems.append('assets')
    if not db.get('ok'):
        problems.append('db')
    return {
        'ok': not problems,
        'status': 'ok' if not problems else 'degraded',
        'problems': problems,
        'version': ASSETS.get('version'),
        'assetVersion': ASSETS.get('version'),
        'championVersion': ASSETS.get('championVersion'),
        'itemVersion': ASSETS.get('itemVersion'),
        'championCount': champ_count,
        'itemCount': item_count,
        'patchMismatch': bool(ASSETS.get('mismatch')),
        'sessionId': stored_session_id(),
        'uptimeSec': round(max(0.0, now - STARTED_AT), 3),
        'startedAt': STARTED_AT,
        'time': now,
        'db': db,
        'lcu': lcu,
    }


def file_age(path):
    try:
        return max(0.0, time.time() - os.stat(path).st_mtime)
    except OSError:
        return None


def chain_of(iid):
    if PURCHASE_CATALOG:
        return purchase.chain_of(PURCHASE_CATALOG, iid)
    seen = set()
    stack = [iid]
    while stack:
        x = stack.pop()
        if x in seen or x not in ITEM_DISPLAY:
            continue
        seen.add(x)
        for y in ITEM_INTO.get(x, ()):
            stack.append(y)
    return sorted(seen)


def find_item_id(name):
    if not name or len(name) > 60:
        return None
    return (ITEM_NAMES.get(name.strip().lower()) or [None])[0]


def _record_lcu(status, reachable, error=None, snapshot=False):
    now = time.time()
    with _LCU_LOCK:
        _LCU_STATE['status'] = status
        _LCU_STATE['reachable'] = reachable
        _LCU_STATE['error'] = error
        _LCU_STATE['checkedAt'] = now
        if snapshot:
            _LCU_STATE['lastSnapshotAt'] = now


def fetch_game():
    try:
        req = urllib.request.Request('https://127.0.0.1:2999/liveclientdata/allgamedata')
        with urllib.request.urlopen(req, context=SSL_CTX, timeout=3) as r:
            raw = r.read()
        try:
            data = json.loads(raw.decode('utf-8'))
        except Exception:
            _record_lcu('malformed_response', True, 'malformed_response')
            return None, 'malformed_response'
        _record_lcu('live', True, None, snapshot=True)
        return data, None
    except urllib.error.HTTPError as ex:
        if ex.code == 404:
            _record_lcu('no_game', True, None)
            return None, 'no_active_game'
        status = 'http_%s' % ex.code
        _record_lcu(status, True, status)
        return None, status
    except urllib.error.URLError as ex:
        reason = getattr(ex, 'reason', None)
        if isinstance(reason, (ConnectionRefusedError, ConnectionResetError)):
            status = 'client_unreachable'
        elif isinstance(reason, TimeoutError):
            status = 'timeout'
        else:
            text = str(reason or ex).lower()
            if 'refused' in text or 'unreachable' in text:
                status = 'client_unreachable'
            elif 'timed out' in text or 'timeout' in text:
                status = 'timeout'
            else:
                status = 'unreachable'
        _record_lcu(status, False, status)
        return None, status
    except Exception as ex:
        _record_lcu('error', False, type(ex).__name__)
        return None, 'error'


def game_status(err):
    if err is None:
        return 'live'
    if err in NO_GAME_REASONS:
        return 'no_game'
    return 'api_error'


def pstat(p, name):
    if p.get(name) is not None:
        return _num(p[name])
    scores = p.get('scores')
    if isinstance(scores, dict):
        return _num(scores.get(name))
    return None


def _player_label(p):
    if not isinstance(p, dict):
        return ''
    return p.get('riotId') or p.get('riotIdGameName') or p.get('summonerName') or ''


def resolve_identity(ap, players):
    active = ap if isinstance(ap, dict) else {}
    tiers = []
    full = str(active.get('riotId') or '').strip().lower()
    if full:
        tiers.append(('riotId', lambda p, f=full: str(p.get('riotId') or '').strip().lower() == f))
    game_name = str(active.get('riotIdGameName') or '').strip().lower()
    tag_line = str(active.get('riotIdTagLine') or '').strip().lower()
    if game_name and tag_line:
        tiers.append(('riotIdGameName+riotIdTagLine',
                      lambda p, g=game_name, t=tag_line:
                      str(p.get('riotIdGameName') or '').strip().lower() == g
                      and str(p.get('riotIdTagLine') or '').strip().lower() == t))
    summoner = str(active.get('summonerName') or '').strip().lower()
    if summoner:
        tiers.append(('summonerName',
                      lambda p, s=summoner: str(p.get('summonerName') or '').strip().lower() == s))
    for method, matches_test in tiers:
        matches = [p for p in players if isinstance(p, dict) and matches_test(p)]
        if len(matches) == 1:
            return matches[0], {'status': 'resolved', 'method': method, 'candidates': []}
        if len(matches) > 1:
            return None, {'status': 'ambiguous', 'method': method,
                          'candidates': [_player_label(p) for p in matches]}
    return None, {'status': 'unresolved', 'method': None, 'candidates': []}


def player_obj(p, mine, ap):
    items = None
    value = None
    raw_items = p.get('items')
    if isinstance(raw_items, list):
        items = []
        value = 0
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            iid = raw.get('itemID')
            if isinstance(iid, bool) or not isinstance(iid, int) or iid <= 0:
                continue
            qty = raw.get('count')
            if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
                qty = 1
            cost = ITEM_COSTS.get(iid)
            entry = {'id': iid, 'n': raw.get('displayName') or ITEM_DISPLAY.get(iid, ''), 'q': qty}
            if cost is not None:
                entry['cost'] = cost * qty
                value += cost * qty
            items.append(entry)
    spells = []
    ss = p.get('summonerSpells')
    if isinstance(ss, dict):
        for slot in ('summonerSpellOne', 'summonerSpellTwo'):
            slot_value = ss.get(slot)
            if isinstance(slot_value, dict) and slot_value.get('displayName'):
                spells.append(slot_value['displayName'])
    champ = p.get('championName')
    obj = {
        'champ': champ,
        'key': CHAMPS.get(champ, (champ or '').replace(' ', '')),
        'name': _player_label(p),
        'pos': p.get('position') or '',
        'level': _num(p.get('level')),
        'k': pstat(p, 'kills'),
        'd': pstat(p, 'deaths'),
        'a': pstat(p, 'assists'),
        'cs': pstat(p, 'creepScore'),
        'items': items,
        'value': value,
        'valueKind': 'inventory_catalog_value',
        'spells': spells,
        'team': p.get('team'),
        'me': mine,
    }
    if mine:
        obj['gold'] = _num(ap.get('currentGold'))
        obj['goldKind'] = 'available_gold'
        abilities = ap.get('abilities')
        levels = {}
        if isinstance(abilities, dict) and 'Q' in abilities:
            for key in ('Q', 'W', 'E', 'R'):
                levels[key] = _num((abilities.get(key) or {}).get('abilityLevel'))
        elif isinstance(abilities, list):
            for entry in abilities:
                if isinstance(entry, dict):
                    levels[str(entry.get('id', '?'))[:1]] = _num(entry.get('abilityLevel'))
        obj['abil'] = levels
        runes = ap.get('fullRunes') or {}
        obj['keystone'] = (runes.get('keystone') or {}).get('displayName')
        obj['tree1'] = (runes.get('primaryRuneTree') or {}).get('displayName')
        obj['tree2'] = (runes.get('secondaryRuneTree') or {}).get('displayName')
    return obj


def build_state():
    data, err = fetch_game()
    status = 'live' if (err is None and isinstance(data, dict) and isinstance(data.get('gameData'), dict)) else game_status(err)
    fetched_at = time.time()
    if status != 'live':
        body = {
            'status': status,
            'inGame': False,
            'time': None,
            'myTeam': [],
            'enemyTeam': [],
            'identity': {'status': 'unknown', 'method': None, 'candidates': []},
            'fetchedAt': fetched_at,
            'snapshotAgeSec': None,
            'assetVersion': ASSETS.get('version'),
        }
        if status == 'api_error':
            body['error'] = err
        else:
            body['reason'] = err or 'no_active_game'
        return body

    g = data['gameData']
    ap = data.get('activePlayer')
    ap = ap if isinstance(ap, dict) else {}
    players = data.get('allPlayers')
    players = players if isinstance(players, list) else []

    me, identity = resolve_identity(ap, players)

    every_player = []
    my_team = []
    enemy_team = []
    my_team_key = me.get('team') if me is not None else None
    for p in players:
        if not isinstance(p, dict):
            continue
        if me is None:
            mine = None
        else:
            mine = p is me
        obj = player_obj(p, mine, ap)
        every_player.append(obj)
        if me is not None and my_team_key is not None and p.get('team') == my_team_key:
            my_team.append(obj)
        elif me is not None and my_team_key is not None:
            enemy_team.append(obj)

    events = []
    dragon_kills = []
    baron_kills = []
    raw_events = data.get('events')
    raw_events = raw_events.get('Events') if isinstance(raw_events, dict) else None
    if isinstance(raw_events, list):
        for e in raw_events:
            if not isinstance(e, dict):
                continue
            event_time = _num(e.get('EventTime'))
            if event_time is None:
                continue
            events.append({
                't': event_time,
                'n': e.get('EventName'),
                'dragon': e.get('DragonType'),
                'killer': e.get('KillerName'),
                'victim': e.get('VictimName'),
                'turret': e.get('TurretKilled'),
                'monster': e.get('MonsterType'),
            })
            if e.get('EventName') == 'DragonKill':
                dragon_kills.append({'t': event_time, 'type': e.get('DragonType')})
            elif e.get('EventName') == 'BaronKill':
                baron_kills.append({'t': event_time})
    events.sort(key=lambda x: x['t'])

    ctx = game_context((data, None))
    return {
        'status': 'live',
        'inGame': True,
        'time': _num(g.get('gameTime')),
        'mode': g.get('gameMode'),
        'map': g.get('mapNumber'),
        'sessionId': ctx.get('session'),
        'identity': identity,
        'fetchedAt': fetched_at,
        'snapshotAgeSec': round(max(0.0, time.time() - fetched_at), 3),
        'assetVersion': ASSETS.get('version'),
        'myTeam': my_team,
        'enemyTeam': enemy_team,
        'players': every_player,
        'events': events[-10:],
        'dragonKills': dragon_kills,
        'baronKills': baron_kills,
    }


_EPOCH_THREAD_LOCK = threading.Lock()
_EPOCH_LOCK_PATH = os.path.join(tempfile.gettempdir(), 'riftsense_game_epoch.lock')


def _acquire_file_lock(handle):
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return True
    if msvcrt is not None:
        deadline = time.time() + 5
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                if time.time() >= deadline:
                    return False
                time.sleep(0.05)
    return True


def _release_file_lock(handle):
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


@contextlib.contextmanager
def epoch_lock():
    with _EPOCH_THREAD_LOCK:
        handle = None
        locked = False
        try:
            try:
                handle = open(_EPOCH_LOCK_PATH, 'a+')
                locked = _acquire_file_lock(handle)
            except OSError:
                handle = None
                locked = False
            yield
        finally:
            if handle is not None:
                if locked:
                    _release_file_lock(handle)
                handle.close()


def _atomic_write_json(path, obj):
    directory = os.path.dirname(path) or '.'
    fd, tmp = tempfile.mkstemp(dir=directory, prefix='.tmp_epoch_', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(obj, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _read_stored_epoch():
    try:
        stored = _read_json(EPOCH_FILE)
        return stored if isinstance(stored, dict) else {}
    except Exception:
        return {}


def game_context(fetched=_UNSET):
    if fetched is _UNSET:
        data, err = fetch_game()
    else:
        data, err = fetched
    with epoch_lock():
        stored = _read_stored_epoch()
        now = int(time.time() * 1000)
        if isinstance(data, dict) and isinstance(data.get('gameData'), dict):
            g = data['gameData']
            game_time = _num(g.get('gameTime'))
            ap = data.get('activePlayer')
            ap = ap if isinstance(ap, dict) else {}
            players = data.get('allPlayers')
            players = players if isinstance(players, list) else []
            me, _identity = resolve_identity(ap, players)
            champ = me.get('championName') if me is not None else None
            if game_time is not None and 0 <= game_time < GAME_TIME_MAX:
                start = now - int(game_time * 1000)
                old_start = _num(stored.get('start'))
                same_session = (
                    old_start is not None
                    and abs(old_start - start) <= EPOCH_DRIFT_MS
                    and (not champ or not stored.get('champ') or stored.get('champ') == champ)
                )
                if same_session:
                    rec = dict(stored)
                    rec['start'] = int(old_start)
                    rec['champ'] = champ or stored.get('champ')
                else:
                    rec = {'start': start, 'champ': champ, 'session': str(uuid.uuid4())}
                rec.update({'observedAt': now, 'gameTime': game_time, 'end': now, 'source': 'live'})
                needs_write = (
                    not stored.get('start')
                    or stored.get('session') != rec.get('session')
                    or stored.get('start') != rec.get('start')
                    or stored.get('champ') != rec.get('champ')
                    or abs(now - int(_num(stored.get('observedAt')) or 0)) >= 5000
                )
                if needs_write:
                    try:
                        _atomic_write_json(EPOCH_FILE, rec)
                    except OSError:
                        pass
                return rec
        if stored.get('start'):
            rec = dict(stored)
            rec['source'] = 'stored'
            rec['error'] = err
            return rec
        source = game_status(err) if err else 'no_game'
        return {
            'start': None,
            'champ': None,
            'session': None,
            'source': source,
            'error': err,
        }


def current_champion():
    return game_context().get('champ')


def build_plan():
    try:
        with open(BUILD_FILE, 'r', encoding='utf-8-sig') as f:
            lines = f.read().splitlines()
    except OSError as ex:
        return {'items': [], 'champ': None, 'status': 'read_error', 'error': str(ex),
                'fetchedAt': time.time(), 'age': None, 'assetVersion': ASSETS.get('version')}
    ctx = game_context()
    champ = ctx.get('champ')
    plan_line = None
    default_line = None
    for raw in lines:
        s = raw.strip()
        m = re.match(r'^PLAN\[([^\]]+)\]\s*:\s*(.+)$', s)
        if m:
            name = m.group(1).strip()
            if champ and name.lower() == champ.lower():
                plan_line = m.group(2)
            elif name.lower() == 'default':
                default_line = m.group(2)
        elif s.upper().startswith('PLAN:') and plan_line is None and default_line is None:
            default_line = s.split(':', 1)[1]
    if plan_line is None:
        plan_line = default_line
    items = []
    if plan_line:
        for part in plan_line.split('->'):
            name = part.strip()
            if not name:
                continue
            iid = find_item_id(name)
            entry = {'name': name, 'id': iid}
            if iid:
                entry['chain'] = chain_of(iid)
                candidates = ITEM_NAMES.get(name.lower(), [])
                if len(candidates) > 1:
                    entry['idCandidates'] = candidates
            items.append(entry)
    if items and not any(it.get('id') for it in items):
        items = []
    return {
        'items': items,
        'champ': champ,
        'status': 'ok',
        'gameSource': ctx.get('source'),
        'sessionId': ctx.get('session'),
        'fetchedAt': time.time(),
        'age': file_age(BUILD_FILE),
        'assetVersion': ASSETS.get('version'),
    }


def _empty_slots():
    return {'total': purchase.SLOT_COUNT, 'used': 0, 'free': purchase.SLOT_COUNT, 'ok': True}


def build_purchase():
    state = build_state()
    plan = build_plan()
    live = state.get('status') == 'live'
    payload = {
        'ok': False,
        'status': 'no_game',
        'champ': plan.get('champ'),
        'gold': None,
        'owned': {},
        'plan': plan.get('items') or [],
        'next': None,
        'alternatives': [],
        'slots': _empty_slots(),
        'reasons': [],
        'sessionId': plan.get('sessionId') or state.get('sessionId'),
        'assetVersion': ASSETS.get('version'),
    }
    if not live:
        status = state.get('status') or 'no_game'
        payload['status'] = 'api_error' if status == 'api_error' else 'no_game'
        payload['reasons'] = [state.get('reason') or state.get('error') or 'no active game']
        return payload

    me = None
    for player in state.get('players') or []:
        if isinstance(player, dict) and player.get('me'):
            me = player
            break
    if me is None:
        payload['status'] = 'no_identity'
        payload['reasons'] = ['could not identify active player inventory']
        return payload

    owned = purchase.inventory_summary(me.get('items') or [])
    gold = _num(me.get('gold'))
    if not PURCHASE_CATALOG:
        payload['owned'] = owned
        payload['gold'] = gold
        payload['status'] = 'no_catalog'
        payload['reasons'] = ['item catalog unavailable']
        return payload

    result = purchase.next_purchase(owned, plan.get('items') or [], gold, PURCHASE_CATALOG)
    status = result.get('status') or 'ok'
    payload.update({
        'ok': status in ('ok', 'plan_complete'),
        'status': status,
        'owned': owned,
        'gold': gold,
        'next': result.get('next'),
        'alternatives': result.get('alternatives') or [],
        'slots': result.get('slots') or payload['slots'],
        'reasons': result.get('reasons') or [],
    })
    return payload


def _db_connect():
    con = sqlite3.connect('file:%s?mode=ro' % DB_PATH.replace('\\', '/'), uri=True, timeout=2)
    con.row_factory = None
    return con


def build_cost():
    ctx = game_context()
    start = _num(ctx.get('start'))
    if not start:
        return {
            'ok': False,
            'status': ctx.get('source') or 'no_game',
            'error': ctx.get('error'),
            'cost': 0,
            'lastTick': 0,
            'ticks': 0,
            'exact': False,
            'estimate': True,
            'sessionId': ctx.get('session'),
            'window': None,
        }
    start = int(start)
    end = int(_num(ctx.get('end')) or (time.time() * 1000))
    if end < start:
        end = start
    try:
        con = _db_connect()
    except sqlite3.Error as ex:
        return {'ok': False, 'status': 'db_error', 'error': str(ex), 'cost': 0, 'lastTick': 0,
                'ticks': 0, 'exact': False, 'estimate': True, 'sessionId': ctx.get('session'),
                'window': {'start': start, 'end': end}}
    try:
        cur = con.cursor()
        columns = {row[1] for row in cur.execute('PRAGMA table_info(session)')}
        if not columns:
            return {'ok': False, 'status': 'schema_error', 'error': 'session table not found',
                    'cost': 0, 'lastTick': 0, 'ticks': 0, 'exact': False, 'estimate': True,
                    'sessionId': ctx.get('session'), 'window': {'start': start, 'end': end}}
        required = {'agent', 'time_created', 'time_updated', 'cost', 'tokens_input'}
        missing = sorted(required - columns)
        if missing:
            return {'ok': False, 'status': 'schema_error',
                    'error': 'missing session columns: %s' % ','.join(missing),
                    'cost': 0, 'lastTick': 0, 'ticks': 0, 'exact': False, 'estimate': True,
                    'sessionId': ctx.get('session'), 'window': {'start': start, 'end': end}}
        base = "agent='lol-coach' AND tokens_input > 0 AND time_created >= ? AND time_created <= ?"
        base_params = [start, end]
        where = base
        params = list(base_params)
        directory = os.path.abspath(ROOT)
        directory_used = None
        directory_degraded = False
        if 'directory' in columns:
            directory_where = base + " AND directory COLLATE NOCASE IN (?, ?)"
            directory_params = base_params + [directory, directory.replace('\\', '/')]
            cur.execute('SELECT COUNT(*), COALESCE(SUM(cost),0) FROM session WHERE ' + directory_where,
                        directory_params)
            filtered_ticks = cur.fetchone()[0]
            if filtered_ticks:
                where = directory_where
                params = directory_params
                directory_used = directory
            else:
                directory_degraded = True
        cur.execute('SELECT COUNT(*), COALESCE(SUM(cost),0) FROM session WHERE ' + where, params)
        ticks, cost = cur.fetchone()
        cur.execute('SELECT cost FROM session WHERE ' + where +
                    ' ORDER BY time_updated DESC LIMIT 1', params)
        row = cur.fetchone()
        last = row[0] if row else 0
        return {
            'ok': True,
            'status': 'ok',
            'cost': cost or 0,
            'lastTick': last or 0,
            'ticks': ticks,
            'exact': False,
            'estimate': True,
            'sessionId': ctx.get('session'),
            'window': {'start': start, 'end': end},
            'directory': directory_used,
            'directoryFilterDegraded': directory_degraded,
            'source': ctx.get('source'),
        }
    except sqlite3.OperationalError as ex:
        text = str(ex).lower()
        status = 'schema_error' if ('no such table' in text or 'no such column' in text) else 'db_error'
        return {'ok': False, 'status': status, 'error': str(ex), 'cost': 0, 'lastTick': 0,
                'ticks': 0, 'exact': False, 'estimate': True, 'sessionId': ctx.get('session'),
                'window': {'start': start, 'end': end}}
    except sqlite3.Error as ex:
        return {'ok': False, 'status': 'db_error', 'error': str(ex), 'cost': 0, 'lastTick': 0,
                'ticks': 0, 'exact': False, 'estimate': True, 'sessionId': ctx.get('session'),
                'window': {'start': start, 'end': end}}
    finally:
        con.close()


def read_text_file(path):
    last_text = ''
    for _attempt in range(3):
        try:
            st1 = os.stat(path)
            with open(path, 'r', encoding='utf-8-sig', errors='replace') as f:
                text = f.read()
            st2 = os.stat(path)
        except FileNotFoundError:
            return '', None, 'missing'
        except OSError:
            return '', None, 'error'
        if st1.st_mtime_ns == st2.st_mtime_ns and st1.st_size == st2.st_size:
            return text.strip(), max(0.0, time.time() - st2.st_mtime), 'ok'
        last_text = text
        time.sleep(0.05)
    return last_text.strip(), None, 'unstable'


def _death_text(value, limit):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ''
    return str(value).strip()[:limit]


def read_death_structured(now=None):
    now = time.time() if now is None else now
    try:
        st = os.stat(DEATH_META_FILE)
    except OSError:
        return None
    if st.st_size <= 0 or st.st_size > DEATH_STRUCTURED_MAX_BYTES:
        return None
    if now - st.st_mtime > DEATH_STRUCTURED_TTL:
        return None
    try:
        with open(DEATH_META_FILE, 'r', encoding='utf-8-sig', errors='replace') as f:
            obj = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get('schema') != 'riftsense.v1' or obj.get('kind') != 'death':
        return None
    if obj.get('status') not in DEATH_STRUCTURED_STATUS:
        return None
    raw_facts = obj.get('facts')
    raw_hypotheses = obj.get('hypotheses')
    if not isinstance(raw_facts, list) or not isinstance(raw_hypotheses, list):
        return None

    def _lines(values):
        out = []
        for value in values:
            text = _death_text(value, 500)
            if text:
                out.append(text)
            if len(out) >= DEATH_STRUCTURED_MAX_ITEMS:
                break
        return out

    facts = _lines(raw_facts)
    hypotheses = _lines(raw_hypotheses)
    now_text = _death_text(obj.get('now'), 800)
    next_text = _death_text(obj.get('next'), 800)
    do_now = _death_text(obj.get('doNow'), 800)
    if not (facts or hypotheses or now_text or next_text or do_now):
        return None
    session_id = obj.get('sessionId') or obj.get('session')
    if not isinstance(session_id, str):
        session_id = None
    seq_value = obj.get('seq')
    if isinstance(seq_value, bool) or not isinstance(seq_value, int):
        seq_value = None
    return {
        'schema': 'riftsense.v1',
        'sessionId': session_id,
        'seq': seq_value,
        'observedAt': _death_text(obj.get('observedAt') or obj.get('completedAt'), 64),
        'gameClock': _death_text(obj.get('gameClock'), 32),
        'killer': _death_text(obj.get('killer'), 120),
        'killedByChampion': obj.get('killedByChampion') is True,
        'died': _death_text(obj.get('died'), 200),
        'facts': facts,
        'hypotheses': hypotheses,
        'now': now_text,
        'next': next_text,
        'doNow': do_now,
        'raw': _death_text(obj.get('raw'), 20000),
    }


def ingest_event(payload):
    kind = str(payload.get('kind') or '').strip()
    session_id = payload.get('sessionId') or payload.get('session_id')
    t_game = payload.get('tGame', payload.get('gameTime'))
    if kind == 'advice':
        text = str(payload.get('text') or '').strip()
        if not text:
            raise ValueError('advice text required')
        return timeline.record_advice(payload.get('adviceKind') or 'coach', text,
                                      t_game=t_game, session_id=session_id,
                                      meta=payload.get('meta'))
    if kind == 'inference':
        return timeline.record_inference(payload.get('requestId') or '',
                                         status=payload.get('status') or 'started',
                                         t_game=t_game, session_id=session_id,
                                         started_at=payload.get('startedAt'),
                                         finished_at=payload.get('finishedAt'),
                                         tokens_in=payload.get('tokensIn') or 0,
                                         tokens_out=payload.get('tokensOut') or 0,
                                         cost=payload.get('cost') or 0.0)
    if not kind:
        raise ValueError('kind required')
    return timeline.record_event(kind, label=payload.get('label'), payload=payload.get('data'),
                                 t_game=t_game, session_id=session_id,
                                 champ=payload.get('champ'), mode=payload.get('mode'),
                                 map_number=payload.get('map'))


def build_timeline(limit=50):
    try:
        return timeline.recent(limit)
    except Exception as ex:
        return {'ok': False, 'status': 'timeline_error',
                'error': type(ex).__name__, 'message': str(ex)}


def build_timeline_current():
    try:
        return timeline.current()
    except Exception as ex:
        return {'ok': False, 'status': 'timeline_error',
                'error': type(ex).__name__, 'message': str(ex)}


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, payload, ctype='application/json; charset=utf-8', code=200):
        if not isinstance(payload, (bytes, bytearray)):
            if isinstance(payload, str):
                payload = payload.encode('utf-8')
            else:
                payload = json.dumps(payload).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        try:
            self._route()
        except Exception as ex:
            try:
                self._send({'status': 'error', 'error': type(ex).__name__, 'message': str(ex)}, code=500)
            except Exception:
                pass

    def _route(self):
        path = self.path.split('?', 1)[0].rstrip('/') or '/'
        if path in ('/', '/index.html'):
            try:
                with open(os.path.join(BASE, 'index.html'), 'rb') as f:
                    self._send(f.read(), 'text/html; charset=utf-8')
            except Exception as ex:
                self.send_error(500, str(ex))
        elif path in ('/overlay', '/overlay.html'):
            try:
                with open(os.path.join(BASE, 'overlay.html'), 'rb') as f:
                    self._send(f.read(), 'text/html; charset=utf-8')
            except Exception as ex:
                self.send_error(500, str(ex))
        elif path == '/api/version':
            self._send(version_info())
        elif path == '/api/health':
            self._send(build_health())
        elif path == '/api/game':
            state = build_state()
            self._send(state, code=503 if state.get('status') == 'api_error' else 200)
        elif path == '/api/coach':
            text, age, status = read_text_file(COACH_FILE)
            self._send({'text': text, 'age': age, 'status': status},
                       code=500 if status == 'error' else 200)
        elif path == '/api/death':
            text, age, status = read_text_file(DEATH_FILE)
            try:
                structured = read_death_structured()
            except Exception:
                structured = None
            self._send({'text': text, 'age': age, 'status': status, 'structured': structured},
                       code=500 if status == 'error' else 200)
        elif path == '/api/cost':
            cost = build_cost()
            self._send(cost, code=503 if cost.get('status') in ('db_error', 'schema_error') else 200)
        elif path == '/api/plan':
            plan = build_plan()
            self._send(plan, code=500 if plan.get('status') == 'read_error' else 200)
        elif path == '/api/purchase':
            self._send(build_purchase())
        elif path == '/api/timeline':
            limit = 50
            if '?' in self.path:
                query = urllib.parse.parse_qs(self.path.split('?', 1)[1])
                try:
                    limit = int((query.get('limit') or ['50'])[0])
                except (TypeError, ValueError):
                    limit = 50
            self._send(build_timeline(limit))
        elif path == '/api/timeline/current':
            self._send(build_timeline_current())
        elif path == '/api/highlight':
            names = [n for n in sorted(ITEMS.keys(), key=len, reverse=True)
                     if len(n) >= 4 and n not in ('Ward', 'Wards')]
            self._send({'items': names})
        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.split('?', 1)[0].rstrip('/')
        try:
            if path != '/api/events':
                self._send({'ok': False, 'error': 'not_found'}, code=404)
                return
            length = int(self.headers.get('Content-Length') or 0)
            if length <= 0 or length > 65536:
                raise ValueError('invalid body length')
            payload = json.loads(self.rfile.read(length).decode('utf-8'))
            if not isinstance(payload, dict):
                raise ValueError('body must be a JSON object')
            self._send(ingest_event(payload))
        except Exception as ex:
            self._send({'ok': False, 'error': type(ex).__name__, 'message': str(ex)}, code=400)

    def log_message(self, fmt, *args):
        pass


refresh_asset_state()


def main():
    os.chdir(BASE)
    ensure_assets()
    refresh_asset_state()
    with http.server.ThreadingHTTPServer(('127.0.0.1', PORT), Handler) as httpd:
        print('LoL Coach UI running at http://127.0.0.1:%d' % PORT)
        print('Keep this window open. Close it to stop the UI server.')
        httpd.serve_forever()


if __name__ == '__main__':
    main()
