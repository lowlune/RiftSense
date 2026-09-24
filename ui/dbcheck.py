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
for sid, tc, tu, cost in sessions:
    print('SESSION', sid, 'updated', round((now_ms - tu) / 1000), 's ago | cost %.5f' % (cost or 0))
    cur.execute("SELECT id, time_created, time_updated, data FROM message WHERE session_id=? AND data LIKE '%\"role\":\"assistant\"%' ORDER BY time_updated DESC LIMIT 3", (sid,))
    for mid, mc, mu, data in cur.fetchall():
        try:
            info = json.loads(data)
            dur = (info.get('time', {}).get('completed', 0) - info.get('time', {}).get('created', 0)) / 1000
        except Exception:
            dur = -1
        print('  msg', mid, 'dur %.1fs' % dur)
        cur.execute("SELECT data FROM part WHERE message_id=? ORDER BY time_updated", (mid,))
        for (pd,) in cur.fetchall():
            try:
                obj = json.loads(pd)
            except Exception:
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
    print()
