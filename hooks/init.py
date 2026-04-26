#!/usr/bin/env python3
"""SessionStart hook: load brain context on session start/resume.

Queries brain.db via context.py for identity nodes on every session start.
No-ops silently if CASHEW_HOME isn't set or context.py isn't present.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

CASHEW_DIR = Path(os.environ.get("CASHEW_HOME", str(Path.home() / ".openclaw" / "workspace" / "graph")))
CONTEXT_SCRIPT = CASHEW_DIR / "context.py"
MANAGED_CRONS = Path.home() / "onyx-claude-bridge" / "cron" / "onyx-managed-crons.json"

IDENTITY_HINTS = os.environ.get(
    "BRIDGE_IDENTITY_HINTS",
    "identity beliefs operating principles assistant brain",
)


def query_brain(query: str, top_k: int = 7) -> str:
    if not CONTEXT_SCRIPT.exists():
        return ""
    env = os.environ.copy()
    env["KMP_DUPLICATE_LIB_OK"] = "TRUE"
    try:
        result = subprocess.run(
            [sys.executable, str(CONTEXT_SCRIPT), query, "--top-k", str(top_k)],
            capture_output=True, text=True, timeout=60,
            cwd=str(CASHEW_DIR), env=env,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except subprocess.TimeoutExpired:
        return ""


def emit_cron_reminder():
    if not MANAGED_CRONS.exists():
        return
    try:
        spec = json.loads(MANAGED_CRONS.read_text())
        jobs = spec.get("jobs", [])
    except Exception:
        return
    if not jobs:
        return
    print("\n## Onyx-Managed Crons (re-register on first action)")
    print(f"CronCreate jobs do not persist across sessions. Spec: {MANAGED_CRONS}")
    print("On your first response, run CronList. For any of these jobs not present, call CronCreate using the cron + prompt fields from the spec file (skip entries marked one_shot=true — those don't auto-re-register):")
    for j in jobs:
        if j.get("one_shot"):
            continue
        print(f"- {j['name']} ({j['cron']}) — {j.get('description','')}")


def main():
    identity = query_brain(IDENTITY_HINTS)
    if identity:
        print(f"## Brain Context (auto-loaded on init)\n{identity}")
        print("Brain context loaded on session start.", file=sys.stderr)
    else:
        print("No brain context available. Running without memory.", file=sys.stderr)
    emit_cron_reminder()


if __name__ == "__main__":
    main()
