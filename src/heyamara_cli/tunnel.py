"""Shared tunneling + pre-flight helpers for RDS access.

Handles the friction points around SSM port forwarding:
  - Pre-flight: verifies RDS IAM auth is enabled before we try to use it
  - TCP probe: after SSM tunnel starts, confirms RDS is actually reachable
  - Background tunnel: opens SSM session as a subprocess and tears down on exit

All functions are idempotent and fail-fast with clear error messages.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.parse
from typing import Optional

import click

from heyamara_cli.helpers import run


# Connection timeout baked into every DATABASE_URL we emit.
# Makes psql fail fast instead of silently hanging on network/auth issues.
CONNECT_TIMEOUT_SECONDS = 10


def preflight_rds_iam_enabled(
    rds_host: str,
    environment: str,
    profile: str,
    region: str,
) -> bool:
    """Check if IAM database authentication is enabled on the RDS cluster.

    Returns True if enabled, False otherwise. Prints a clear error on False so the
    caller can fail fast before starting a tunnel that would silently hang.
    """
    # Resolve the cluster_id from the endpoint hostname
    # e.g. heyamara-staging-instance.xxx.rds.amazonaws.com → describe by endpoint
    result = run(
        [
            "aws", "rds", "describe-db-clusters",
            "--query",
            f"DBClusters[?TagList[?Key=='Environment' && Value=='{environment}']]"
            ".{id: DBClusterIdentifier, iam: IAMDatabaseAuthenticationEnabled} | [0]",
            "--output", "json",
            "--region", region,
            "--profile", profile,
        ],
        capture=True,
        check=False,
        environment=environment,
    )

    try:
        data = json.loads(result.stdout.strip())
        if not data or data.get("iam") is None:
            click.secho(
                f"Could not determine IAM auth status for {environment} RDS cluster.",
                fg="yellow",
            )
            return True  # fail open — let psql handle the error

        if data["iam"]:
            return True

        click.secho(
            "\nERROR: IAM database authentication is not enabled on this RDS cluster.",
            fg="red",
            bold=True,
        )
        click.echo(f"  Cluster: {data['id']}")
        click.echo("  Enable it by setting in Terraform:")
        click.echo("    rds_iam_database_authentication_enabled = true")
        click.echo("  Or connect without --iam using the master password.")
        return False
    except (json.JSONDecodeError, KeyError, TypeError):
        # Can't parse — fail open rather than blocking
        return True


def wait_for_tcp(host: str, port: int, timeout: float = 5.0) -> bool:
    """Probe a TCP port until it accepts connections or the timeout expires.

    Returns True if the port becomes reachable within the timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except (ConnectionRefusedError, OSError, socket.timeout):
            time.sleep(0.3)
    return False


