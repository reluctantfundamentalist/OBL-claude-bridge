#!/usr/bin/env python3
"""Feishu UserPromptSubmit hook — instant reaction on inbound Feishu messages.

Reacts with eyes emoji immediately so the sender knows the message was received,
even if Claude's turn takes minutes to complete.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

FEISHU_CHANNEL_RE = re.compile(
    r'<channel\s+(?P<attrs>[^>]*source="plugin:feishu:[^"]*"[^>]*)>',
    re.IGNORECASE,
)
CHAT_ID_RE = re.compile(r'chat_id="([^"]+)"')
MSG_ID_RE = re.compile(r'message_id="([^"]+)"')

FEISHU_API_BASE = "https://open.feishu.cn/open-apis"
_DEFAULT_LOG_DIR = Path(os.environ.get("BRIDGE_LOG_DIR") or (Path.home() / "onyx-claude-logs"))
REACT_LOG = Path(
    os.environ.get("USER_PROMPT_HOOK_LOG")
    or (_DEFAULT_LOG_DIR / "user-prompt-hook.log")
)


def _log(event: str, **fields) -> None:
    try:
        REACT_LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
        with open(REACT_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def _get_feishu_creds() -> tuple[str | None, str | None]:
    """Load Feishu app_id and app_secret from env or config file."""
    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    if app_id and app_secret:
        return app_id.strip(), app_secret.strip()

    config_path = Path.home() / ".lark-cli" / "openclaw" / "config.json"
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text())
            apps = cfg.get("apps", [])
            if apps:
                app = apps[0]
                app_id = app.get("appId")
                app_secret_source = app.get("appSecret", {}).get("source")
                if app_secret_source == "keychain":
                    keychain_id = app.get("appSecret", {}).get("id")
                    if keychain_id:
                        import subprocess
                        try:
                            result = subprocess.run(
                                ["security", "find-generic-password", "-s", keychain_id, "-w"],
                                capture_output=True, text=True, timeout=5,
                            )
                            if result.returncode == 0:
                                app_secret = result.stdout.strip()
                        except Exception:
                            pass
        except Exception:
            pass
    return app_id, app_secret


def _get_tenant_access_token(app_id: str, app_secret: str) -> str | None:
    """Obtain Feishu tenant access token."""
    import subprocess
    url = f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal"
    payload = json.dumps({"app_id": app_id, "app_secret": app_secret})
    try:
        proc = subprocess.run(
            ["curl", "-sS", "--fail-with-body", "--max-time", "15",
             "-X", "POST", "-H", "Content-Type: application/json",
             "-d", payload, url],
            capture_output=True, text=True, timeout=20,
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)
        if data.get("code") == 0:
            return data.get("tenant_access_token")
    except Exception:
        pass
    return None


def _metacog_write(payload: dict, channel: str) -> None:
    """Best-effort metacog logging. Never raises."""
    try:
        import sys as _sys
        graph = str(Path.home() / ".openclaw" / "workspace" / "graph")
        if graph not in _sys.path:
            _sys.path.insert(0, graph)
        from metacog_writer import write_turn
        text = payload.get("prompt") or ""
        # strip Feishu channel envelope so signal scoring sees real message text
        cleaned = re.sub(r'<channel\s+source="plugin:feishu[^>]*>', "", text)
        cleaned = re.sub(r'</channel>', "", cleaned).strip() or text
        write_turn(
            "user",
            cleaned,
            session_id=payload.get("session_id"),
            channel=channel,
            extra_context={"raw_len": len(text)},
        )
    except Exception as e:
        _log("metacog_skip", detail=f"{type(e).__name__}: {e}"[:200])


def _react(token: str, chat_id: str, message_id: str, emoji: str = "\U0001f440") -> tuple[bool, str]:
    """Add emoji reaction to a Feishu message."""
    import subprocess as _sp
    url = f"{FEISHU_API_BASE}/im/v1/messages/{message_id}/reactions"
    # Feishu reaction_type: { type: "emoji", emoji_type: "<emoji>" }
    payload = json.dumps({
        "reaction_type": {"type": "emoji", "emoji_type": emoji}
    })
    try:
        proc = _sp.run(
            ["curl", "-sS", "--fail-with-body", "--max-time", "10",
             "--connect-timeout", "5", "-X", "POST",
             "-H", f"Authorization: Bearer {token}",
             "-H", "Content-Type: application/json",
             "-d", payload, url],
            capture_output=True, text=True, timeout=12,
        )
        if proc.returncode != 0:
            return False, f"curl exit {proc.returncode}: {(proc.stderr or proc.stdout)[:200]}"
        return True, proc.stdout
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0

    prompt = payload.get("prompt") or ""
    m = FEISHU_CHANNEL_RE.search(prompt)
    _metacog_write(payload, channel="feishu" if m else "cli")
    if not m:
        return 0
    attrs = m.group("attrs") or ""
    chat = CHAT_ID_RE.search(attrs)
    mid = MSG_ID_RE.search(attrs)
    if not chat or not mid:
        return 0

    chat_id, message_id = chat.group(1), mid.group(1)
    sink = os.environ.get("USER_PROMPT_HOOK_TEST_SINK")
    if sink:
        with open(sink, "a") as f:
            f.write(json.dumps({"chat_id": chat_id, "message_id": message_id, "emoji": "\U0001f440", "channel": "feishu"}) + "\n")
        _log("feishu_react_test", chat_id=chat_id, message_id=message_id)
        return 0

    app_id, app_secret = _get_feishu_creds()
    if not app_id or not app_secret:
        _log("feishu_skip", reason="no_creds")
        return 0

    token = _get_tenant_access_token(app_id, app_secret)
    if not token:
        _log("feishu_skip", reason="no_token")
        return 0

    ok, detail = _react(token, chat_id, message_id)
    if ok:
        _log("feishu_react_ok", chat_id=chat_id, message_id=message_id)
    else:
        _log("feishu_react_fail", chat_id=chat_id, message_id=message_id, detail=detail[:200])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        _log("feishu_crash", detail=f"{type(e).__name__}: {e}")
        sys.exit(0)
