import http.server
import json
import os
import re
import sqlite3
import ssl
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
CHAMP_FILE = os.path.join(ROOT, 'champion.json')
COACH_FILE = os.path.join(ROOT, 'coach_latest.txt')
DEATH_FILE = os.path.join(ROOT, 'death_latest.txt')
DB_PATH = os.path.join(os.path.expanduser('~'), '.local', 'share', 'opencode', 'opencode.db')
PORT = 7777

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def load_champ_map():
    try:
        with open(CHAMP_FILE, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)['data']
        return {v['name']: v['id'] for v in data.values()}
    except Exception:
        return {}


# champion/item maps are loaded after the asset check below

ITEM_FILE = os.path.join(ROOT, 'items.json')
BUILD_FILE = os.path.join(ROOT, 'build_intent.txt')


def ensure_assets():
    if os.path.exists(CHAMP_FILE) and os.path.exists(ITEM_FILE):
        return
    ver = '16.18.1'
    try:
        with urllib.request.urlopen('https://ddragon.leagueoflegends.com/api/versions.json', timeout=15) as r:
            ver = json.loads(r.read().decode('utf-8'))[0]
    except Exception:
        pass
    for url, path in (
        ('https://ddragon.leagueoflegends.com/cdn/%s/data/en_US/champion.json' % ver, CHAMP_FILE),
        ('https://ddragon.leagueoflegends.com/cdn/%s/data/en_US/item.json' % ver, ITEM_FILE),
    ):
        if not os.path.exists(path):
            try:
                with urllib.request.urlopen(url, timeout=60) as r, open(path, 'wb') as f:
                    f.write(r.read())
            except Exception:
                pass


ensure_assets()
CHAMPS = load_champ_map()


def load_item_data():
    ids = {}
    costs = {}
    into_names = {}
    try:
        with open(ITEM_FILE, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)['data']
        for k, v in data.items():
            name = v['name']
            iid = int(k)
            if name not in ids or iid < ids[name]:
                ids[name] = iid
            if name not in costs:
                costs[name] = int((v.get('gold') or {}).get('total', 0))
            succ = into_names.setdefault(name, set())
            for x in (v.get('into') or []):
                sx = data.get(str(x))
                if sx:
                    succ.add(sx['name'])
        into_ids = {}
        for name, succs in into_names.items():
            if name in ids:
                into_ids[ids[name]] = sorted({ids[s] for s in succs if s in ids})
        return ids, costs, into_ids
    except Exception:
        return {}, {}, {}


ITEMS, ITEM_COSTS, ITEM_INTO = load_item_data()


def chain_of(iid):
    seen = set()
    stack = [iid]
    while stack:
        x = stack.pop()
        if x in seen:
            continue
        seen.add(x)
        for y in ITEM_INTO.get(x, []):
            stack.append(y)
    return sorted(seen)


def find_item_id(name):
    if not name or len(name) > 40:
        return None
    if name in ITEMS:
        return ITEMS[name]
    low = name.lower()
    for n, i in ITEMS.items():
        if n.lower() == low:
            return i
    return None


def current_champion():
    _, champ = game_context()
    return champ


def build_plan():
    lines = []
    try:
        with open(BUILD_FILE, 'r', encoding='utf-8-sig') as f:
            lines = f.read().splitlines()
    except Exception:
        return {'items': [], 'champ': None}
    champ = current_champion()
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
            if name:
                iid = find_item_id(name)
                entry = {'name': name, 'id': iid}
                if iid:
                    entry['chain'] = chain_of(iid)
                items.append(entry)
    if items and not any(it.get('id') for it in items):
        items = []
    return {'items': items, 'champ': champ}


def fetch_game():
    try:
        req = urllib.request.Request('https://127.0.0.1:2999/liveclientdata/allgamedata')
        with urllib.request.urlopen(req, context=ctx, timeout=3) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception:
        return None


def pstat(p, name):
    if name in p and p[name] is not None:
        return p[name]
    s = p.get('scores') or {}
    return s.get(name)


