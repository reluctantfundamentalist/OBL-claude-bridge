Run the autonomous introspection cycle — select a knowledge gap, research it, ingest findings.

```bash
cd ~/.openclaw/workspace/graph && python3 autonomous_introspection.py
```

Report:
- Gap selected (query, type, prior uncertainty)
- Research status (filled, timeout, error)
- Nodes ingested
- Cluster bridges from sleep
- Dopamine signal (immediate value)

If error, report full traceback. Do not retry.
