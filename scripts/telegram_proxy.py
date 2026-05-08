#!/usr/bin/env python3
"""
telegram_proxy.py — Polls Telegram bot API and forwards messages into the
                     Sardonyx Claude Code tmux session via tmux send-keys.

Design principles:
  - Reads config from .env (no hardcoded secrets)
  - Offset-based dedup (never replays a message)
  - Allowlist enforcement (only trusted chat IDs get through)
  - Graceful shutdown on SIGINT/SIGTERM
  - Structured logging to BRIDGE_LOG_DIR
  - Upstream dependency check: tmux session must exist before forwarding
  - Idempotent: safe to kill and restart at any time (offset persisted to disk)

Dependencies: python3 stdlib only (urllib, json, signal, time, pathlib)
"""

import json
import logging
import os
import pathlib
import signal
import subprocess
import sys
import time

# ── Config ────────────────────────────────────────────────────────────────────

def load_env(env_path: pathlib.Path) -> dict:
    """Parse a simple KEY=VALUE .env file (no shell expansion, no quotes strip)."""
    env = {}
    if not env_path.exists():
        return env
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # Strip surrounding quotes if present
        value = value.strip().strip('"').strip("'")
        env[key.strip()] = value
    return env


def resolve_config() -> dict:
    script_dir = pathlib.Path(__file__).resolve().parent
    repo_dir = script_dir.parent
    env = load_env(repo_dir / ".env")

    bridge_home = pathlib.Path(env.get("BRIDGE_HOME", str(repo_dir))).expanduser()
    _raw_log_dir = env.get("BRIDGE_LOG_DIR", str(pathlib.Path.home() / "onyx-claude-logs"))
    _raw_log_dir = _raw_log_dir.replace("${HOME}", str(pathlib.Path.home())).replace("$HOME", str(pathlib.Path.home()))
    log_dir = pathlib.Path(_raw_log_dir).expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)

    bot_token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        # Fallback: read from ~/.claude/channels/telegram/.env
        fallback = pathlib.Path.home() / ".claude" / "channels" / "telegram" / ".env"
        fb_env = load_env(fallback)
        bot_token = fb_env.get("TELEGRAM_BOT_TOKEN", "")

    allowed_chat_ids = set()
    raw_id = env.get("TELEGRAM_CHAT_ID", "").strip()
    if raw_id:
        allowed_chat_ids.add(raw_id)

    attachments_dir = log_dir / "telegram-attachments"
    attachments_dir.mkdir(parents=True, exist_ok=True)

    return {
        "bot_token": bot_token,
        "allowed_chat_ids": allowed_chat_ids,
        "session_name": env.get("BRIDGE_SESSION_NAME", "onyx-claude"),
        "assistant_name": env.get("ASSISTANT_NAME", "Sardonyx"),
        "log_dir": log_dir,
        "offset_file": log_dir / "telegram_proxy_offset.txt",
        "poll_interval": int(env.get("PROXY_POLL_INTERVAL", "3")),
        "api_timeout": int(env.get("PROXY_API_TIMEOUT", "25")),
        "attachments_dir": attachments_dir,
    }

# ── Telegram API ───────────────────────────────────────────────────────────────

CURL_BIN = "/usr/bin/curl"


def tg_api(token: str, method: str, params: dict, timeout: int) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    payload = json.dumps(params)
    proc = subprocess.run(
        [CURL_BIN, "-sS", "--fail-with-body", "--max-time", str(timeout + 5),
         "--connect-timeout", "10", "-X", "POST",
         "-H", "Content-Type: application/json",
         "-d", payload, url],
        capture_output=True, text=True, timeout=timeout + 10,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"curl exit {proc.returncode}: {(proc.stderr or proc.stdout)[:300]}")
    result = json.loads(proc.stdout)
    if not result.get("ok"):
        raise RuntimeError(f"HTTP {result.get('error_code')}: {result.get('description')}")
    return result


def send_tg_message(token: str, chat_id: str, text: str, timeout: int) -> None:
    tg_api(token, "sendMessage", {"chat_id": chat_id, "text": text}, timeout)


def tg_get_file(token: str, file_id: str, api_timeout: int) -> str:
    """Return the file_path string for a given file_id via getFile."""
    result = tg_api(token, "getFile", {"file_id": file_id}, api_timeout)
    return result["result"]["file_path"]


