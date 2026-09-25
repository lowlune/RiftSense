import sqlite3
import json
import os
import time

DB = os.path.join(os.path.expanduser('~'), '.local', 'share', 'opencode', 'opencode.db')
con = sqlite3.connect('file:' + DB.replace('\\', '/') + '?mode=ro', uri=True)
cur = con.cursor()

cur.execute("SELECT id, time_created, time_updated, cost FROM session WHERE agent='lol-coach' ORDER BY time_updated DESC LIMIT 3")
sessions = cur.fetchall()
now_ms = int(time.time() * 1000)


def assistant_info(data):
    try:
        info = json.loads(data)
    except (TypeError, ValueError):
        return None
    if not isinstance(info, dict) or info.get('role') != 'assistant':
        return None
    return info


def format_duration(info, now_ms):
    timing = info.get('time') or {}
    created = timing.get('created')
    completed = timing.get('completed')
    if completed is None:
        if created is None:
            return 'running'
        return 'running (%.0fs)' % max(0.0, (now_ms - created) / 1000.0)
    if created is None:
        return 'unknown'
    return '%.1fs' % ((completed - created) / 1000.0)


for sid, tc, tu, cost in sessions:
    print('SESSION', sid, 'updated', round((now_ms - tu) / 1000), 's ago | cost %.5f' % (cost or 0))
    cur.execute(
        "SELECT id, time_created, time_updated, data FROM message WHERE session_id=? ORDER BY time_updated DESC LIMIT 30",
        (sid,))
    shown = 0
    for mid, mc, mu, data in cur.fetchall():
        info = assistant_info(data)
        if info is None:
            continue
        print('  msg', mid, 'dur %s' % format_duration(info, now_ms))
        cur.execute("SELECT data FROM part WHERE message_id=? ORDER BY time_updated", (mid,))
        for (pd,) in cur.fetchall():
            try:
                obj = json.loads(pd)
            except (TypeError, ValueError):
                continue
            if obj.get('type') == 'text' and obj.get('text'):
                bad = []
                for c in obj['text']:
                    if ord(c) > 127:
                        bad.append('U+%04X' % ord(c))
                print('    text non-ascii:', ' '.join(sorted(set(bad))) if bad else 'none')
                for line in obj['text'].split('\n')[:6]:
                    print('    |', line[:130])
                break
        shown += 1
        if shown >= 3:
            break
    print()
