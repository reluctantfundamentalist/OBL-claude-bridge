# Onyx Claude Bridge

A template for running [Claude Code](https://claude.com/claude-code) as a **persistent personal AI assistant** with:

- **Telegram connectivity** — receive messages and reply via bot, autodeliver on session response
- **macOS launchd watchdog** — auto-restart on crashes, run scheduled jobs
- **Hooks** — pre/post compaction, session start, stop, and prompt-submit lifecycle
- **Cron prompts** — declarative recurring tasks (sleep cycles, research, briefings, etc.)
- **Optional Engram integration** — pairs with the [Engram knowledge-graph skill](https://github.com/shadowfax1312/engram) for persistent memory

## Architecture

```
~/onyx-claude-bridge/
├── CLAUDE.md.template      # Customise: identity, rules, tool inventory
├── .env.example            # Copy to .env, fill in tokens
├── hooks/                  # Lifecycle handlers
│   ├── init.py             #   SessionStart — load identity / context
│   ├── pre_compact.py      #   PreCompact — extract signal before summary
│   ├── post_compact.py     #   PostCompact — refresh identity after summary
│   ├── user_prompt_submit.py  # Eyes-emoji ack on inbound TG message
│   └── stop.py             #   Auto-deliver assistant text to Telegram
├── cron/
│   ├── prompts/            # Markdown prompts fired by launchd jobs
│   ├── telegram-notify.sh  # Helper for cron-side TG sends
│   └── managed-crons.json  # In-session CronCreate jobs (re-registered on init)
├── launchd/
│   └── jobs/*.plist.template  # macOS launchd templates
└── scripts/
    ├── generate-plists.sh  # Materialise plists from templates
    ├── run-job.sh          # Helper invoked by launchd to run a cron prompt
    └── watchdog.sh         # Restart Claude Code if it dies
```

## Quick start

1. Clone this repo to `~/onyx-claude-bridge` (or any `$BRIDGE_DIR` you choose):
   ```bash
   git clone https://github.com/shadowfax1312/onyx-claude-bridge.git ~/onyx-claude-bridge
   cd ~/onyx-claude-bridge
   ```

2. Configure environment:
   ```bash
   cp .env.example .env
   # Fill in TELEGRAM_BOT_TOKEN (get from @BotFather), TELEGRAM_CHAT_ID
   ```

3. Customise your identity and rules:
   ```bash
   cp CLAUDE.md.template CLAUDE.md
   # Edit — replace __ASSISTANT_NAME__, __YOUR_NAME__ etc., adjust rules
   ```

4. Wire hooks into Claude Code (`~/.claude/settings.json`):
   ```json
   {
     "hooks": {
       "SessionStart":     [{"matcher":"","hooks":[{"type":"command","command":"python3 ~/onyx-claude-bridge/hooks/init.py"}]}],
       "UserPromptSubmit": [{"matcher":"","hooks":[{"type":"command","command":"python3 ~/onyx-claude-bridge/hooks/user_prompt_submit.py"}]}],
       "Stop":             [{"matcher":"","hooks":[{"type":"command","command":"python3 ~/onyx-claude-bridge/hooks/stop.py"}]}],
       "PreCompact":       [{"matcher":"","hooks":[{"type":"command","command":"python3 ~/onyx-claude-bridge/hooks/pre_compact.py"}]}],
       "PostCompact":      [{"matcher":"","hooks":[{"type":"command","command":"python3 ~/onyx-claude-bridge/hooks/post_compact.py"}]}]
     }
   }
   ```

5. (Optional) Set up launchd watchdog + cron jobs (macOS):
   ```bash
   bash scripts/generate-plists.sh   # materialise *.plist from templates
   cp launchd/jobs/*.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.onyx.claude.*.plist
   ```

6. (Optional) Install [Engram](https://github.com/shadowfax1312/engram) for persistent knowledge graph memory.

## Pairs With

- **[Engram](https://github.com/shadowfax1312/engram)** — knowledge graph + extraction skill. The bridge handles "always-on session"; Engram handles "always-on memory".
- **Claude Code** — Anthropic's CLI for Claude
- **Telegram MCP plugin** — for Telegram tool surface

## Telegram Setup

Cookbook for getting Telegram wired:

1. Message `@BotFather` on Telegram → `/newbot` → grab the token, set as `TELEGRAM_BOT_TOKEN`
2. Message `@userinfobot` to get your numeric chat ID, set as `TELEGRAM_CHAT_ID`
3. Send your bot a message first (else it can't DM you)
4. Optional: install the official `mcp-telegram` plugin for the read/reply tool surface from inside Claude Code

## Honest caveats

- **macOS-only watchdog/launchd setup.** Linux equivalent (systemd user units) not provided yet — easy port if you want.
- **Hooks assume Claude Code's hook API as of April 2026.** Anthropic may change hook payload schemas.
- **Cron prompts use Claude API tokens** — these are real money. Start with low-frequency jobs, watch your spend.
- **Telegram auto-delivery via Stop hook** has occasional SSL handshake timeouts on residential connections — known issue, retries usually succeed.

## License

MIT

## Credits

Built by [@shadowfax1312](https://github.com/shadowfax1312) as the bridge layer for the Onyx personal assistant.