def start_tunnel_background(
    instance_id: str,
    remote_host: str,
    remote_port: int,
    local_port: int,
    profile: str,
    region: str,
) -> subprocess.Popen:
    """Open an SSM port-forwarding session as a detached subprocess.

    Registers an atexit handler to kill the session on process exit, including
    on SIGINT/SIGTERM. Returns the Popen handle so the caller can wait or kill
    explicitly.
    """
    params = json.dumps({
        "host": [remote_host],
        "portNumber": [str(remote_port)],
        "localPortNumber": [str(local_port)],
    })

    # start_new_session so Ctrl+C in the parent doesn't immediately murder the
    # SSM agent before we have a chance to tidy up
    proc = subprocess.Popen(
        [
            "aws", "ssm", "start-session",
            "--target", instance_id,
            "--document-name", "AWS-StartPortForwardingSessionToRemoteHost",
            "--parameters", params,
            "--region", region,
            "--profile", profile,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    def _cleanup():
        if proc.poll() is None:
            try:
                # Kill the whole process group (start-session spawns plugin)
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                proc.wait(timeout=3)
            except (ProcessLookupError, subprocess.TimeoutExpired, OSError):
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass

    atexit.register(_cleanup)

    # Also handle SIGINT / SIGTERM so Ctrl+C cleans up
    def _signal_handler(signum, frame):
        _cleanup()
        raise SystemExit(130 if signum == signal.SIGINT else 143)

    try:
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
    except ValueError:
        # signal() only works on main thread; caller ran us from a thread
        pass

    return proc


def open_tunnel_and_probe(
    instance_id: str,
    remote_host: str,
    remote_port: int,
    local_port: int,
    profile: str,
    region: str,
    probe_timeout: float = 8.0,
) -> subprocess.Popen:
    """Start tunnel + probe port + return handle. Fails with clear error if unreachable.

    This is the main entry point callers should use. It handles the full
    'did the tunnel actually work' check.
    """
    proc = start_tunnel_background(
        instance_id, remote_host, remote_port, local_port, profile, region
    )

    click.echo(f"Waiting for tunnel to localhost:{local_port}...")
    if not wait_for_tcp("localhost", local_port, timeout=probe_timeout):
        # Collect any stderr the SSM plugin printed
        stderr = b""
        try:
            stderr = proc.stderr.read() if proc.stderr else b""
        except Exception:
            pass
        click.secho(
            f"\nERROR: Tunnel on localhost:{local_port} is not reachable after "
            f"{probe_timeout}s.",
            fg="red",
            bold=True,
        )
        click.echo("  Possible causes:")
        click.echo("    - RDS security group does not allow traffic from the EKS node")
        click.echo("    - RDS and EKS are in different VPCs")
        click.echo("    - SSM session failed to start (check your AWS session)")
        click.echo("    - Another process is already using this port")
        if stderr:
            click.echo(f"\n  SSM plugin stderr:\n    {stderr.decode(errors='replace').strip()}")
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        raise SystemExit(1)

    click.secho("✓ Tunnel ready", fg="green")
    return proc


def build_database_url(
    user: str,
    password: str,
    host: str,
    port: int,
    dbname: str,
    sslmode: str = "require",
    connect_timeout: Optional[int] = None,
) -> str:
    """Build a PostgreSQL connection URL with sensible defaults.

    Always includes connect_timeout to avoid silent hangs when the database
    or tunnel is misconfigured.
    """
    encoded_pw = urllib.parse.quote(password, safe="")
    timeout = connect_timeout if connect_timeout is not None else CONNECT_TIMEOUT_SECONDS
    return (
        f"postgresql://{user}:{encoded_pw}@{host}:{port}/{dbname}"
        f"?sslmode={sslmode}"
        f"&connect_timeout={timeout}"
        f"&application_name=heyamara-cli"
    )


def probe_iam_auth(
    local_port: int,
    db_user: str,
    db_name: str,
    token: str,
    timeout: float = 5.0,
) -> tuple[bool, str]:
    """Probe RDS IAM auth via psql before handing the user a foreground tunnel.

    Returns (ok, error_message). ok=True on a successful login (we exit cleanly
    with `\\q`), ok=False with a human-readable message on auth/connection
    failure. Skips silently and returns ok=True if psql isn't on PATH.

    The point of this probe is to surface the "DB role missing rds_iam grant"
    case — which RDS reports as a generic `password authentication failed`
    that's easy to mistake for a wrong password or permissions bug.
    """
    if not shutil.which("psql"):
        return True, ""

    env = dict(os.environ)
    env["PGPASSWORD"] = token
    # Suppress libpq's IPv6-first ::1 attempt; SSM port-forwarding only binds
    # 127.0.0.1 and the resulting "connection refused" line is noise.
    try:
        result = subprocess.run(
            [
                "psql",
                "-h", "127.0.0.1",
                "-p", str(local_port),
                "-U", db_user,
                "-d", db_name,
                "-v", "ON_ERROR_STOP=1",
                "--set", "sslmode=require",
                "-At",
                "-c", "SELECT 1",
            ],
            env=env,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"psql probe timed out after {timeout:.0f}s"
    except OSError as exc:
        return False, f"psql probe could not start: {exc}"

    if result.returncode == 0:
        return True, ""

    stderr = (result.stderr or b"").decode(errors="replace").strip()
    return False, stderr


def generate_rds_auth_token(
    rds_host: str,
    rds_port: int,
    db_user: str,
    profile: str,
    region: str,
) -> str:
    """Generate an IAM auth token for RDS. Clear error on failure."""
    result = run(
        [
            "aws", "rds", "generate-db-auth-token",
            "--hostname", rds_host,
            "--port", str(rds_port),
            "--username", db_user,
            "--region", region,
            "--profile", profile,
        ],
        capture=True,
        check=False,
    )

    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        click.secho("Failed to generate IAM auth token.", fg="red", bold=True)
        click.secho(
            "Make sure your IAM role has rds-db:connect permission for the target DB user.",
            fg="yellow",
        )
        if result.stderr:
            click.echo(f"  AWS CLI error: {result.stderr.strip()}")
        raise SystemExit(1)

    return token
