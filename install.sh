#!/usr/bin/env bash
# =============================================================================
# HeyAmara CLI installer
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Hey-Amara/cli/main/install.sh | bash
#
# Install a specific version:
#   curl -fsSL ... | bash -s -- v1.4.0
# =============================================================================
set -euo pipefail

REPO="Hey-Amara/cli"
VERSION="${1:-}"

if [ -n "$VERSION" ]; then
    GIT_URL="git+https://github.com/${REPO}.git@${VERSION}"
    info_version="$VERSION"
else
    GIT_URL="git+https://github.com/${REPO}.git"
    info_version="latest"
fi

info()  { echo "[heyamara] $*"; }
error() { echo "[heyamara] ERROR: $*" >&2; exit 1; }

# Check Python 3
if ! command -v python3 &>/dev/null; then
    error "Python 3.9+ is required. Install it first:
  macOS:  brew install python3
  Linux:  sudo apt-get install python3 python3-pip"
fi

# Prefer pipx for isolated install, fallback to pip
info "Installing heyamara-cli ($info_version)..."

if command -v pipx &>/dev/null; then
    info "Using pipx..."
    pipx install "$GIT_URL" --force
elif command -v pip3 &>/dev/null; then
    info "Using pip3..."
    pip3 install "$GIT_URL" --quiet
elif python3 -m pip --version &>/dev/null 2>&1; then
    info "Using pip..."
    python3 -m pip install "$GIT_URL" --quiet
else
    error "pip or pipx is required. Install one:
  macOS:  brew install pipx
  Linux:  python3 -m ensurepip --user"
fi

# Verify
if command -v heyamara &>/dev/null; then
    info "Installed: $(heyamara version)"
    info ""
    info "Get started:"
    info "  heyamara setup                     # Install required tools"
    info "  heyamara config set aws_profile    # Set your AWS profile"
    info "  heyamara config set grafana_token  # Set Grafana token for log search"
    info "  heyamara doctor                    # Verify everything works"
else
    info "Installed, but 'heyamara' is not on your PATH."
    info "Add this to your shell profile:"
    info "  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi
