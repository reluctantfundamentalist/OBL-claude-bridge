Run the Onyx brain sleep cycle — dedup, cross-link, fitness scoring, and garbage collection.

```bash
cd ~/.openclaw/workspace/graph && python3 onyx_sleep.py
```

Report:
- Nodes deduped (merged count)
- Cross-links created
- Fitness scores updated
- Nodes garbage collected
- Any interesting cross-links discovered

If error, report full traceback. Do not retry — log and exit.