def same_player(p, ap):
    for k in ('riotId', 'riotIdGameName', 'summonerName'):
        if ap.get(k) and p.get(k) and str(ap[k]).lower() == str(p[k]).lower():
            return True
    return False


def player_obj(p, mine, ap):
    items = [{'id': i.get('itemID', 0), 'n': i.get('displayName', '')} for i in (p.get('items') or [])]
    value = 0
    for it in items:
        value += ITEM_COSTS.get(it['n'], 0)
    spells = []
    ss = p.get('summonerSpells') or {}
    for slot in ('summonerSpellOne', 'summonerSpellTwo'):
        if ss.get(slot):
            spells.append(ss[slot].get('displayName', ''))
    name = p.get('riotId') or p.get('riotIdGameName') or p.get('summonerName') or ''
    obj = {
        'champ': p.get('championName'),
        'key': CHAMPS.get(p.get('championName'), (p.get('championName') or '').replace(' ', '')),
        'name': name,
        'pos': p.get('position') or '',
        'level': p.get('level', 0),
        'k': pstat(p, 'kills'), 'd': pstat(p, 'deaths'), 'a': pstat(p, 'assists'),
        'cs': pstat(p, 'creepScore'),
        'items': items,
        'spells': spells,
        'team': p.get('team'),
        'me': mine,
        'value': value,
    }
    if mine:
        obj['gold'] = ap.get('currentGold')
        ab = ap.get('abilities')
        lv = {}
        if isinstance(ab, dict) and 'Q' in ab:
            for k in ('Q', 'W', 'E', 'R'):
                lv[k] = (ab.get(k) or {}).get('abilityLevel', 0)
        elif isinstance(ab, list):
            for x in ab:
                lv[str(x.get('id', '?'))[:1]] = x.get('abilityLevel', 0)
        obj['abil'] = lv
        fr = ap.get('fullRunes') or {}
        obj['keystone'] = (fr.get('keystone') or {}).get('displayName')
        obj['tree1'] = (fr.get('primaryRuneTree') or {}).get('displayName')
        obj['tree2'] = (fr.get('secondaryRuneTree') or {}).get('displayName')
    return obj


def build_state():
    d = fetch_game()
    if not d or 'gameData' not in d:
        return {'inGame': False}
    g = d['gameData']
    ap = d.get('activePlayer') or {}
    players = d.get('allPlayers') or []

    me = None
    for p in players:
        if same_player(p, ap):
            me = p
            break

    my_team = []
    enemy_team = []
    for p in players:
        mine = (p is me)
        o = player_obj(p, mine, ap)
        if me and p.get('team') == me.get('team'):
            my_team.append(o)
        else:
            enemy_team.append(o)
    if me is None:
        my_team = [player_obj(p, False, ap) for p in players if p.get('team') == 'ORDER']
        enemy_team = [player_obj(p, False, ap) for p in players if p.get('team') == 'CHAOS']

    events = []
    dragon_kills = []
    baron_kills = []
    for e in (d.get('events') or {}).get('Events', []):
        if e.get('EventTime') is None:
            continue
        events.append({
            't': e.get('EventTime'),
            'n': e.get('EventName'),
            'dragon': e.get('DragonType'),
            'killer': e.get('KillerName'),
            'victim': e.get('VictimName'),
            'turret': e.get('TurretKilled'),
            'monster': e.get('MonsterType'),
        })
        if e.get('EventName') == 'DragonKill':
            dragon_kills.append({'t': e.get('EventTime'), 'type': e.get('DragonType')})
        elif e.get('EventName') == 'BaronKill':
            baron_kills.append({'t': e.get('EventTime')})
    events.sort(key=lambda x: x['t'])

    return {
        'inGame': True,
        'time': g.get('gameTime', 0),
        'mode': g.get('gameMode', ''),
        'map': g.get('mapNumber'),
        'myTeam': my_team,
        'enemyTeam': enemy_team,
        'events': events[-10:],
        'dragonKills': dragon_kills,
        'baronKills': baron_kills,
    }


