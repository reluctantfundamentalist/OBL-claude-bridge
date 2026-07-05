#!/usr/bin/env python3
"""Feishu PostCompact hook — restore identity + recent context after session compaction.

Identical to post_compact.py but with identity hints tuned for Feishu sessions.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

CASHEW_DIR = Path(os.environ.get("CASHEW_HOME", str(Path.home() / ".openclaw" / "workspace" / "graph")))
CONTEXT_SCRIPT = CASHEW_DIR / "context.py"
IDENTITY_HINTS = os.environ.get(
    "BRIDGE_IDENTITY_HINTS",
    "Onyx identity beliefs operating principles assistant brain",
)
RECENT_HINTS = os.environ.get(
    "BRIDGE_RECENT_HINTS",
    "recent projects decisions active work TODO commitments",
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


def main():
    identity = query_brain(IDENTITY_HINTS)
    if identity:
        print(f"## Identity (post-compact)\n{identity}")
    recent = query_brain(RECENT_HINTS, top_k=5)
    if recent:
        print(f"## Recent Context\n{recent}")
    print("Brain context restored after compaction.", file=sys.stderr)


if __name__ == "__main__":
    main()
