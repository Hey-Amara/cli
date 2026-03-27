#!/usr/bin/env bash
# =============================================================================
# HeyAmara CLI installer
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Hey-Amara/cli/main/install.sh | bash
#
# Or with a specific version:
#   curl -fsSL ... | bash -s -- v1.0.0
# =============================================================================
set -euo pipefail

REPO="Hey-Amara/cli"
INSTALL_DIR="${HOME}/.local/bin"
VERSION="${1:-latest}"

info()  { echo "[heyamara] $*"; }
error() { echo "[heyamara] ERROR: $*" >&2; exit 1; }

# Check Python 3
if ! command -v python3 &>/dev/null; then
    error "Python 3 is required. Install it first:
  macOS:  brew install python3
  Linux:  sudo apt-get install python3 python3-pip"
fi

# Prefer pipx for isolated install, fallback to pip
if command -v pipx &>/dev/null; then
    INSTALLER="pipx"
elif command -v pip3 &>/dev/null; then
    INSTALLER="pip3"
elif python3 -m pip --version &>/dev/null 2>&1; then
    INSTALLER="python3 -m pip"
else
    error "pip or pipx is required. Install one:
  macOS:  brew install pipx
  Linux:  python3 -m ensurepip --user"
fi

# Determine download URL
if [ "$VERSION" = "latest" ]; then
    info "Fetching latest release..."
    DOWNLOAD_URL=$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
        | grep '"browser_download_url".*\.tar\.gz"' \
        | head -1 \
        | sed 's/.*"browser_download_url": "\(.*\)"/\1/')
else
    DOWNLOAD_URL="https://github.com/${REPO}/releases/download/${VERSION}/heyamara_cli-${VERSION#v}.tar.gz"
fi

if [ -z "$DOWNLOAD_URL" ]; then
    error "Could not find a release to download. Check https://github.com/${REPO}/releases"
fi

info "Downloading from ${DOWNLOAD_URL}..."
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

curl -fsSL "$DOWNLOAD_URL" -o "${TMP_DIR}/heyamara-cli.tar.gz"

info "Installing with ${INSTALLER}..."
if [ "$INSTALLER" = "pipx" ]; then
    pipx install "${TMP_DIR}/heyamara-cli.tar.gz" --force
else
    $INSTALLER install "${TMP_DIR}/heyamara-cli.tar.gz" --force-reinstall --quiet
fi

# Verify installation
if command -v heyamara &>/dev/null; then
    info "Installed successfully: $(heyamara version)"
    info ""
    info "Get started:"
    info "  heyamara setup                     # Install required tools"
    info "  heyamara config set aws_profile    # Set your AWS profile"
    info "  heyamara help                      # See all commands"
else
    info "Installed, but 'heyamara' is not on your PATH."
    info "Add this to your shell profile:"
    info "  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi
