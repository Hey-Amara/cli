"""Resolve the latest published CLI version from canonical release tags."""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import Optional


# Canonical stable release tags only: `vMAJOR.MINOR.PATCH`. Each component is an
# ASCII, leading-zero-free integer, bounded in length. `[0-9]` (not `\d`) keeps
# Unicode digits like `v١.٢.٣` out; `0|[1-9][0-9]{0,8}` rejects non-canonical
# leading zeros (`v01.2.3`) and caps the digit run so a pathological tag can't
# feed a multi-thousand-digit string into int() and crash the resolver.
_STABLE_VERSION_TAG_RE = re.compile(
    r"^v(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})$"
)


def canonical_release_version(tag: str) -> str:
    """Return the version from a canonical stable release tag.

    Update installation targets are constructed as ``@v<version>``, so bare
    versions are intentionally rejected. Prereleases are also excluded to keep
    the Git fallback consistent with GitHub's latest stable release behavior.
    """
    match = _STABLE_VERSION_TAG_RE.fullmatch(tag.strip())
    return ".".join(match.groups()) if match else ""


def parse_git_tag_output(output: str) -> str:
    """Return the greatest stable SemVer from ls-remote output.

    Annotated tags normally produce both ``refs/tags/vX.Y.Z`` and a peeled
    ``refs/tags/vX.Y.Z^{}`` pseudo-ref. The latter is not an installable tag and
    must never be exposed as a version. Selection does not trust Git's version
    sort because its prerelease ordering differs from SemVer precedence.
    """
    candidates = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue

        ref = fields[1]
        prefix = "refs/tags/"
        if not ref.startswith(prefix) or ref.endswith("^{}"):
            continue

        version = canonical_release_version(ref.removeprefix(prefix))
        if version:
            candidates.append((tuple(int(part) for part in version.split(".")), version))

    return max(candidates)[1] if candidates else ""


def _run(command: list[str], timeout: Optional[float]) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def fetch_latest_release_version(repo: str, timeout: Optional[float] = None) -> str:
    """Resolve the latest release through GitHub CLI, then a Git fallback."""
    if shutil.which("gh"):
        result = _run(
            ["gh", "release", "view", "--repo", repo, "--json", "tagName", "--jq", ".tagName"],
            timeout,
        )
        if result and result.returncode == 0:
            version = canonical_release_version(result.stdout)
            if version:
                return version

    result = _run(
        [
            "git",
            "ls-remote",
            "--refs",
            "--tags",
            "--sort=-v:refname",
            f"https://github.com/{repo}.git",
        ],
        timeout,
    )
    if result and result.returncode == 0:
        return parse_git_tag_output(result.stdout)

    return ""