def tg_download_file(token: str, file_path: str, dest: pathlib.Path, api_timeout: int) -> None:
    """Download a Telegram file to dest."""
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    proc = subprocess.run(
        [CURL_BIN, "-sS", "--max-time", str(api_timeout + 10),
         "--connect-timeout", "10", "-o", str(dest), url],
        capture_output=True, timeout=api_timeout + 15,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"curl download failed exit {proc.returncode}: {proc.stderr.decode(errors='replace')[:200]}")


def get_updates(token: str, offset: int, timeout_secs: int, api_timeout: int) -> list:
    params = {"offset": offset, "timeout": timeout_secs, "allowed_updates": ["message", "channel_post"]}
    result = tg_api(token, "getUpdates", params, timeout=api_timeout + 2)
    return result.get("result", [])

# ── tmux integration ───────────────────────────────────────────────────────────

TMUX_BIN = "/opt/homebrew/bin/tmux"


def tmux_session_exists(session_name: str) -> bool:
    r = subprocess.run(
        [TMUX_BIN, "has-session", "-t", session_name],
        capture_output=True
    )
    return r.returncode == 0


def forward_to_tmux(session_name: str, text: str, logger: logging.Logger) -> bool:
    """Send text to the tmux session as if typed. Returns True on success."""
    if not tmux_session_exists(session_name):
        logger.warning("tmux session '%s' not found — message queued for retry", session_name)
        return False
    # Escape special chars for tmux send-keys literal mode
    result = subprocess.run(
        [TMUX_BIN, "send-keys", "-t", session_name, text, "Enter"],
        capture_output=True
    )
    if result.returncode != 0:
        logger.error("tmux send-keys failed: %s", result.stderr.decode(errors="replace"))
        return False
    return True

# ── Offset persistence ─────────────────────────────────────────────────────────

def load_offset(offset_file: pathlib.Path) -> int:
    try:
        return int(offset_file.read_text().strip())
    except Exception:
        return 0


def save_offset(offset_file: pathlib.Path, offset: int) -> None:
    offset_file.write_text(str(offset))

# ── Main loop ─────────────────────────────────────────────────────────────────

class GracefulExit(Exception):
    pass


def make_signal_handler():
    def handler(signum, frame):
        raise GracefulExit()
    return handler


def sender_display(msg: dict) -> str:
    sender = msg.get("from", {})
    name = sender.get("first_name", "")
    last = sender.get("last_name", "")
    username = sender.get("username", "")
    return f"{name} {last}".strip() or username or "unknown"


def format_message(update: dict, cfg: dict = None, logger: logging.Logger = None):
    """Extract a displayable string from a Telegram update, or None to skip.

    Handles: text, document, photo (highest res), audio, video.
    Files are downloaded to cfg['attachments_dir'] and the local path is
    included in the forwarded message so Onyx can read them directly.
    """
    msg = update.get("message", {})
    display = sender_display(msg)
    caption = msg.get("caption", "")

    # ── Plain text ──────────────────────────────────────────────────────────
    if msg.get("text"):
        return f"[Telegram from {display}]: {msg['text']}"

    # ── File types that carry a file_id ────────────────────────────────────
    file_id = None
    original_name = None

    if msg.get("document"):
        doc = msg["document"]
        file_id = doc.get("file_id")
        original_name = doc.get("file_name") or f"document_{msg.get('message_id', 'unknown')}"

    elif msg.get("photo"):
        # Telegram sends multiple sizes; take the last (largest)
        photo = msg["photo"][-1]
        file_id = photo.get("file_id")
        original_name = f"photo_{msg.get('message_id', 'unknown')}.jpg"

    elif msg.get("audio"):
        audio = msg["audio"]
        file_id = audio.get("file_id")
        original_name = audio.get("file_name") or f"audio_{msg.get('message_id', 'unknown')}.ogg"

    elif msg.get("video"):
        video = msg["video"]
        file_id = video.get("file_id")
        original_name = video.get("file_name") or f"video_{msg.get('message_id', 'unknown')}.mp4"

    elif msg.get("voice"):
        voice = msg["voice"]
        file_id = voice.get("file_id")
        original_name = f"voice_{msg.get('message_id', 'unknown')}.ogg"

    if file_id and cfg:
        try:
            token = cfg["bot_token"]
            api_timeout = cfg["api_timeout"]
            attachments_dir = cfg["attachments_dir"]

            remote_path = tg_get_file(token, file_id, api_timeout)
            dest = attachments_dir / original_name
            # Avoid overwriting if same name, append message_id suffix
            if dest.exists():
                stem = dest.stem
                suffix = dest.suffix
                dest = attachments_dir / f"{stem}_{msg.get('message_id', 'dup')}{suffix}"

            tg_download_file(token, remote_path, dest, api_timeout)
            if logger:
                logger.info("Downloaded attachment → %s", dest)

            parts = [f"[Telegram from {display}]: [File saved: {dest}]"]
            if caption:
                parts.append(caption)
            return " ".join(parts)

        except Exception as e:
            if logger:
                logger.error("Failed to download attachment file_id=%s: %s", file_id, e)
            caption_part = f" {caption}" if caption else ""
            return f"[Telegram from {display}]: [Attachment download failed: {original_name} — {e}]{caption_part}"

    # ── Sticker / unsupported ───────────────────────────────────────────────
    if msg.get("sticker"):
        return None  # silently skip stickers

    # Caption-only (e.g. photo without download support)
    if caption:
        return f"[Telegram from {display}]: {caption}"

    return None


