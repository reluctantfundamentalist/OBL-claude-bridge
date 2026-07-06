#!/usr/bin/env python3
"""
feishu_proxy.py — Receives Feishu webhook events and forwards messages into the
                   Claude Code tmux session via tmux send-keys.

Setup:
  1. Create a Feishu custom app in developer console
  2. Enable Event Subscription, set URL to http://<your-ip>:<PORT>/webhook
  3. Subscribe to im.message.receive_v1 event
  4. Set FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_VERIFICATION_TOKEN in .env

Dependencies: python3 stdlib only
"""

from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import logging
import os
import pathlib
import signal
import subprocess
import sys
import time
import urllib.request

# ── Config ─────────────────────────────────────────────────────────────────────

def load_env(env_path: pathlib.Path) -> dict:
    env = {}
    if not env_path.exists():
        return env
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        env[key.strip()] = value
    return env


def resolve_config() -> dict:
    script_dir = pathlib.Path(__file__).resolve().parent
    repo_dir = script_dir.parent
    env = load_env(repo_dir / ".env")

    log_dir_raw = env.get("BRIDGE_LOG_DIR", str(pathlib.Path.home() / "onyx-claude-logs"))
    log_dir = pathlib.Path(log_dir_raw.replace("${HOME}", str(pathlib.Path.home())).replace("$HOME", str(pathlib.Path.home()))).expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)

    return {
        "app_id":             env.get("FEISHU_APP_ID", ""),
        "app_secret":         env.get("FEISHU_APP_SECRET", ""),
        "verification_token": env.get("FEISHU_VERIFICATION_TOKEN", ""),
        "encrypt_key":        env.get("FEISHU_ENCRYPT_KEY", ""),
        "port":               int(env.get("FEISHU_PROXY_PORT", "5200")),
        "session_name":       env.get("BRIDGE_SESSION_NAME", "onyx-claude"),
        "window":             env.get("BRIDGE_WINDOW", "0"),
        "log_dir":            log_dir,
        "log_file":           log_dir / "feishu_proxy.log",
        "processed_ids_file": log_dir / "feishu_proxy_processed.json",
        "attachment_dir":     log_dir / "telegram-attachments",  # reuse same dir
        "allowed_chat_ids":   set(filter(None, env.get("FEISHU_ALLOWED_CHAT_IDS", "").split(","))),
    }


# ── Logging ────────────────────────────────────────────────────────────────────

def setup_logger(log_file: pathlib.Path) -> logging.Logger:
    logger = logging.getLogger("feishu_proxy")
    logger.setLevel(logging.DEBUG)
    fh = logging.FileHandler(log_file)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


# ── Deduplication ─────────────────────────────────────────────────────────────

def load_processed(path: pathlib.Path, window: int = 3600) -> set:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
        cutoff = time.time() - window
        return {k for k, v in data.items() if v > cutoff}
    except Exception:
        return set()


def save_processed(path: pathlib.Path, ids: set) -> None:
    try:
        existing: dict = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text())
            except Exception:
                pass
        cutoff = time.time() - 7200
        existing = {k: v for k, v in existing.items() if v > cutoff}
        now = time.time()
        for mid in ids:
            existing[mid] = now
        path.write_text(json.dumps(existing))
    except Exception:
        pass


# ── Feishu API ─────────────────────────────────────────────────────────────────

_token_cache: dict = {"token": "", "expires": 0}

def get_tenant_token(app_id: str, app_secret: str, logger: logging.Logger) -> str:
    if time.time() < _token_cache["expires"] - 60:
        return _token_cache["token"]
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    payload = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    try:
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if data.get("code") == 0:
            _token_cache["token"] = data["tenant_access_token"]
            _token_cache["expires"] = time.time() + data.get("expire", 7200)
            return _token_cache["token"]
    except Exception as e:
        logger.warning("get_tenant_token failed: %s", e)
    return ""


