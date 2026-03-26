import importlib.metadata
import json
import shutil
import subprocess
import sys
import tempfile

import click


REPO = "heyamara/cli"


def _get_latest_release() -> dict:
    """Fetch latest release info from GitHub API."""
    result = subprocess.run(
        ["curl", "-fsSL", f"https://api.github.com/repos/{REPO}/releases/latest"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def _get_download_url(release: dict) -> str:
    """Extract the .tar.gz download URL from a release."""
    for asset in release.get("assets", []):
        if asset["name"].endswith(".tar.gz"):
            return asset["browser_download_url"]
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

    release = _get_latest_release()
    if not release:
        click.secho("Could not fetch release info from GitHub.", fg="red")
        click.echo(f"Check manually: https://github.com/{REPO}/releases")
        raise SystemExit(1)

    latest = release.get("tag_name", "").lstrip("v")
    if not latest:
        click.secho("Could not determine latest version.", fg="red")
        raise SystemExit(1)

    if latest == current:
        click.secho(f"Already up to date ({current}).", fg="green")
        return

    click.echo(f"New version available: {latest}")

    if check:
        click.echo(f"Run 'heyamara update' to install.")
        return

    download_url = _get_download_url(release)
    if not download_url:
        click.secho("No downloadable asset found in the release.", fg="red")
        click.echo(f"Install manually: https://github.com/{REPO}/releases/tag/v{latest}")
        raise SystemExit(1)

    click.echo(f"Downloading {download_url}...")

    with tempfile.TemporaryDirectory() as tmp_dir:
        tarball = f"{tmp_dir}/heyamara-cli.tar.gz"
        dl = subprocess.run(
            ["curl", "-fsSL", download_url, "-o", tarball],
            capture_output=True,
        )
        if dl.returncode != 0:
            click.secho("Download failed.", fg="red")
            raise SystemExit(1)

        # Detect installer
        if shutil.which("pipx"):
            click.echo("Installing with pipx...")
            subprocess.run(["pipx", "install", tarball, "--force"], check=True)
        else:
            click.echo("Installing with pip...")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", tarball, "--force-reinstall", "--quiet"],
                check=True,
            )

    # Verify
    result = subprocess.run(["heyamara", "version"], capture_output=True, text=True)
    if result.returncode == 0:
        click.secho(f"Updated successfully: {current} -> {latest}", fg="green")
    else:
        click.secho(f"Update installed but verification failed. Run 'heyamara version' to check.", fg="yellow")
