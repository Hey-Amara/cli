"""Non-blocking update check — runs in background, caches result for 24h."""

from __future__ import annotations

import importlib.metadata
import json
import os
import subprocess
import shutil
import time
from pathlib import Path

import click

CACHE_DIR = Path.home() / ".heyamara"
CACHE_FILE = CACHE_DIR / ".update-check"
CHECK_INTERVAL = 86400  # 24 hours
REPO = "Hey-Amara/cli"


def _read_cache() -> dict:
    """Read the cached version check result."""
    try:
        if CACHE_FILE.exists():
            with open(CACHE_FILE) as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _write_cache(latest: str) -> None:
    """Write a version check result to cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump({"latest": latest, "checked_at": time.time()}, f)


def _fetch_latest_version() -> str:
    """Fetch latest release tag from GitHub (quick, silent)."""
    try:
        if shutil.which("gh"):
            result = subprocess.run(
                ["gh", "release", "view", "--repo", REPO, "--json", "tagName", "--jq", ".tagName"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().lstrip("v")
    except (subprocess.TimeoutExpired, OSError):
        pass

    try:
        result = subprocess.run(
            ["git", "ls-remote", "--tags", "--sort=-v:refname", f"https://github.com/{REPO}.git"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                ref = line.split("refs/tags/")[-1]
                if ref.startswith("v"):
                    return ref.lstrip("v")
    except (subprocess.TimeoutExpired, OSError):
        pass

    return ""


def check_and_notify() -> None:
    """Check for updates and print a notification if a newer version exists.

    - Reads from cache if checked within the last 24 hours.
    - Fetches from GitHub otherwise (with 5s timeout — won't block the CLI).
    - Prints a single yellow line if an update is available, then continues.
    """
    try:
        current = importlib.metadata.version("heyamara-cli")
    except importlib.metadata.PackageNotFoundError:
        return

    cache = _read_cache()
    now = time.time()

    # Use cache if fresh enough
    if cache.get("checked_at") and (now - cache["checked_at"]) < CHECK_INTERVAL:
        latest = cache.get("latest", "")
    else:
        # Fetch and cache
        latest = _fetch_latest_version()
        if latest:
            _write_cache(latest)

    if latest and _is_newer(latest, current):
        click.secho(
            f"\n  Update available: {current} → {latest}  —  run `heyamara update` to upgrade\n",
            fg="yellow",
            err=True,
        )


def _is_newer(candidate: str, current: str) -> bool:
    """Return True if `candidate` is strictly newer than `current`.

    Uses packaging.version when available (handles pre-releases, build metadata),
    falls back to a simple numeric tuple compare for stripped-down environments.
    """
    try:
        from packaging.version import InvalidVersion, Version
        try:
            return Version(candidate) > Version(current)
        except InvalidVersion:
            pass
    except ImportError:
        pass

    # Fallback: compare numeric segments only (1.5.0 vs 1.6.0 etc.)
    def _segments(v: str) -> tuple:
        parts = []
        for p in v.split("-")[0].split("."):  # ignore pre-release suffixes
            try:
                parts.append(int(p))
            except ValueError:
                parts.append(0)
        return tuple(parts)

    return _segments(candidate) > _segments(current)
