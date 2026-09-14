"""Shared tunneling + pre-flight helpers for RDS access.

Handles the friction points around SSM port forwarding:
  - Pre-flight: verifies RDS IAM auth is enabled before we try to use it
  - TCP probe: after SSM tunnel starts, confirms RDS is actually reachable
  - Background tunnel: opens SSM session as a subprocess and tears down on exit

All functions are idempotent and fail-fast with clear error messages.
"""

from __future__ import annotations

import atexit
import getpass
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.parse
from collections import deque
from typing import Optional

import click

from heyamara_cli.helpers import _format_access_denied, _is_access_denied, run


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


# The session-manager-plugin prints this verbatim on stdout the moment the
# session is created (before the data channel opens), so it is captured even for
# a session that then fails on a saturated node. Hard-coded in the plugin's Go
# source (no i18n) — the authoritative id, no describe-sessions guessing needed.
_SESSION_ID_RE = re.compile(r"Starting session with SessionId:\s*(\S+)")


def _safe_echo(msg: str) -> None:
    """click.echo that never raises — stdout may be gone during shutdown/SIGHUP."""
    try:
        click.echo(msg)
    except Exception:
        pass


def filter_ssm_online(instance_ids: list, profile: str, region: str) -> list:
    """Return the subset of instance_ids whose SSM agent is Online.

    Best-effort: avoids picking a freshly-booted or draining node whose agent
    hasn't registered (→ TargetNotConnected). Falls back to the full list if the
    check errors or matches nothing — better to try a node than fail selection.
    """
    if not instance_ids:
        return instance_ids
    try:
        r = subprocess.run(
            ["aws", "ssm", "describe-instance-information",
             "--filters", "Key=PingStatus,Values=Online",
             "--query", "InstanceInformationList[].InstanceId",
             "--output", "text", "--region", region, "--profile", profile],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        return instance_ids
    online = set(r.stdout.split())
    subset = [i for i in instance_ids if i in online]
    return subset or instance_ids


def _terminate_session(session_id: str, profile: str, region: str) -> None:
    """Terminate an SSM session server-side. Authoritative close + bounded; never
    raises. Killing the local plugin does not free the node's session slot, and
    the plugin's own SIGTERM-triggered terminate is unreliable with SSO creds on
    some versions — which is how sessions orphan and pile onto a node.
    """
    _safe_echo(f"Terminating SSM session {session_id}...")
    manual = (
        f"  Close it manually: aws ssm terminate-session --session-id "
        f"{session_id} --region {region} --profile {profile}"
    )
    try:
        r = subprocess.run(
            ["aws", "ssm", "terminate-session", "--session-id", session_id,
             "--region", region, "--profile", profile,
             "--cli-connect-timeout", "3", "--cli-read-timeout", "5"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        _safe_echo(f"  Could not terminate session {session_id} ({exc}).\n{manual}")
        return
    if r.returncode != 0:
        first = (r.stderr or "").strip().splitlines()
        detail = f": {first[0]}" if first else ""
        _safe_echo(f"  Could not terminate session {session_id}{detail}.\n{manual}")


def spawn_port_forward(
    instance_id: str,
    remote_host: str,
    remote_port: int,
    local_port: int,
    profile: str,
    region: str,
    echo: bool = False,
) -> tuple:
    """Open an SSM port-forwarding session and guarantee it is torn down — local
    process AND server-side session — on exit (atexit + SIGINT/SIGTERM/SIGHUP).

    The real SessionId is captured from the plugin's stdout so we terminate
    exactly our own session, even on a saturated node or a fast Ctrl+C. Returns
    (proc, state); state["cleanup"] is an idempotent teardown callable and
    state["lines"] holds recent plugin output for diagnostics.
    """
    params = json.dumps({
        "host": [remote_host],
        "portNumber": [str(remote_port)],
        "localPortNumber": [str(local_port)],
    })
    # Attribute the session so any future orphan is traceable in describe-sessions.
    reason = f"heyamara-cli {getpass.getuser()}@{socket.gethostname()} pid={os.getpid()}"

    proc = subprocess.Popen(
        [
            "aws", "ssm", "start-session",
            "--target", instance_id,
            "--document-name", "AWS-StartPortForwardingSessionToRemoteHost",
            "--parameters", params,
            "--reason", reason,
            "--region", region,
            "--profile", profile,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
        # Own session/group: the terminal's SIGINT/SIGHUP reach only us, and we
        # kill the child group ourselves after tidy-up (pgid == pid here).
        start_new_session=True,
    )

    state: dict = {"session_id": None, "done": False, "lines": deque(maxlen=30)}

    def _reader():
        # Parse the SessionId from the first matching line and keep draining to
        # EOF — the plugin prints a line per accepted connection, so a stalled
        # reader would fill the pipe and block it.
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                state["lines"].append(line)
                if state["session_id"] is None:
                    m = _SESSION_ID_RE.search(line)
                    if m:
                        state["session_id"] = m.group(1)
                if echo:
                    _safe_echo(line)
        except Exception:
            return

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    state["reader"] = reader

    def _cleanup(*_args):
        if state["done"]:
            return
        state["done"] = True
        # Terminate server-side FIRST: it frees the scarce node session slot and
        # makes the plugin exit promptly, so the killpg below usually no-ops.
        sid = state.get("session_id")
        if sid:
            _terminate_session(sid, profile, region)
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=3)
            except (ProcessLookupError, subprocess.TimeoutExpired, OSError):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass

    atexit.register(_cleanup)
    state["cleanup"] = _cleanup

    # SIGHUP (terminal close), SIGTERM (kill), SIGINT (Ctrl+C): close the session
    # server-side, then chain any handler already installed (e.g. the rabbitmq
    # /etc/hosts cleanup) rather than clobbering it.
    def _make_handler(prev):
        def _handler(signum, frame):
            _cleanup()
            if callable(prev):
                prev(signum, frame)
            else:
                raise SystemExit(128 + signum)
        return _handler

    for _sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(_sig, _make_handler(signal.getsignal(_sig)))
        except (ValueError, OSError, AttributeError):
            # not the main thread, or the platform lacks this signal
            pass

    return proc, state


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
    'did the tunnel actually work' check. The session is always terminated
    server-side on exit (spawn_port_forward wires up atexit + signal cleanup).
    """
    proc, state = spawn_port_forward(
        instance_id, remote_host, remote_port, local_port, profile, region, echo=False
    )

    click.echo(f"Waiting for tunnel to localhost:{local_port}...")
    deadline = time.time() + probe_timeout
    ready = False
    while time.time() < deadline:
        if wait_for_tcp("localhost", local_port, timeout=0.5):
            ready = True
            break
        # Bail early if start-session already exited (bad target / auth / port
        # in use) instead of burning the full probe_timeout.
        if proc.poll() is not None:
            break

    if not ready:
        state["reader"].join(timeout=1)  # let the last plugin lines land
        recent = "\n".join(state["lines"]).strip()
        click.secho(
            f"\nERROR: Tunnel on localhost:{local_port} is not reachable after "
            f"{probe_timeout}s.",
            fg="red",
            bold=True,
        )
        if recent and _is_access_denied(recent):
            click.echo(_format_access_denied(recent))
        else:
            click.echo("  Possible causes:")
            click.echo("    - RDS security group does not allow traffic from the EKS node")
            click.echo("    - RDS and EKS are in different VPCs")
            click.echo("    - SSM session failed to start (check your AWS session)")
            click.echo("    - Another process is already using this port")
            if recent:
                indented = recent.replace("\n", "\n    ")
                click.echo(f"\n  SSM plugin output:\n    {indented}")
        state["cleanup"]()
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


class IamProbeFailure(str):
    """Categorized failure from probe_iam_auth.

    Subclassing str keeps the (ok, err) tuple shape so callers can keep
    treating the second element as a printable message, while the .category
    attribute lets us tailor the hint we show.
    """
    category: str = "unknown"

    def __new__(cls, message: str, category: str = "unknown") -> "IamProbeFailure":
        instance = super().__new__(cls, message)
        instance.category = category
        return instance


def _classify_psql_error(stderr: str) -> str:
    """Map a libpq stderr blob to a coarse failure category.

    Categories:
      auth_failed    — `password authentication failed`. Usually missing
                       rds_iam grant or bad token.
      db_missing     — `database "..." does not exist`.
      role_missing   — `role "..." does not exist`.
      ssl_required   — RDS rejected the connection because SSL is required.
      timeout        — set by caller, not classified here.
      unknown        — fallthrough.
    """
    s = stderr.lower()
    if "password authentication failed" in s:
        return "auth_failed"
    if "does not exist" in s and "database" in s:
        return "db_missing"
    if "does not exist" in s and ("role" in s or "user" in s):
        return "role_missing"
    if "no pg_hba.conf entry" in s and "ssl off" in s:
        return "ssl_required"
    return "unknown"


def discover_databases(
    local_port: int,
    db_user: str,
    token: str,
    timeout: float = 10.0,
) -> tuple[list[str], str]:
    """Enumerate user databases on the cluster via psql to the `postgres` DB.

    Returns (databases, error_message). The list excludes Postgres template
    databases and the locked `rdsadmin` system DB but keeps `postgres` itself
    (useful for admin queries).

    On any failure (psql missing, connection error, query error) returns
    (empty list, error message). Callers should fall back gracefully.
    """
    if not shutil.which("psql"):
        return [], "psql not on PATH"

    env = dict(os.environ)
    env["PGPASSWORD"] = token
    env["PGSSLMODE"] = "require"
    env["PGCONNECT_TIMEOUT"] = str(int(CONNECT_TIMEOUT_SECONDS))

    query = (
        "SELECT datname FROM pg_database "
        "WHERE datistemplate = false AND datname <> 'rdsadmin' "
        "ORDER BY datname;"
    )

    try:
        result = subprocess.run(
            [
                "psql",
                "-h", "127.0.0.1",
                "-p", str(local_port),
                "-U", db_user,
                "-d", "postgres",
                "-v", "ON_ERROR_STOP=1",
                "-At",
                "-c", query,
            ],
            env=env,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return [], f"discovery timed out after {timeout:.0f}s"
    except OSError as exc:
        return [], f"psql could not start: {exc}"

    if result.returncode != 0:
        return [], (result.stderr or b"").decode(errors="replace").strip()

    names = [
        line.strip()
        for line in (result.stdout or b"").decode(errors="replace").splitlines()
        if line.strip()
    ]
    return names, ""


def probe_iam_auth(
    local_port: int,
    db_user: str,
    db_name: str,
    token: str,
    timeout: float = 12.0,
) -> tuple[bool, IamProbeFailure]:
    """Probe RDS IAM auth via psql before handing the user a foreground tunnel.

    Returns (ok, err). On success err is empty. On failure, err is an
    IamProbeFailure with a `.category` attribute the caller can use to
    pick a tailored hint (e.g. `db_missing` vs `auth_failed`).

    Skips silently and returns ok=True if psql isn't on PATH.
    """
    if not shutil.which("psql"):
        return True, IamProbeFailure("", "skipped")

    env = dict(os.environ)
    env["PGPASSWORD"] = token
    # PGSSLMODE is the env equivalent of `sslmode=require` in a connection
    # string. RDS rejects non-SSL connections, so without this the probe
    # silently times out at TLS negotiation instead of returning a useful
    # libpq error.
    env["PGSSLMODE"] = "require"
    # Match the connect_timeout we bake into DATABASE_URL so the probe behaves
    # the same way the user's psql will.
    env["PGCONNECT_TIMEOUT"] = str(int(CONNECT_TIMEOUT_SECONDS))

    try:
        result = subprocess.run(
            [
                "psql",
                "-h", "127.0.0.1",
                "-p", str(local_port),
                "-U", db_user,
                "-d", db_name,
                "-v", "ON_ERROR_STOP=1",
                "-At",
                "-c", "SELECT 1",
            ],
            env=env,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, IamProbeFailure(
            f"psql probe timed out after {timeout:.0f}s", "timeout",
        )
    except OSError as exc:
        return False, IamProbeFailure(f"psql probe could not start: {exc}", "skipped")

    if result.returncode == 0:
        return True, IamProbeFailure("", "ok")

    stderr = (result.stderr or b"").decode(errors="replace").strip()
    return False, IamProbeFailure(stderr, _classify_psql_error(stderr))


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
