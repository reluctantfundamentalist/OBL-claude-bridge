#!/usr/bin/env bash
# Materialise *.plist files from *.plist.template by substituting $HOME and bridge install dir.
set -euo pipefail
INSTALL_DIR="${BRIDGE_DIR:-$HOME/onyx-claude-bridge}"
TPL_DIR="$INSTALL_DIR/launchd/jobs"
for tpl in "$TPL_DIR"/*.plist.template; do
  out="${tpl%.template}"
  sed -e "s|__HOME__|$HOME|g" \
      -e "s|__INSTALL_DIR__|$(basename "$INSTALL_DIR")|g" \
      "$tpl" > "$out"
  echo "wrote: $out"
done
