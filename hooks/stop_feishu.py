#!/usr/bin/env python3
"""Feishu stop hook — auto-deliver assistant text via Feishu Bot API.

Fires when Claude finishes an assistant turn. If the user turn that triggered
this response came from a Feishu channel AND the assistant turn emitted
user-facing text AND no tool-based Feishu reply was sent, this hook POSTs
the text directly to the Feishu bot API.

This is a supplementary delivery layer on top of OpenClaw's native delivery.
It provides extra reliability (dedup, retries, chunking) and metacog telemetry.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

FEISHU_CHANNEL_RE = re.compile(
    r'<channel\s+source="plugin:feishu:[^"]*"(?P<attrs>[^>]*)>',
    re.IGNORECASE,
)
TELEGRAM_CHANNEL_RE = re.compile(
    r'<channel\s+source="plugin:telegram:[^"]*"(?P<attrs>[^>]*)>',
    re.IGNORECASE,
)
CHAT_ID_RE = re.compile(r'chat_id="([^"]+)"')
MSG_ID_RE = re.compile(r'message_id="([^"]+)"')
FEISHU_API_BASE = "https://open.feishu.cn/open-apis"
FEISHU_MAX_LEN = 4000  # Feishu text message limit

_DEFAULT_LOG_DIR = Path(os.environ.get("BRIDGE_LOG_DIR") or (Path.home() / "onyx-claude-logs"))
DELIVERY_LOG = Path(
    os.environ.get("STOP_HOOK_DELIVERY_LOG")
    or (_DEFAULT_LOG_DIR / "stop-hook-delivery.log")
)


def _log(event: str, **fields) -> None:
    try:
        DELIVERY_LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
        with open(DELIVERY_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def _read_transcript(path: Path) -> list[dict]:
    if not path.exists():
        return []
    messages: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            messages.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return messages


def _message_text(msg: dict) -> str:
    m = msg.get("message") or {}
    content = m.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(block.get("text", ""))
        elif btype == "thinking":
            pass  # Never surface thinking blocks to end users
    return "\n\n".join(p for p in parts if p)


def _tool_uses(msg: dict) -> list[str]:
    m = msg.get("message") or {}
    content = m.get("content")
    if not isinstance(content, list):
        return []
    names: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            names.append(block.get("name", ""))
    return names


def _is_real_user_message(msg: dict) -> bool:
    if msg.get("type") != "user":
        return False
    content = (msg.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    if not isinstance(content, list):
        return False
    for block in content:
        if isinstance(block, dict) and block.get("type") != "tool_result":
            return True
    return False


def _last_user_and_tail_assistant(
    messages: list[dict],
) -> tuple[dict | None, list[dict]]:
    latest_text_assistant_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("type") == "assistant" and _message_text(messages[i]).strip():
            latest_text_assistant_idx = i
            break

    anchor_idx = None
    if latest_text_assistant_idx is not None:
        for i in range(latest_text_assistant_idx - 1, -1, -1):
            if _is_real_user_message(messages[i]):
                anchor_idx = i
                break

    if anchor_idx is None:
        for i in range(len(messages) - 1, -1, -1):
            if _is_real_user_message(messages[i]):
                anchor_idx = i
                break
    if anchor_idx is None:
        return None, []
    end_idx = len(messages)
    for i in range(anchor_idx + 1, len(messages)):
        if _is_real_user_message(messages[i]):
            end_idx = i
            break
    tail = [m for m in messages[anchor_idx + 1 : end_idx] if m.get("type") == "assistant"]
    return messages[anchor_idx], tail


def _metacog_write_assistant(text: str, tool_uses: list[str], session_id: str | None, channel: str) -> None:
    """Best-effort metacog logging for assistant turn. Never raises."""
    try:
        import sys as _sys
        graph = str(Path.home() / ".openclaw" / "workspace" / "graph")
        if graph not in _sys.path:
            _sys.path.insert(0, graph)
        from metacog_writer import write_turn
        write_turn(
            "assistant",
            text or "",
            session_id=session_id,
            channel=channel,
            tool_uses=tool_uses or [],
        )
    except Exception as e:
        _log("metacog_skip", detail=f"{type(e).__name__}: {e}"[:200])


def _last_known_chat_id() -> str | None:
    if not DELIVERY_LOG.exists():
        return None
    try:
        lines = DELIVERY_LOG.read_text().splitlines()
    except Exception:
        return None
    for line in reversed(lines[-200:]):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("event") == "send_ok" and rec.get("chat_id") and rec.get("channel") == "feishu":
            return rec["chat_id"]
    return None


def _extract_channel_info(user_text: str) -> tuple[str | None, str | None]:
    m = FEISHU_CHANNEL_RE.search(user_text)
    if not m:
        return None, None
    attrs = m.group("attrs") or ""
    chat = CHAT_ID_RE.search(attrs)
    mid = MSG_ID_RE.search(attrs)
    return (chat.group(1) if chat else None, mid.group(1) if mid else None)


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
                # app_secret is stored in keychain, referenced as "source": "keychain"
                # We need to load it from keychain or env
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


def _chunk_for_feishu(text: str) -> list[str]:
    text = text.strip()
    if len(text) <= FEISHU_MAX_LEN:
        return [text] if text else []
    chunks: list[str] = []
    remaining = text
    while len(remaining) > FEISHU_MAX_LEN:
        window = remaining[:FEISHU_MAX_LEN]
        for sep in ("\n\n", "\n", ". ", " "):
            idx = window.rfind(sep)
            if idx > FEISHU_MAX_LEN // 2:
                chunks.append(remaining[:idx].rstrip())
                remaining = remaining[idx + len(sep):].lstrip()
                break
        else:
            chunks.append(remaining[:FEISHU_MAX_LEN])
            remaining = remaining[FEISHU_MAX_LEN:]
    if remaining:
        chunks.append(remaining)
    return chunks


def _send_message(
    token: str,
    chat_id: str,
    text: str,
    reply_to: str | None,
) -> tuple[bool, str]:
    """Send text message via Feishu Bot API."""
    import subprocess
    url = f"{FEISHU_API_BASE}/im/v1/messages?receive_id_type=chat_id"
    payload = {
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}),
    }
    if reply_to:
        payload["reply_in_thread"] = True

    last_err = ""
    for attempt in range(4):
        if attempt > 0:
            time.sleep(0.5 * (2 ** (attempt - 1)))  # 0.5s, 1s, 2s
        try:
            proc = subprocess.run(
                ["curl", "-sS", "--fail-with-body", "--max-time", "30",
                 "--connect-timeout", "10", "-X", "POST",
                 "-H", f"Authorization: Bearer {token}",
                 "-H", "Content-Type: application/json",
                 "-d", json.dumps(payload), url],
                capture_output=True, text=True, timeout=35,
            )
            if proc.returncode != 0:
                last_err = f"curl exit {proc.returncode}: {(proc.stderr or proc.stdout)[:200].strip()}"
                if proc.returncode == 22 and proc.stdout:
                    try:
                        parsed = json.loads(proc.stdout)
                        if not parsed.get("ok") and parsed.get("error_code", 500) < 500:
                            return False, parsed.get("msg", proc.stdout)
                    except Exception:
                        pass
                continue
            try:
                parsed = json.loads(proc.stdout)
            except Exception as e:
                last_err = f"json parse: {e}: {proc.stdout[:200]}"
                continue
            if parsed.get("code") != 0:
                return False, parsed.get("msg", proc.stdout)
            return True, ""
        except subprocess.TimeoutExpired:
            last_err = "curl subprocess timeout (>35s)"
        except FileNotFoundError:
            return False, "curl not found on PATH"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
    return False, last_err


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}

    transcript_path = payload.get("transcript_path")
    _log("feishu_hook_start", transcript=bool(transcript_path))
    if not transcript_path:
        return 0

    # If this is a Telegram channel message, let stop.py handle it
    messages_raw = Path(transcript_path).read_text() if Path(transcript_path).exists() else ""
    if "plugin:telegram:" in messages_raw:
        _log("feishu_skip", reason="telegram_channel_delegated")
        return 0

    messages: list[dict] = []
    user_msg = None
    assistant_msgs: list[dict] = []
    had_text = False
    for attempt in range(20):
        messages = _read_transcript(Path(transcript_path))
        user_msg, assistant_msgs = _last_user_and_tail_assistant(messages)
        if user_msg and assistant_msgs:
            if any(_message_text(am).strip() for am in assistant_msgs):
                had_text = True
                break
        if user_msg is None or not assistant_msgs:
            break
        time.sleep(0.5)
    if user_msg is None or not assistant_msgs:
        _log("feishu_skip", reason="no_anchor_or_tail", attempts=attempt + 1)
        return 0
    if not had_text:
        _log("feishu_skip", reason="no_tail_text_after_retries", attempts=attempt + 1)
        return 0

    # Telemetry FIRST
    _early_buf = "\n\n".join(t for t in (_message_text(am).strip() for am in assistant_msgs) if t)
    _early_tool_uses: list[str] = []
    for am in assistant_msgs:
        _early_tool_uses.extend(_tool_uses(am))
    _inbound_chat_pre, _ = _extract_channel_info(_message_text(user_msg))
    _metacog_write_assistant(
        _early_buf,
        _early_tool_uses,
        session_id=payload.get("session_id"),
        channel="feishu" if _inbound_chat_pre else "cli",
    )

    inbound_text = _message_text(user_msg)
    chat_id, reply_msg_id = _extract_channel_info(inbound_text)

    latest_chat_id = None
    latest_reply_to = None
    for m in reversed(messages):
        if not _is_real_user_message(m):
            continue
        c, r = _extract_channel_info(_message_text(m))
        if c:
            latest_chat_id, latest_reply_to = c, r
            break
    if latest_chat_id:
        chat_id, reply_msg_id = latest_chat_id, latest_reply_to

    # Skip delivery if this is an OpenClaw-managed native channel.
    # OpenClaw already delivers responses to Feishu natively.
    # We still run metacog telemetry but skip the manual send.
    if os.environ.get("OBL_SKIP_NATIVE_CHANNELS") == "1" and chat_id:
        _log("feishu_openclaw_managed_skip", reason="native_channel", chat_id=chat_id)
        return 0

    if not chat_id:
        chat_id = _last_known_chat_id()
        reply_msg_id = None
        if not chat_id:
            _log("feishu_skip", reason="no_channel_tag")
            return 0
        _log("feishu_fallback", reason="post_compaction_chat_id", chat_id=chat_id)

    parts = [t for t in (_message_text(am).strip() for am in assistant_msgs) if t]
    buf = "\n\n".join(parts) if parts else ""

    if not parts:
        _log("feishu_skip", reason="no_text", chat_id=chat_id)
        return 0

    # Deduplication
    import hashlib
    sig = hashlib.sha1(f"{chat_id}:{buf}".encode()).hexdigest()[:12]
    if DELIVERY_LOG.exists():
        now_epoch = time.time()
        from datetime import datetime as dt
        for line in DELIVERY_LOG.read_text().splitlines()[-20:]:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("event") != "send_ok" or rec.get("sig") != sig or rec.get("channel") != "feishu":
                continue
            try:
                prev_epoch = dt.fromisoformat(rec.get("ts", "")).timestamp()
                if now_epoch - prev_epoch > 1800:
                    continue
            except Exception:
                pass
            _log("feishu_skip", reason="duplicate", chat_id=chat_id, sig=sig)
            return 0

    if not buf.strip():
        _log("feishu_skip", reason="no_text", chat_id=chat_id)
        return 0

    app_id, app_secret = _get_feishu_creds()
    if not app_id or not app_secret:
        sys.stderr.write(
            "Feishu auto-delivery: FEISHU_APP_ID or FEISHU_APP_SECRET not found.\n"
        )
        return 2

    token = _get_tenant_access_token(app_id, app_secret)
    if not token:
        sys.stderr.write("Feishu auto-delivery: could not obtain tenant access token.\n")
        return 2

    chunks = _chunk_for_feishu(buf)
    first = True
    for chunk in chunks:
        ok, err = _send_message(
            token=token,
            chat_id=chat_id,
            text=chunk,
            reply_to=reply_msg_id if first else None,
        )
        if not ok:
            _log("feishu_send_fail", chat_id=chat_id, error=err)
            sys.stderr.write(
                f"Feishu auto-delivery failed: {err}. chat_id={chat_id}.\n"
            )
            return 2
        first = False
    _log("feishu_send_ok", chat_id=chat_id, reply_to=reply_msg_id, chunks=len(chunks),
         bytes=sum(len(c) for c in chunks), sig=sig, channel="feishu")
    return 0


if __name__ == "__main__":
    sys.exit(main())
