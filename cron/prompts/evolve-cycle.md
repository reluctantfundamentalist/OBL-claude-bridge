Run the Onyx evolution/PRD cycle — generate capability PRDs from dopamine-ranked gaps.

```bash
cd ~/.openclaw/workspace/graph && python3 evolve.py
```

Report:
- Gap types processed (with dopamine scores)
- PRDs generated (title, capability, effort, priority score)
- PRDs delivered to Telegram (if score >= 0.50)
- Any PRDs penalized (vague spec, no research basis)

If error, report full traceback. Do not retry.
