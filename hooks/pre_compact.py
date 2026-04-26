#!/usr/bin/env python3
"""PreCompact hook: extract signal from the pre-compact transcript to brain.

Routes the conversation through extract_transcript.py (transcript-shape
extractor running on a local Ollama model) instead of extract_inline.py
(which is for hand-curated signal blobs and would re-bill API tokens).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

CASHEW_DIR = Path(os.environ.get("CASHEW_HOME", str(Path.home() / ".openclaw" / "workspace" / "graph")))
EXTRACT_SCRIPT = CASHEW_DIR / "extract_transcript.py"
EXTRACT_MODEL = os.environ.get("ONYX_PRECOMPACT_MODEL", "gemma4:latest")
EXTRACT_TIMEOUT = int(os.environ.get("ONYX_PRECOMPACT_TIMEOUT", "600"))


def extract_to_brain(conversation_text: str) -> bool:
    if not EXTRACT_SCRIPT.exists():
        print(f"warning: extract script not found at {EXTRACT_SCRIPT}", file=sys.stderr)
        return False
    env = os.environ.copy()
    env["KMP_DUPLICATE_LIB_OK"] = "TRUE"
    try:
        result = subprocess.run(
            [sys.executable, str(EXTRACT_SCRIPT), "--input", "-", "--model", EXTRACT_MODEL],
            input=conversation_text,
            capture_output=True, text=True, timeout=EXTRACT_TIMEOUT,
            cwd=str(CASHEW_DIR), env=env,
        )
        out = (result.stderr or "").strip().splitlines()
        tail = "\n".join(out[-6:]) if out else ""
        if result.returncode == 0:
            print(f"PreCompact extraction complete:\n{tail}", file=sys.stderr)
            return True
        print(f"PreCompact extraction failed (rc={result.returncode}):\n{tail}", file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print(f"PreCompact extraction timed out after {EXTRACT_TIMEOUT}s", file=sys.stderr)
        return False


def main():
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    conversation = hook_input.get("conversation", "")
    trigger = hook_input.get("trigger", "unknown")

    if conversation:
        print(f"PreCompact ({trigger}): mining transcript via {EXTRACT_MODEL}", file=sys.stderr)
        extract_to_brain(conversation)
    else:
        print("PreCompact: no conversation content to extract", file=sys.stderr)

    print("Preserve in summary: all decisions, commitments, TODOs, corrections, "
          "and project status changes. (Brain extraction has run; this is a backup.)")


if __name__ == "__main__":
    main()
