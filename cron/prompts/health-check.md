Check brain database health — verify both brain.db and onyx_brain.db are accessible and have reasonable stats.

```bash
cd ~/.openclaw/workspace/graph && python3 -c "
import sqlite3, os

for db_name in ['brain.db', 'onyx_brain.db']:
    if not os.path.exists(db_name):
        print(f'{db_name}: NOT FOUND')
        continue
    db = sqlite3.connect(db_name)
    nodes = db.execute('SELECT COUNT(*) FROM nodes').fetchone()[0]
    edges = db.execute('SELECT COUNT(*) FROM edges').fetchone()[0]
    size_mb = os.path.getsize(db_name) / (1024*1024)
    print(f'{db_name}: {nodes} nodes, {edges} edges, {size_mb:.1f}MB')
    db.close()
"
```

If both databases return stats: report HEALTH_CHECK_OK with the numbers.
If any error: report HEALTH_CHECK_FAILED with full error details.
