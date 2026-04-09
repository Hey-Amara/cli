from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys

import click


# ---- Verbose / debug mode ---------------------------------------------------

_verbose: bool = False


def set_verbose(value: bool) -> None:
    """Enable or disable verbose debug output."""
    global _verbose
    _verbose = value


def debug(msg: str) -> None:
    """Print a debug message when verbose mode is active."""
    if _verbose:
        click.secho(f"[debug] {msg}", fg="bright_black", err=True)


# ---- Port utilities ---------------------------------------------------------


def check_port_free(port: int) -> bool:
    """Return True if the local TCP port is available."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) != 0


# ---- IAM role detection -----------------------------------------------------


def detect_iam_role(caller_arn: str) -> str | None:
    """Extract the role name from an assumed-role ARN.

    ARN format: arn:aws:sts::ACCOUNT:assumed-role/ROLE_NAME/SESSION
    Returns the ROLE_NAME segment, or None if not an assumed-role ARN.
    """
    if not caller_arn:
        return None
    parts = caller_arn.split(":")
    if len(parts) < 6:
        return None
    resource = parts[5]  # e.g. "assumed-role/power_user/session-name"
    segments = resource.split("/")
    if len(segments) >= 2 and segments[0] == "assumed-role":
        return segments[1]
    return None


# ---- AWS error handling -----------------------------------------------------


def _is_access_denied(text: str) -> bool:
    """Check if an error message indicates an access denied issue."""
    if not text:
        return False
    denied_patterns = [
        "AccessDenied",
        "AccessDeniedException",
        "UnauthorizedAccess",
        "is not authorized to perform",
        "not authorized to perform",
        "An error occurred (403)",
        "Forbidden",
    ]
    return any(p in text for p in denied_patterns)


def _format_access_denied(stderr: str, environment: str = "") -> str:
    """Return a helpful error message for AWS access denied errors."""
    lines = [
        click.style("Access Denied", fg="red", bold=True),
        "",
    ]

    if environment:
        lines.append(f"  You do not have permission to access the '{environment}' environment.")
        lines.append("")
        lines.append("  This is likely because your AWS SSO role does not include")
        lines.append(f"  the required permissions for {environment} resources.")
    else:
        lines.append("  Your AWS role does not have permission for this action.")

    lines.append("")
    lines.append("  Troubleshooting:")
    lines.append("    1. Check your active profile:  heyamara config get aws_profile")
    lines.append("    2. Re-login if needed:         heyamara login")
    lines.append("    3. Contact your admin to request the appropriate permission set")
    lines.append("       in AWS Identity Center (SSO) for this environment.")

    # Include the original error for debugging
    if stderr:
        lines.append("")
        lines.append(click.style("  AWS error:", dim=True))
        for line in stderr.strip().splitlines()[:3]:
            lines.append(click.style(f"    {line}", dim=True))

    return "\n".join(lines)


def run(
    cmd: list[str],
    capture: bool = False,
    check: bool = True,
    environment: str = "",
    **kwargs,
) -> subprocess.CompletedProcess:
    """Run a shell command, streaming output by default.

    Args:
        cmd: Command and arguments.
        capture: If True, capture stdout/stderr instead of streaming.
        check: If True, exit on non-zero return code.
        environment: Optional environment name for contextual error messages.
    """
    debug(f"Running: {' '.join(str(c) for c in cmd)}")
    if capture:
        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("text", True)
    try:
        result = subprocess.run(cmd, check=False, **kwargs)

        if result.returncode != 0:
            stderr = result.stderr if capture and result.stderr else ""

            if _is_access_denied(stderr):
                click.echo(_format_access_denied(stderr, environment))
                sys.exit(1)

            if check:
                if capture and stderr:
                    click.secho(stderr.strip(), fg="red")
                sys.exit(result.returncode)

        return result
    except FileNotFoundError:
        click.secho(f"Command not found: {cmd[0]}", fg="red")
        sys.exit(1)


def check_tool(name: str) -> bool:
    """Check if a CLI tool is available on PATH."""
    return shutil.which(name) is not None


def require_tool(name: str, install_hint: str = "") -> None:
    """Exit with error if a tool is not installed."""
    if not check_tool(name):
        msg = f"{name} is not installed."
        if install_hint:
            msg += f" Install: {install_hint}"
        click.secho(msg, fg="red")
        sys.exit(1)


def require_aws_session(profile: str) -> str:
    """Verify AWS credentials are valid. Auto-initiates SSO login if expired.

    Returns the caller ARN string (empty string on parse failure).
    """
    require_tool("aws")
    result = run(
        ["aws", "sts", "get-caller-identity", "--profile", profile],
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        click.secho("AWS session expired. Initiating SSO login...", fg="yellow")
        run(["aws", "sso", "login", "--profile", profile])
        # Verify again after login
        result = run(
            ["aws", "sts", "get-caller-identity", "--profile", profile],
            capture=True,
            check=False,
        )
        if result.returncode != 0:
            click.secho("AWS login failed. Check your AWS config.", fg="red")
            sys.exit(1)
        click.secho("AWS login successful.", fg="green")

    try:
        identity = json.loads(result.stdout)
        arn = identity.get("Arn", "")
        debug(f"Caller ARN: {arn}")
        return arn
    except (json.JSONDecodeError, AttributeError):
        return ""