def get_user_name(open_id: str, token: str, logger: logging.Logger) -> str:
    url = f"https://open.feishu.cn/open-apis/contact/v3/users/{open_id}?user_id_type=open_id"
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        if data.get("code") == 0:
            user = data.get("data", {}).get("user", {})
            return user.get("name") or user.get("en_name") or open_id
    except Exception:
        pass
    return open_id


def decrypt_feishu(encrypt_key: str, encrypted: str) -> str | None:
    """AES-256-CBC decrypt Feishu encrypted body."""
    try:
        import base64
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend
        key = hashlib.sha256(encrypt_key.encode()).digest()
        data = base64.b64decode(encrypted)
        iv, ciphertext = data[:16], data[16:]
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        dec = cipher.decryptor()
        plaintext = dec.update(ciphertext) + dec.finalize()
        pad = plaintext[-1]
        return plaintext[:-pad].decode("utf-8")
    except Exception:
        return None


# ── tmux injection ─────────────────────────────────────────────────────────────

TMUX_BIN = os.environ.get("TMUX_BIN") or "/opt/homebrew/bin/tmux"
if not pathlib.Path(TMUX_BIN).exists():
    TMUX_BIN = "tmux"


def strip_lone_surrogates(text: str) -> str:
    return text.encode("utf-8", "ignore").decode("utf-8", "ignore")


def forward_to_tmux(session: str, window: str, text: str, logger: logging.Logger) -> bool:
    target = f"{session}:{window}"
    result = subprocess.run(
        [TMUX_BIN, "send-keys", "-t", target, text, "Enter"],
        capture_output=True,
    )
    if result.returncode != 0:
        logger.error("tmux send-keys failed: %s", result.stderr.decode(errors="replace"))
        return False
    return True


def tmux_session_exists(session: str) -> bool:
    return subprocess.run(
        [TMUX_BIN, "has-session", "-t", session],
        capture_output=True,
    ).returncode == 0


# ── Message formatting ─────────────────────────────────────────────────────────

def format_message(sender_name: str, chat_id: str, message_id: str,
                   chat_type: str, text: str, group_name: str = "") -> str:
    channel_envelope = (
        f'<channel source="plugin:feishu:direct" '
        f'chat_id="{chat_id}" message_id="{message_id}">'
    )
    if chat_type == "p2p":
        prefix = f"[Feishu from {sender_name}]"
    else:
        grp = f" in {group_name}" if group_name else ""
        prefix = f"[Feishu from {sender_name}{grp}]"
    return f"{channel_envelope}\n{prefix}: {text}"


# ── HTTP handler ───────────────────────────────────────────────────────────────

