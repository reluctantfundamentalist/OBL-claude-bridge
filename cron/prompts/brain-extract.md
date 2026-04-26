Extract any pending signal from the staging file to brain.db.

1. Check if `/tmp/onyx-extract.md` exists and is non-empty.
2. If yes, run:
```bash
cd ~/.openclaw/workspace/graph && python3 extract_inline.py
```
3. Report: nodes extracted count, or "no pending signal".

If the file doesn't exist or is empty, report EXTRACT_OK — nothing to do.
If extraction fails, report the error but do not retry.
