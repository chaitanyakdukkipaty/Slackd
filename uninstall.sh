#!/usr/bin/env bash
# Uninstall Slackd

set -euo pipefail

INSTALL_DIR="$HOME/.slackd"
BIN_DIR="$HOME/.local/bin"
CMD_NAME="slackd"
PLIST="$HOME/Library/LaunchAgents/com.slackorganizer.plist"

RED='\033[0;31m'; GREEN='\033[0;32m'; BOLD='\033[1m'; RESET='\033[0m'
info()  { echo -e "${GREEN}==>${RESET} ${BOLD}$*${RESET}"; }
error() { echo -e "${RED}Error:${RESET} $*" >&2; exit 1; }

# Stop and remove LaunchAgent
if [[ -f "$PLIST" ]]; then
  info "Removing LaunchAgent..."
  launchctl unload -w "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
fi

# Remove slackd command
if [[ -f "$BIN_DIR/$CMD_NAME" ]]; then
  info "Removing slackd command..."
  rm -f "$BIN_DIR/$CMD_NAME"
fi

# Remove install directory
if [[ -d "$INSTALL_DIR" ]]; then
  info "Removing $INSTALL_DIR ..."
  rm -rf "$INSTALL_DIR"
fi

echo ""
echo -e "${GREEN}${BOLD}✓ Slackd uninstalled.${RESET}"
echo "  You can also remove Accessibility permission for Terminal in"
echo "  System Settings → Privacy & Security → Accessibility."
echo ""
