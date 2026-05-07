import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import click


REPO = "Hey-Amara/cli"
GIT_URL = f"git+https://github.com/{REPO}.git"


def _get_latest_version() -> str:
    """Fetch latest release tag from GitHub via gh CLI or API."""
    if shutil.which("gh"):
        result = subprocess.run(
            ["gh", "release", "view", "--repo", REPO, "--json", "tagName", "--jq", ".tagName"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().lstrip("v")

    result = subprocess.run(
        ["git", "ls-remote", "--tags", "--sort=-v:refname", f"https://github.com/{REPO}.git"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        for line in result.stdout.strip().splitlines():
            ref = line.split("refs/tags/")[-1]
            if ref.startswith("v"):
                return ref.lstrip("v")

    return ""


_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+(?:[\w.+-]*)?)")


def _resolve_binary_version(binary: str) -> str:
    """Run the heyamara binary on PATH and parse its reported version."""
    try:
        result = subprocess.run([binary, "version"], capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    match = _VERSION_RE.search(result.stdout)
    return match.group(1) if match else ""


def _find_shadowing_binaries() -> list[Path]:
    """Return every `heyamara` executable found on PATH, in PATH order."""
    found: list[Path] = []
    seen: set[Path] = set()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        candidate = Path(entry) / "heyamara"
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if candidate.is_file() and os.access(candidate, os.X_OK) and resolved not in seen:
            seen.add(resolved)
            found.append(candidate)
    return found


def _local_install_reason() -> str:
    """Return a short reason when the installed package points at local source."""
    try:
        direct_url = importlib.metadata.distribution("heyamara-cli").read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        return ""

    if not direct_url:
        return ""

    try:
        data = json.loads(direct_url)
    except json.JSONDecodeError:
        return ""

    url = data.get("url", "")
    if data.get("dir_info", {}).get("editable"):
        return f"editable install from {url}"
    if url.startswith("file://"):
        return f"local install from {url}"
    return ""


def _is_newer(candidate: str, current: str) -> bool:
    """Return True if candidate is strictly newer than current."""
    try:
        from packaging.version import InvalidVersion, Version
        try:
            return Version(candidate) > Version(current)
        except InvalidVersion:
            pass
    except ImportError:
        pass

    def segments(value: str) -> tuple[int, ...]:
        parts = []
        for part in value.split("-")[0].split("."):
            try:
                parts.append(int(part))
            except ValueError:
                parts.append(0)
        return tuple(parts)

    return segments(candidate) > segments(current)


@click.command()
@click.option("--check", is_flag=True, help="Only check for updates, don't install.")
def update(check):
    """Update heyamara CLI to the latest version.

    \b
    Examples:
      heyamara update          # Download and install latest
      heyamara update --check  # Just check if an update is available
    """
    current = importlib.metadata.version("heyamara-cli")
    click.echo(f"Current version: {current}")
    click.echo("Checking for updates...")

    latest = _get_latest_version()
    if not latest:
        click.secho("Could not fetch version info from GitHub.", fg="red")
        click.echo(f"Check manually: https://github.com/{REPO}/releases")
        raise SystemExit(1)

    local_install = _local_install_reason()
    latest_is_newer = _is_newer(latest, current)

    if not latest_is_newer and latest != current:
        click.secho(
            f"Current version {current} is newer than the latest GitHub release ({latest}).",
            fg="yellow",
        )
        if local_install:
            click.echo(f"Current install is local ({local_install}); leaving it unchanged.")
        return

    if not latest_is_newer and not local_install:
        click.secho(f"Already up to date ({current}).", fg="green")
        return

    if not latest_is_newer:
        click.echo(f"Latest GitHub version is {latest}.")
        click.secho(
            f"Current install is local ({local_install}); reinstalling the official release will replace it.",
            fg="yellow",
        )
    else:
        click.echo(f"New version available: {latest}")

    if check:
        click.echo("Run 'heyamara update' to install.")
        return

    pinned_url = f"{GIT_URL}@v{latest}"
    click.echo(f"Installing from {pinned_url}...")

    if shutil.which("pipx"):
        click.echo("Updating with pipx...")
        result = subprocess.run(
            ["pipx", "install", pinned_url, "--force"],
            capture_output=True,
            text=True,
        )
    else:
        click.echo("Updating with pip...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", pinned_url, "--quiet"],
            capture_output=True,
            text=True,
        )

    if result.returncode != 0:
        click.secho("Update failed.", fg="red")
        if result.stderr:
            click.echo(result.stderr.strip())
        raise SystemExit(1)

    # Verify by actually parsing the version output of whatever `heyamara` is on PATH.
    path_binaries = _find_shadowing_binaries()
    active_binary = path_binaries[0] if path_binaries else None
    active_version = _resolve_binary_version("heyamara") if active_binary else ""

    if active_version == latest:
        click.secho(f"Updated successfully: {current} -> {latest}", fg="green")
        return

    # Update did install (pipx/pip succeeded) but `heyamara` on PATH is still old.
    click.secho(
        f"Install succeeded, but `heyamara` on PATH still reports {active_version or 'unknown'} "
        f"(expected {latest}).",
        fg="yellow",
    )

    if len(path_binaries) > 1:
        click.secho("Multiple `heyamara` binaries found on PATH:", fg="yellow")
        for idx, path in enumerate(path_binaries):
            marker = "  <- first on PATH (wins)" if idx == 0 else ""
            ver = _resolve_binary_version(str(path)) or "?"
            click.echo(f"  {path}  [{ver}]{marker}")
        click.echo(
            "\nThe older binary is shadowing the freshly installed one. "
            "Remove it (or reorder PATH) and re-run `heyamara version`:"
        )
        click.echo(f"  rm {path_binaries[0]}")
        click.echo("  hash -r   # or restart your shell")
    else:
        click.echo("Try opening a new shell or running `hash -r`, then re-check `heyamara version`.")