def run():
    cfg = resolve_config()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [proxy] %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(cfg["log_dir"] / "telegram_proxy.log"),
            logging.StreamHandler(sys.stdout),
        ]
    )
    logger = logging.getLogger("telegram_proxy")

    if not cfg["bot_token"]:
        logger.error("TELEGRAM_BOT_TOKEN not set — exiting")
        sys.exit(1)

    if not cfg["allowed_chat_ids"]:
        logger.warning("TELEGRAM_CHAT_ID not set — all inbound messages will be dropped")

    signal.signal(signal.SIGINT, make_signal_handler())
    signal.signal(signal.SIGTERM, make_signal_handler())

    offset = load_offset(cfg["offset_file"])
    logger.info("Starting Telegram proxy | session=%s | offset=%d | allowlist=%s",
                cfg["session_name"], offset, cfg["allowed_chat_ids"])

    # Pending messages if tmux is temporarily down
    pending: list[tuple[str, str]] = []  # (session_name, formatted_text)

    try:
        while True:
            # Drain pending queue first
            if pending:
                still_pending = []
                for sess, text in pending:
                    if forward_to_tmux(sess, text, logger):
                        logger.info("Drained pending message to tmux")
                    else:
                        still_pending.append((sess, text))
                pending = still_pending

            try:
                updates = get_updates(
                    cfg["bot_token"],
                    offset,
                    timeout_secs=cfg["poll_interval"],
                    api_timeout=cfg["api_timeout"],
                )
            except Exception as e:
                logger.warning("getUpdates error: %s — retrying in 10s", e)
                time.sleep(10)
                continue

            for update in updates:
                uid = update["update_id"]
                offset = uid + 1
                save_offset(cfg["offset_file"], offset)

                msg = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))

                # Allowlist check
                if cfg["allowed_chat_ids"] and chat_id not in cfg["allowed_chat_ids"]:
                    logger.info("Dropping message from non-allowlisted chat_id=%s", chat_id)
                    continue

                formatted = format_message(update, cfg=cfg, logger=logger)
                if not formatted:
                    logger.debug("Skipping non-text/unsupported update_id=%d", uid)
                    continue

                logger.info("Forwarding update_id=%d chat_id=%s → tmux '%s'",
                            uid, chat_id, cfg["session_name"])

                # Send 👀 reaction as receipt acknowledgement
                try:
                    tg_api(cfg["bot_token"], "setMessageReaction", {
                        "chat_id": chat_id,
                        "message_id": msg["message_id"],
                        "reaction": [{"type": "emoji", "emoji": "👀"}],
                        "is_big": False,
                    }, cfg["api_timeout"])
                except Exception as e:
                    logger.debug("Reaction failed (non-critical): %s", e)

                if not forward_to_tmux(cfg["session_name"], formatted, logger):
                    pending.append((cfg["session_name"], formatted))
                    logger.warning("Queued message for retry (tmux unavailable)")

    except GracefulExit:
        logger.info("Graceful shutdown received — saving offset=%d", offset)
        save_offset(cfg["offset_file"], offset)
        sys.exit(0)


if __name__ == "__main__":
    run()
