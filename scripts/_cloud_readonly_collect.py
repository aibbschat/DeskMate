import sqlite3, json, sys
c = sqlite3.connect(__DB_PATH__)
T = __TABLES__
out = {}
out['stats'] = list(c.execute(
    'SELECT COUNT(*), SUM(sync_uuid IS NULL), COUNT(DISTINCT sync_uuid),'
    ' SUM(sync_hlc IS NULL), SUM(sync_tombstone) FROM profile_items'
).fetchone())
out['uuids'] = [r[0] for r in c.execute(
    'SELECT sync_uuid FROM profile_items ORDER BY sync_uuid')]
out['tables'] = {}
for t in T:
    try:
        ids = [r[0] for r in c.execute('SELECT id FROM ' + t + ' ORDER BY id')]
        st = c.execute(
            'SELECT COUNT(*), SUM(sync_uuid IS NULL),'
            ' COUNT(DISTINCT sync_uuid), SUM(sync_hlc IS NULL) FROM ' + t
        ).fetchone()
        tg = c.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'"
            " AND name LIKE 'trg_' || ? || '_%'", (t,)).fetchone()[0]
        out['tables'][t] = {'ids': ids, 'stats': list(st), 'triggers': tg}
    except Exception:
        out['tables'][t] = None
print(json.dumps(out))
