import importlib.metadata
import shutil
import subprocess
import sys

import click


REPO = "Hey-Amara/cli"
GIT_URL = f"git+https://github.com/{REPO}.git"


def _get_latest_version() -> str:
    """Fetch latest release tag from GitHub via gh CLI or API."""
    # Try gh CLI first (handles private repos with auth)
    if shutil.which("gh"):
        result = subprocess.run(
            ["gh", "release", "view", "--repo", REPO, "--json", "tagName", "--jq", ".tagName"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().lstrip("v")

    # Fallback to git ls-remote (works with git credentials)
    result = subprocess.run(
        ["git", "ls-remote", "--tags", "--sort=-v:refname", f"https://github.com/{REPO}.git"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        # Parse latest tag: "refs/tags/v1.0.0" -> "1.0.0"
        for line in result.stdout.strip().splitlines():
            ref = line.split("refs/tags/")[-1]
            if ref.startswith("v"):
                return ref.lstrip("v")

    return ""


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

    if latest == current:
        click.secho(f"Already up to date ({current}).", fg="green")
        return

    click.echo(f"New version available: {latest}")

    if check:
        click.echo("Run 'heyamara update' to install.")
        return

    click.echo(f"Installing from {GIT_URL}...")

    if shutil.which("pipx"):
        click.echo("Updating with pipx...")
        result = subprocess.run(
            ["pipx", "install", GIT_URL, "--force"],
            capture_output=True,
            text=True,
        )
    else:
        click.echo("Updating with pip...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", GIT_URL, "--quiet"],
            capture_output=True,
            text=True,
        )

    if result.returncode != 0:
        click.secho("Update failed.", fg="red")
        if result.stderr:
            click.echo(result.stderr.strip())
        raise SystemExit(1)

    # Verify
    verify = subprocess.run(["heyamara", "version"], capture_output=True, text=True)
    if verify.returncode == 0:
        click.secho(f"Updated successfully: {current} -> {latest}", fg="green")
    else:
        click.secho("Update installed but verification failed. Run 'heyamara version' to check.", fg="yellow")