EPOCH_FILE = os.path.join(ROOT, 'game_epoch.json')


def game_context():
    stored = {}
    try:
        with open(EPOCH_FILE, 'r', encoding='utf-8-sig') as f:
            stored = json.load(f)
    except Exception:
        stored = {}
    d = fetch_game()
    if d and d.get('gameData'):
        try:
            gt = float(d['gameData'].get('gameTime') or 0)
            start = int(time.time() * 1000 - gt * 1000)
            ap = d.get('activePlayer') or {}
            champ = None
            for p in d.get('allPlayers') or []:
                if same_player(p, ap):
                    champ = p.get('championName')
                    break
            old_start = int(stored.get('start', 0) or 0)
            if not old_start or abs(old_start - start) > 120000 or (champ and stored.get('champ') != champ):
                stored = {'start': start, 'champ': champ}
                try:
                    with open(EPOCH_FILE, 'w', encoding='utf-8') as f:
                        json.dump(stored, f)
                except Exception:
                    pass
            return stored.get('start'), champ
        except Exception:
            pass
    return stored.get('start'), stored.get('champ')


def build_cost():
    epoch, _ = game_context()
    if not epoch:
        return {'ok': False}
    try:
        con = sqlite3.connect('file:%s?mode=ro' % DB_PATH.replace('\\', '/'), uri=True, timeout=2)
    except Exception:
        return {'ok': False}
    try:
        cur = con.cursor()
        cur.execute("SELECT COUNT(*), COALESCE(SUM(cost),0) FROM session "
                    "WHERE agent='lol-coach' AND time_created >= ? AND tokens_input > 0", (epoch,))
        ticks, cost = cur.fetchone()
        cur.execute("SELECT cost FROM session WHERE agent='lol-coach' AND tokens_input > 0 ORDER BY time_updated DESC LIMIT 1")
        row = cur.fetchone()
        last = row[0] if row else 0
        return {
            'ok': True,
            'cost': cost or 0,
            'lastTick': last or 0,
            'ticks': ticks,
        }
    except Exception:
        return {'ok': False}
    finally:
        con.close()


def read_text_file(path):
    text = ''
    age = None
    try:
        with open(path, 'r', encoding='utf-8-sig', errors='replace') as f:
            text = f.read().strip()
        age = time.time() - os.path.getmtime(path)
    except Exception:
        pass
    return text, age


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            try:
                with open(os.path.join(BASE, 'index.html'), 'rb') as f:
                    self._send(f.read(), 'text/html; charset=utf-8')
            except Exception as ex:
                self.send_error(500, str(ex))
        elif self.path.startswith('/api/game'):
            self._send(json.dumps(build_state()).encode('utf-8'), 'application/json')
        elif self.path.startswith('/api/coach'):
            text, age = read_text_file(COACH_FILE)
            self._send(json.dumps({'text': text, 'age': age}).encode('utf-8'), 'application/json')
        elif self.path.startswith('/api/death'):
            text, age = read_text_file(DEATH_FILE)
            self._send(json.dumps({'text': text, 'age': age}).encode('utf-8'), 'application/json')
        elif self.path.startswith('/api/cost'):
            self._send(json.dumps(build_cost()).encode('utf-8'), 'application/json')
        elif self.path.startswith('/api/plan'):
            self._send(json.dumps(build_plan()).encode('utf-8'), 'application/json')
        elif self.path.startswith('/api/highlight'):
            names = [n for n in sorted(ITEMS.keys(), key=len, reverse=True) if len(n) >= 4 and n not in ('Ward', 'Wards')]
            self._send(json.dumps({'items': names}).encode('utf-8'), 'application/json')
        else:
            self.send_error(404)

    def log_message(self, fmt, *args):
        pass


if __name__ == '__main__':
    os.chdir(BASE)
    with http.server.ThreadingHTTPServer(('127.0.0.1', PORT), Handler) as httpd:
        print('LoL Coach UI running at http://127.0.0.1:%d' % PORT)
        print('Keep this window open. Close it to stop the UI server.')
        httpd.serve_forever()
