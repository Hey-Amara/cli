#!/usr/bin/env python3
"""Dry-run the release workflow state machine without publishing releases.

This executes the workflow's shell blocks in a temporary local Git repository
with a fake `gh` executable. It proves the important release states while
keeping all tag/release side effects inside the temp directory. The extracted
workflow shell runs with a temporary HOME, a strict environment allowlist, and
only the local tools needed by the release steps on PATH.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

WORKFLOW = Path(os.environ.get("WORKFLOW_FILE", ".github/workflows/release.yml"))
LEGACY_WORKFLOW = Path(
    os.environ.get(
        "LEGACY_WORKFLOW_FILE",
        ".github/tests/fixtures/release-workflow-legacy.yml",
    )
)
TAG = os.environ.get("TAG", "v1.2.3")
HARNESS_TOOLS = (
    "awk",
    "bash",
    "cat",
    "chmod",
    "git",
    "grep",
    "head",
    "mktemp",
    "rm",
    "sed",
)


def extract_step_run(workflow: Path, step_name: str) -> str:
    lines = workflow.read_text().splitlines()
    needle = f"- name: {step_name}"
    for index, line in enumerate(lines):
        if line.strip() != needle:
            continue

        run_index: Optional[int] = None
        for candidate in range(index + 1, len(lines)):
            stripped = lines[candidate].strip()
            if stripped.startswith("- name: "):
                break
            if stripped == "run: |":
                run_index = candidate
                break
        if run_index is None:
            break

        run_indent = len(lines[run_index]) - len(lines[run_index].lstrip(" "))
        block: list[str] = []
        for block_line in lines[run_index + 1 :]:
            if block_line.strip():
                indent = len(block_line) - len(block_line.lstrip(" "))
                if indent <= run_indent:
                    break
                strip = min(run_indent + 2, indent)
            else:
                strip = run_indent + 2
            block.append(block_line[strip:] if len(block_line) >= strip else "")
        return "\n".join(block) + "\n"

    raise AssertionError(f"could not find run block for step {step_name!r}")


def extract_step_if(workflow: Path, step_name: str) -> Optional[str]:
    lines = workflow.read_text().splitlines()
    needle = f"- name: {step_name}"
    for index, line in enumerate(lines):
        if line.strip() != needle:
            continue

        for candidate in range(index + 1, len(lines)):
            stripped = lines[candidate].strip()
            if stripped.startswith("- name: "):
                break
            if stripped.startswith("if: "):
                return stripped.removeprefix("if: ").strip()
        return None

    raise AssertionError(f"could not find step {step_name!r}")


def assert_workflow_guards(workflow: Path) -> None:
    expected = {
        "Check tag and GitHub release state": "steps.ver.outputs.version_changed == 'true'",
        "Create and push missing release tag": (
            "steps.ver.outputs.version_changed == 'true' && "
            "steps.state.outputs.tag_exists == 'false'"
        ),
        "Create missing GitHub release": (
            "steps.ver.outputs.version_changed == 'true' && "
            "steps.state.outputs.release_exists == 'false'"
        ),
    }
    for step_name, guard in expected.items():
        assert_eq(extract_step_if(workflow, step_name), guard, f"{step_name} if guard")
    print("workflow_if_guards: ok")


def run(
    cmd: list[str],
    *,
    cwd: Optional[Path] = None,
    env: Optional[dict[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True, check=True)


def run_step(script: str, *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
        handle.write(script)
        script_path = Path(handle.name)
    try:
        return subprocess.run(["bash", str(script_path)], cwd=cwd, env=env, text=True, capture_output=True)
    finally:
        script_path.unlink(missing_ok=True)


def output_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key] = value
    return values


def assert_eq(actual: object, expected: object, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


def assert_contains(text: str, needle: str, message: str) -> None:
    if needle not in text:
        raise AssertionError(f"{message}: missing {needle!r} in {text!r}")


def git(work: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(work), *args], text=True, capture_output=True, check=check)


def reset_tag(work: Path) -> None:
    git(work, "tag", "-d", TAG, check=False)
    git(work, "push", "origin", f":refs/tags/{TAG}", check=False)


def create_tag(work: Path, commit: str) -> None:
    git(work, "tag", "-a", TAG, commit, "-m", f"Release {TAG}")
    git(work, "push", "origin", f"refs/tags/{TAG}")


def write_fake_gh(path: Path) -> None:
    path.write_text(
        r'''#!/usr/bin/env bash
set -euo pipefail
releases_dir=${FAKE_RELEASES_DIR:?}
log_file=${FAKE_COMMAND_LOG:?}

if [[ "${1:-}" == "api" ]]; then
  if [[ "${FAKE_GH_API:-}" == "500" ]]; then
    printf 'HTTP/2.0 500 Internal Server Error\n\n'
    echo 'gh: Internal Server Error (HTTP 500)' >&2
    exit 1
  fi
  endpoint=""
  for arg in "$@"; do
    case "$arg" in
      repos/*/releases/tags/*) endpoint="$arg" ;;
    esac
  done
  tag="${endpoint##*/}"
  if [[ -n "$tag" && -f "$releases_dir/$tag" ]]; then
    printf 'HTTP/2.0 200 OK\n\n{}\n'
    exit 0
  fi
  printf 'HTTP/2.0 404 Not Found\n\n{"message":"Not Found"}\n'
  echo 'gh: Not Found (HTTP 404)' >&2
  exit 1
fi

if [[ "${1:-}" == "release" && "${2:-}" == "create" ]]; then
  tag="${3:-}"
  printf 'release_create %s %s\n' "$tag" "$*" >> "$log_file"
  if [[ -f "$releases_dir/$tag" ]]; then
    echo "release already exists" >&2
    exit 1
  fi
  if [[ " $* " == *" --verify-tag "* ]] && \
    ! git ls-remote --exit-code --tags origin "refs/tags/$tag" >/dev/null 2>&1; then
    echo "tag missing remotely" >&2
    exit 1
  fi
  : > "$releases_dir/$tag"
  exit 0
fi

echo "unsupported gh args: $*" >&2
exit 2
'''
    )
    path.chmod(0o755)


def install_tool_links(bin_dir: Path) -> None:
    for tool in HARNESS_TOOLS:
        target = shutil.which(tool)
        if target is None:
            raise AssertionError(f"required test tool {tool!r} is not available")
        link = bin_dir / tool
        link.unlink(missing_ok=True)
        link.symlink_to(target)


def setup_repo(root: Path) -> tuple[Path, str]:
    origin = root / "origin.git"
    work = root / "work"
    run(["git", "init", "--bare", str(origin)])
    run(["git", "clone", str(origin), str(work)])
    git(work, "config", "user.email", "reviewer@example.com")
    git(work, "config", "user.name", "Reviewer")
    (work / "pyproject.toml").write_text('version = "1.2.3"\n')
    git(work, "add", "pyproject.toml")
    git(work, "commit", "-m", "init")
    git(work, "push", "origin", "HEAD:main")
    git(work, "checkout", "-B", "main")
    return work, git(work, "rev-parse", "HEAD").stdout.strip()


def base_env(root: Path, output: Path, sha: str) -> dict[str, str]:
    return {
        "PATH": str(root / "bin"),
        "HOME": str(root / "home"),
        "XDG_CONFIG_HOME": str(root / "xdg-config"),
        "TMPDIR": str(root),
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_GLOBAL": str(root / "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GH_TOKEN": "fake-token",
        "GITHUB_REPOSITORY": "Hey-Amara/cli",
        "GITHUB_OUTPUT": str(output),
        "GITHUB_SHA": sha,
        "RUNNER_TEMP": str(root),
        "FAKE_RELEASES_DIR": str(root / "releases"),
        "FAKE_COMMAND_LOG": str(root / "commands.log"),
        "TAG": TAG,
    }


def command_log(root: Path) -> str:
    path = root / "commands.log"
    return path.read_text() if path.exists() else ""


def run_release_case(
    root: Path,
    work: Path,
    scripts: dict[str, str],
    name: str,
    tag_present: bool,
    release_present: bool,
    sha: str,
) -> None:
    releases = root / "releases"
    reset_tag(work)
    shutil.rmtree(releases)
    releases.mkdir()
    (root / "commands.log").write_text("")
    if tag_present:
        create_tag(work, sha)
    if release_present:
        (releases / TAG).touch()

    output = root / f"{name}.outputs"
    output.unlink(missing_ok=True)
    env = base_env(root, output, sha)

    state = run_step(scripts["state"], cwd=work, env=env)
    if state.returncode:
        raise AssertionError(f"{name}: state failed unexpectedly\n{state.stderr}")
    values = output_values(output)

    tag_action = "skipped"
    release_action = "skipped"
    if values.get("tag_exists") == "false":
        tag_result = run_step(scripts["tag"], cwd=work, env=env)
        if tag_result.returncode:
            raise AssertionError(f"{name}: tag step failed\n{tag_result.stderr}")
        tag_action = "created"
    if values.get("release_exists") == "false":
        release_result = run_step(scripts["release"], cwd=work, env=env)
        if release_result.returncode:
            raise AssertionError(f"{name}: release step failed\n{release_result.stderr}")
        release_action = "created"

    expected = {
        "missing_tag_missing_release": ("false", "false", "created", "created", 1),
        "missing_tag_existing_release": ("false", "true", "created", "skipped", 0),
        "existing_tag_missing_release": ("true", "false", "skipped", "created", 1),
        "existing_tag_existing_release": ("true", "true", "skipped", "skipped", 0),
    }[name]
    assert_eq(values.get("tag_exists"), expected[0], f"{name} tag_exists")
    assert_eq(values.get("release_exists"), expected[1], f"{name} release_exists")
    assert_eq(tag_action, expected[2], f"{name} tag action")
    assert_eq(release_action, expected[3], f"{name} release action")
    assert_eq(command_log(root).count("release_create"), expected[4], f"{name} release create calls")
    if name == "existing_tag_missing_release":
        assert_contains(command_log(root), "--verify-tag", f"{name} should verify tag")

    print(
        f"{name}: tag_exists={values['tag_exists']} "
        f"release_exists={values['release_exists']} "
        f"tag_action={tag_action} "
        f"release_action={release_action}"
    )


def run_version_case(
    root: Path,
    work: Path,
    version_script: str,
    name: str,
    base_sha: str,
    head_sha: str,
    expected: str,
) -> None:
    output = root / f"{name}.outputs"
    output.unlink(missing_ok=True)
    env = base_env(root, output, head_sha)
    env["BASE_SHA"] = base_sha
    result = run_step(version_script, cwd=work, env=env)
    if result.returncode:
        raise AssertionError(f"{name}: version step failed\n{result.stderr}")
    actual = output_values(output).get("version_changed")
    assert_eq(actual, expected, f"{name} version_changed")
    print(f"{name}: version_changed={actual}")


def run_failure_case(
    root: Path,
    work: Path,
    state_script: str,
    name: str,
    original_sha: str,
    current_sha: str,
) -> None:
    reset_tag(work)
    shutil.rmtree(root / "releases")
    (root / "releases").mkdir()
    (root / "commands.log").write_text("")
    output = root / f"{name}.outputs"
    env = base_env(root, output, current_sha)
    if name == "mismatched_tag":
        create_tag(work, original_sha)
    else:
        env["FAKE_GH_API"] = "500"

    result = run_step(state_script, cwd=work, env=env)
    if result.returncode == 0:
        raise AssertionError(f"{name}: expected fail-closed state check")
    assert_eq(command_log(root).count("release_create"), 0, f"{name} should not create releases")
    expected_text = "not triggering commit" if name == "mismatched_tag" else "Could not determine GitHub release state"
    assert_contains(result.stderr, expected_text, f"{name} diagnostic")
    print(f"{name}: failed_closed=true")


def run_legacy_red_check(root: Path, work: Path, original_sha: str) -> None:
    if not LEGACY_WORKFLOW.exists():
        raise AssertionError(f"legacy workflow fixture not found: {LEGACY_WORKFLOW}")

    tagcheck = extract_step_run(LEGACY_WORKFLOW, "Check if tag already exists")
    release = extract_step_run(LEGACY_WORKFLOW, "Create tag and GitHub release")

    reset_tag(work)
    shutil.rmtree(root / "releases")
    (root / "releases").mkdir()
    (root / "commands.log").write_text("")
    create_tag(work, original_sha)
    output = root / "legacy.outputs"
    env = base_env(root, output, original_sha)
    result = run_step(tagcheck, cwd=work, env=env)
    if result.returncode:
        raise AssertionError(f"legacy tag check failed\n{result.stderr}")
    if output_values(output).get("exists") == "false":
        run_step(release, cwd=work, env=env)
    assert_eq(
        command_log(root).count("release_create"),
        0,
        "legacy workflow should skip release creation when tag exists",
    )
    assert_eq((root / "releases" / TAG).exists(), False, "legacy workflow should leave release missing")
    print("legacy_workflow_existing_tag_missing_release: failed_as_expected=true")


def main() -> None:
    assert_workflow_guards(WORKFLOW)
    scripts = {
        "version": extract_step_run(WORKFLOW, "Read version from pyproject.toml"),
        "state": extract_step_run(WORKFLOW, "Check tag and GitHub release state"),
        "tag": extract_step_run(WORKFLOW, "Create and push missing release tag"),
        "release": extract_step_run(WORKFLOW, "Create missing GitHub release"),
    }

    with tempfile.TemporaryDirectory(prefix="release-workflow-test.") as tmp:
        root = Path(tmp)
        (root / "bin").mkdir()
        (root / "home").mkdir()
        (root / "xdg-config").mkdir()
        (root / "releases").mkdir()
        (root / "gitconfig").write_text("")
        install_tool_links(root / "bin")
        write_fake_gh(root / "bin" / "gh")
        work, original_sha = setup_repo(root)

        run_release_case(root, work, scripts, "missing_tag_missing_release", False, False, original_sha)
        run_release_case(root, work, scripts, "missing_tag_existing_release", False, True, original_sha)
        run_release_case(root, work, scripts, "existing_tag_missing_release", True, False, original_sha)
        run_release_case(root, work, scripts, "existing_tag_existing_release", True, True, original_sha)

        (work / "pyproject.toml").write_text('version = "1.2.3"\n# non-version pyproject change\n')
        git(work, "add", "pyproject.toml")
        git(work, "commit", "-m", "non-version-change")
        same_version_sha = git(work, "rev-parse", "HEAD").stdout.strip()
        run_version_case(
            root,
            work,
            scripts["version"],
            "same_version_pyproject_change",
            original_sha,
            same_version_sha,
            "false",
        )

        (work / "pyproject.toml").write_text('version = "1.2.4"\n# version bump\n')
        git(work, "add", "pyproject.toml")
        git(work, "commit", "-m", "version-bump")
        current_sha = git(work, "rev-parse", "HEAD").stdout.strip()
        run_version_case(root, work, scripts["version"], "version_bump", same_version_sha, current_sha, "true")

        run_failure_case(root, work, scripts["state"], "mismatched_tag", original_sha, current_sha)
        run_failure_case(root, work, scripts["state"], "unknown_release_state", original_sha, current_sha)
        run_legacy_red_check(root, work, original_sha)


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        print(f"release workflow state-machine test failed: {exc}", file=sys.stderr)
        sys.exit(1)
