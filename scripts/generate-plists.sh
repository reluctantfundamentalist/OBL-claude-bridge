#!/bin/bash
# Generate launchd plist files for Onyx scheduled jobs.
#
# Reads .env for BRIDGE_HOME, BRIDGE_LOG_DIR, BRIDGE_LABEL_PREFIX.
# Install after generation:
#   for f in launchd/jobs/*.plist; do
#     cp "$f" ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/$(basename "$f")
#   done

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SELF_DIR/.." && pwd)"
[ -f "$REPO_DIR/.env" ] && source "$REPO_DIR/.env"

BRIDGE_DIR="${BRIDGE_HOME:-$REPO_DIR}"
LOG_DIR="${BRIDGE_LOG_DIR:-$HOME/onyx-claude-logs}"
LABEL_PREFIX="${BRIDGE_LABEL_PREFIX:-com.onyx.claude}"

PLIST_DIR="$BRIDGE_DIR/launchd/jobs"
RUNNER="$BRIDGE_DIR/scripts/run-job.sh"
mkdir -p "$PLIST_DIR"

generate_plist() {
    local NAME="$1"
    local HOUR="$2"
    local MINUTE="$3"
    local WEEKDAY="$4"
    local DAY="$5"
    local INTERVAL="$6"
    local NOTIFY="$7"

    local LABEL="${LABEL_PREFIX}.job.${NAME}"
    local FILE="$PLIST_DIR/${LABEL}.plist"

    local NOTIFY_FLAG=""
    [ "$NOTIFY" = "true" ] && NOTIFY_FLAG="<string>--notify</string>"

    if [ -n "$INTERVAL" ]; then
        cat > "$FILE" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${RUNNER}</string>
        <string>${NAME}</string>
        ${NOTIFY_FLAG}
    </array>
    <key>StartInterval</key>
    <integer>${INTERVAL}</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/${NAME}-launchd.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/${NAME}-launchd-err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>HOME</key>
        <string>${HOME}</string>
    </dict>
</dict>
</plist>
PLIST
    else
        local CALENDAR=""
        if [ -n "$WEEKDAY" ] && [ -n "$DAY" ]; then
            CALENDAR="<key>Weekday</key><integer>${WEEKDAY}</integer><key>Day</key><integer>${DAY}</integer>"
        elif [ -n "$WEEKDAY" ]; then
            CALENDAR="<key>Weekday</key><integer>${WEEKDAY}</integer>"
        elif [ -n "$DAY" ]; then
            CALENDAR="<key>Day</key><integer>${DAY}</integer>"
        fi

        cat > "$FILE" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${RUNNER}</string>
        <string>${NAME}</string>
        ${NOTIFY_FLAG}
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>${HOUR}</integer>
        <key>Minute</key>
        <integer>${MINUTE}</integer>
        ${CALENDAR}
    </dict>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/${NAME}-launchd.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/${NAME}-launchd-err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>HOME</key>
        <string>${HOME}</string>
    </dict>
</dict>
</plist>
PLIST
    fi
    echo "Generated: $FILE"
}

# === WATCHDOG (60s keepalive) ===
WATCHDOG_LABEL="${LABEL_PREFIX}.claude-bridge"
WATCHDOG_FILE="$PLIST_DIR/${WATCHDOG_LABEL}.plist"
cat > "$WATCHDOG_FILE" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${WATCHDOG_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${BRIDGE_DIR}/scripts/watchdog.sh</string>
    </array>
    <key>StartInterval</key>
    <integer>60</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/watchdog-launchd.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/watchdog-launchd-err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>HOME</key>
        <string>${HOME}</string>
    </dict>
</dict>
</plist>
PLIST
echo "Generated: $WATCHDOG_FILE"

# === ONYX BRAIN MAINTENANCE JOBS ===
generate_plist "brain-extract"     "" "" "" "" "7200"  "false"    # every 2hrs
generate_plist "sleep-cycle"       "" "" "" "" "21600" "false"    # every 6hrs
generate_plist "ruminate-cycle"    "" "" "" "" "21600" "false"    # every 6hrs (offset by start time)
generate_plist "health-check"      "8" "0" "" "" ""    "true"     # 8am daily, notify
generate_plist "evolve-cycle"      "10" "0" "" "" ""   "true"     # 10am daily, notify
generate_plist "research-cycle"    "" "" "" "" "28800" "false"    # every 8hrs
generate_plist "introspection-cycle" "" "" "" "" "28800" "false"  # every 8hrs
generate_plist "self-introspection-cycle" "3" "0" "" "" "" "false" # 3am daily

echo ""
echo "All plists generated in $PLIST_DIR"
echo "To install:"
echo "  for f in $PLIST_DIR/*.plist; do"
echo "    cp \"\$f\" ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/\$(basename \"\$f\")"
echo "  done"

# === FEISHU PROXY (KeepAlive webhook server) ===
FEISHU_PROXY_LABEL="${LABEL_PREFIX}.feishu-proxy"
FEISHU_PROXY_FILE="$PLIST_DIR/${FEISHU_PROXY_LABEL}.plist"
PYTHON_BIN="$(command -v python3 || echo /opt/homebrew/bin/python3)"
sed \
    -e "s|__PYTHON__|${PYTHON_BIN}|g" \
    -e "s|__BRIDGE_DIR__|${BRIDGE_DIR}|g" \
    -e "s|__HOME__|${HOME}|g" \
    -e "s|__LOG_DIR__|${LOG_DIR}|g" \
    "$BRIDGE_DIR/launchd/jobs/com.onyx.claude.feishu-proxy.plist.template" > "$FEISHU_PROXY_FILE"
echo "Generated: $FEISHU_PROXY_FILE"