class FeishuHandler(http.server.BaseHTTPRequestHandler):
    cfg: dict
    logger: logging.Logger
    processed: set

    def log_message(self, fmt, *args):
        pass  # suppress default access log

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        try:
            data = json.loads(body)
        except Exception:
            self._respond(400, b"invalid json")
            return

        # Encrypted event
        if "encrypt" in data:
            if not self.cfg.get("encrypt_key"):
                self.logger.warning("Received encrypted event but FEISHU_ENCRYPT_KEY not set")
                self._respond(400, b"encryption not configured")
                return
            decrypted = decrypt_feishu(self.cfg["encrypt_key"], data["encrypt"])
            if not decrypted:
                self._respond(400, b"decryption failed")
                return
            try:
                data = json.loads(decrypted)
            except Exception:
                self._respond(400, b"decrypted json invalid")
                return

        # URL verification challenge
        if data.get("type") == "url_verification":
            challenge = data.get("challenge", "")
            self.logger.info("Feishu URL verification challenge received")
            self._respond(200, json.dumps({"challenge": challenge}).encode())
            return

        # Token verification (v1 events)
        token = data.get("token") or (data.get("header") or {}).get("token", "")
        if self.cfg.get("verification_token") and token != self.cfg["verification_token"]:
            self.logger.warning("Verification token mismatch — dropping event")
            self._respond(401, b"unauthorized")
            return

        # Respond 200 immediately (Feishu requires fast response)
        self._respond(200, b'{"code":0}')

        # Process event asynchronously
        try:
            self._handle_event(data)
        except Exception as e:
            self.logger.error("Event handling error: %s", e)

    def _respond(self, code: int, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _handle_event(self, data: dict):
        # Support both v1 and v2 event schemas
        schema = data.get("schema", "1.0")
        if schema == "2.0":
            header = data.get("header", {})
            event_type = header.get("event_type", "")
            event = data.get("event", {})
        else:
            event_type = data.get("event", {}).get("type", "")
            event = data.get("event", {})

        if event_type not in ("im.message.receive_v1", "message"):
            return

        message = event.get("message", {})
        message_id = message.get("message_id", "")
        chat_id = message.get("chat_id", "")
        chat_type = message.get("chat_type", "p2p")
        message_type = message.get("message_type", "text")

        if not message_id or not chat_id:
            return

        # Dedup
        if message_id in self.processed:
            self.logger.debug("Skipping already-processed message_id=%s", message_id)
            return
        self.processed.add(message_id)
        save_processed(self.cfg["processed_ids_file"], {message_id})

        # Allowlist
        if self.cfg["allowed_chat_ids"] and chat_id not in self.cfg["allowed_chat_ids"]:
            self.logger.info("Dropping message from non-allowlisted chat_id=%s", chat_id)
            return

        # Get sender name
        sender = event.get("sender", {})
        open_id = (sender.get("sender_id") or {}).get("open_id", "")
        sender_name = open_id
        if open_id and self.cfg.get("app_id") and self.cfg.get("app_secret"):
            token = get_tenant_token(self.cfg["app_id"], self.cfg["app_secret"], self.logger)
            if token:
                sender_name = get_user_name(open_id, token, self.logger)

        # Get chat name (for groups)
        group_name = message.get("chat_id", "")  # Could fetch chat info but keep it simple

        # Parse message content
        if message_type != "text":
            self.logger.debug("Skipping non-text message type=%s", message_type)
            return

        try:
            content = json.loads(message.get("content", "{}"))
            text = content.get("text", "").strip()
        except Exception:
            text = ""

        if not text:
            return

        text = strip_lone_surrogates(text)
        formatted = format_message(sender_name, chat_id, message_id, chat_type, text, group_name)

        if not tmux_session_exists(self.cfg["session_name"]):
            self.logger.warning("tmux session '%s' not found — dropping message", self.cfg["session_name"])
            return

        self.logger.info("Forwarding message_id=%s chat_id=%s → tmux '%s:%s'",
                         message_id, chat_id, self.cfg["session_name"], self.cfg["window"])
        forward_to_tmux(self.cfg["session_name"], self.cfg["window"], formatted, self.logger)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    cfg = resolve_config()
    logger = setup_logger(cfg["log_file"])

    if not cfg["app_id"] or not cfg["app_secret"]:
        logger.error("FEISHU_APP_ID and FEISHU_APP_SECRET must be set in .env")
        sys.exit(1)

    processed = load_processed(cfg["processed_ids_file"])
    logger.info("Loaded %d recently processed message IDs", len(processed))

    # Inject config/state into handler class
    FeishuHandler.cfg = cfg
    FeishuHandler.logger = logger
    FeishuHandler.processed = processed

    server = http.server.HTTPServer(("0.0.0.0", cfg["port"]), FeishuHandler)
    logger.info("Feishu proxy listening on port %d → tmux session '%s:%s'",
                cfg["port"], cfg["session_name"], cfg["window"])

    def _shutdown(sig, frame):
        logger.info("Shutting down (signal %d)", sig)
        server.shutdown()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    server.serve_forever()


if __name__ == "__main__":
    main()
