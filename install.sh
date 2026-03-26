#!/usr/bin/env bash
# Slackd — macOS Slack notification organiser
# Install with:
#   curl -fsSL https://raw.githubusercontent.com/chaitanyakdukkipaty/Slackd/main/install.sh | bash

set -euo pipefail

REPO="https://github.com/chaitanyakdukkipaty/Slackd.git"
INSTALL_DIR="$HOME/.slackd"
BIN_DIR="$HOME/.local/bin"
CMD_NAME="slackd"

# ── colours ────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${GREEN}==>${RESET} ${BOLD}$*${RESET}"; }
warn()    { echo -e "${YELLOW}Warning:${RESET} $*"; }
error()   { echo -e "${RED}Error:${RESET} $*" >&2; exit 1; }

# ── pre-flight checks ──────────────────────────────────────────────────────
[[ "$(uname)" == "Darwin" ]] || error "Slackd requires macOS."

OS_VERSION=$(sw_vers -productVersion)
MAJOR=$(echo "$OS_VERSION" | cut -d. -f1)
[[ "$MAJOR" -ge 13 ]] || error "Slackd requires macOS 13 (Ventura) or later. Found: $OS_VERSION"

# Python 3.9+
PYTHON=""
for py in python3 python3.12 python3.11 python3.10 python3.9; do
  if command -v "$py" &>/dev/null; then
    VER=$("$py" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    MAJ=$(echo "$VER" | cut -d. -f1)
    MIN=$(echo "$VER" | cut -d. -f2)
    if [[ "$MAJ" -ge 3 && "$MIN" -ge 9 ]]; then
      PYTHON="$py"
      break
    fi
  fi
done
[[ -n "$PYTHON" ]] || error "Python 3.9+ is required. Install with: brew install python@3.12"

# git
command -v git &>/dev/null || error "git is required. Install Xcode Command Line Tools: xcode-select --install"

info "Installing Slackd..."
echo "  Install dir : $INSTALL_DIR"
echo "  Python      : $PYTHON ($VER)"
echo "  macOS       : $OS_VERSION"
echo ""

# ── clone / update ─────────────────────────────────────────────────────────
if [[ -d "$INSTALL_DIR/.git" ]]; then
  info "Updating existing installation..."
  git -C "$INSTALL_DIR" pull --ff-only
else
  info "Cloning repository..."
  git clone "$REPO" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# ── virtual environment ────────────────────────────────────────────────────
info "Setting up Python environment..."
if [[ ! -d ".venv" ]]; then
  "$PYTHON" -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
info "Dependencies installed."

# ── data directory ─────────────────────────────────────────────────────────
mkdir -p data

# ── slackd command ─────────────────────────────────────────────────────────
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/$CMD_NAME" << EOF
#!/usr/bin/env bash
# Slackd launcher
exec "$INSTALL_DIR/.venv/bin/python3" "$INSTALL_DIR/main.py" "\$@"
EOF
chmod +x "$BIN_DIR/$CMD_NAME"

# Ensure ~/. local/bin is in PATH (add to shell profile if not already)
for profile in "$HOME/.zprofile" "$HOME/.bash_profile" "$HOME/.profile"; do
  if [[ -f "$profile" ]] || [[ "$profile" == "$HOME/.zprofile" ]]; then
    if ! grep -q "$BIN_DIR" "$profile" 2>/dev/null; then
      echo "" >> "$profile"
      echo "# Added by Slackd installer" >> "$profile"
      echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$profile"
      info "Added ~/.local/bin to PATH in $profile"
    fi
    break
  fi
done

# ── done ──────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}✓ Slackd installed successfully!${RESET}"
echo ""
echo -e "${BOLD}Required: Grant Accessibility permission${RESET}"
echo "  1. Open System Settings → Privacy & Security → Accessibility"
echo "  2. Enable Terminal (or your terminal app)"
echo "  This allows Slackd to read notification content."
echo ""
echo -e "${BOLD}Quick start:${RESET}"
echo "  slackd              # start the menu bar app"
echo ""
echo -e "${BOLD}Or run directly:${RESET}"
echo "  cd $INSTALL_DIR && .venv/bin/python3 main.py"
echo ""
echo -e "${YELLOW}Tip:${RESET} Once running, enable 'Launch at Login' in the menu bar:"
echo "  🔔 menu → ⚙ Settings → Launch at Login"
echo ""
