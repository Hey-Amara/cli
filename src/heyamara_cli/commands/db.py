"""`heyamara db ...` — ergonomic database commands.

One-shot wrappers around the SSM tunnel + RDS IAM auth flow. Designed so devs
don't have to think about tunnels at all:

  heyamara db psql staging ats              → interactive psql, tunnel auto-closes
  heyamara db url staging ats               → prints DATABASE_URL (tunnel lives while shell does)
  heyamara db run staging ats -- node …     → runs command with DATABASE_URL set
  heyamara db doctor staging                → deep diagnostic for connection issues

Every URL includes connect_timeout=10 so psql fails fast instead of hanging.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Optional

import click

from heyamara_cli import config
from heyamara_cli.completions import ENVIRONMENT
from heyamara_cli.config import CLUSTERS, NAMESPACES
from heyamara_cli.helpers import (
    check_port_free,
    detect_iam_role,
    require_aws_session,
    run,
)
from heyamara_cli.prompts import select
from heyamara_cli.tunnel import (
    build_database_url,
    generate_rds_auth_token,
    open_tunnel_and_probe,
    preflight_rds_iam_enabled,
    wait_for_tcp,
)


ENVS = list(NAMESPACES.keys())

# IAM-enabled login role used by humans when connecting through the CLI.
# Service users (ats_backend, ae_backend, etc.) don't have rds_iam grants —
# they use password auth from inside K8s. The CLI always auths as `developer`
# unless the caller explicitly overrides with `--as <user>`.
DEFAULT_IAM_USER = "developer"

# service name → db name
# Extend here as new services get their own databases.
SERVICE_DBS: dict[str, str] = {
    "ats": "ats_staging",
    "ae": "ae_staging",
    "ai": "ai_staging",
    "memory": "memory_staging",
    "profile": "profile_staging",
}

# Production databases. Today the ATS-family services share one DB (heyamara_prod),
# ai-backend has its own, and memory-service has its own. Extend as production is
# migrated to per-service DBs.
PRODUCTION_SERVICE_DBS: dict[str, str] = {
    "ats":     "heyamara_prod",        # ats-backend, ats-frontend, ae-backend, profile-service all live here today
    "ae":      "heyamara_prod",        # (alias — same DB as ats while prod isn't split)
    "profile": "heyamara_prod",        # (alias)
    "ai":      "ai_production",
    "memory":  "memory_service_prod",
}


# --------------------------------------------------------------------------
# Shared resolution helpers
# --------------------------------------------------------------------------


def _pick_service(service: Optional[str], env: str) -> str:
    """Resolve the target database name from a service arg (or via interactive picker)."""
    mapping = PRODUCTION_SERVICE_DBS if env == "production" and PRODUCTION_SERVICE_DBS else SERVICE_DBS
    if not mapping:
        click.secho(
            f"No service → database mapping configured for {env}.",
            fg="red",
        )
        raise SystemExit(1)

    if not service:
        service = select("Select service database:", sorted(mapping.keys()))
    if service not in mapping:
        click.secho(
            f"Unknown service '{service}'. Known: {', '.join(sorted(mapping.keys()))}",
            fg="red",
        )
        raise SystemExit(1)

    return mapping[service]


def _resolve_env_profile_region(
    env: Optional[str], profile: Optional[str], region: Optional[str]
) -> tuple[str, str, str]:
    """Resolve environment (via picker if needed) and AWS profile/region."""
    if not env:
        env = select("Select environment:", ENVS)
    p = profile or config.get("aws_profile")
    r = region or config.get("aws_region")
    return env, p, r


def _find_eks_node(environment: str, profile: str, region: str) -> str:
    """Locate a running EKS worker for SSM tunneling."""
    cluster_name = CLUSTERS.get(environment)
    if not cluster_name:
        click.secho(f"Unknown environment: {environment}", fg="red")
        raise SystemExit(1)

    result = run(
        [
            "aws", "ec2", "describe-instances",
            "--filters",
            f"Name=tag:eks:cluster-name,Values={cluster_name}",
            "Name=instance-state-name,Values=running",
            "--query", "Reservations[].Instances[0].InstanceId",
            "--output", "text",
            "--region", region,
            "--profile", profile,
        ],
        capture=True,
        check=False,
        environment=environment,
    )

    instance_id = result.stdout.strip().split()[0] if result.stdout.strip() else ""
    if result.returncode != 0 or not instance_id or instance_id == "None":
        click.secho(f"No EKS worker nodes found for {environment} ({cluster_name})", fg="red")
        raise SystemExit(1)

    return instance_id


def _find_rds_endpoint(environment: str, profile: str, region: str) -> tuple[str, int]:
    """Discover RDS writer endpoint by Environment tag."""
    import json as _json

    result = run(
        [
            "aws", "rds", "describe-db-clusters",
            "--query",
            f"DBClusters[?TagList[?Key=='Environment' && Value=='{environment}']].[Endpoint, Port] | [0]",
            "--output", "json",
            "--region", region,
            "--profile", profile,
        ],
        capture=True,
        check=False,
        environment=environment,
    )

    try:
        data = _json.loads(result.stdout.strip())
        return data[0], int(data[1])
    except (_json.JSONDecodeError, IndexError, TypeError):
        click.secho(f"No RDS cluster found for {environment}", fg="red")
        click.secho(
            f"  Ensure the RDS cluster has an 'Environment' tag set to '{environment}'.",
            fg="yellow",
        )
        raise SystemExit(1)


def _pick_local_port(preferred: int) -> int:
    """Find a free local port, starting at the preferred one."""
    if check_port_free(preferred):
        return preferred
    # Walk up to find the next free port
    for p in range(preferred + 1, preferred + 50):
        if check_port_free(p):
            return p
    click.secho(f"Could not find a free local port near {preferred}.", fg="red")
    raise SystemExit(1)


def _generate_token_and_url(
    *,
    db_user: str,
    dbname: str,
    rds_host: str,
    rds_port: int,
    local_port: int,
    profile: str,
    region: str,
) -> str:
    """Generate IAM token + return a DATABASE_URL pointed at localhost."""
    token = generate_rds_auth_token(rds_host, rds_port, db_user, profile, region)
    return build_database_url(
        user=db_user,
        password=token,
        host="localhost",
        port=local_port,
        dbname=dbname,
    )


# --------------------------------------------------------------------------
# Click group
# --------------------------------------------------------------------------


@click.group()
def db():
    """Database helpers — one-shot psql, URL export, script wrappers.

    \b
    Quick reference:
      heyamara db psql staging ats                  interactive psql session
      heyamara db url staging ats                   print DATABASE_URL (tunnel stays up while shell lives)
      heyamara db run staging ats -- node script.js run a command with DATABASE_URL set
      heyamara db doctor staging                    diagnose connection issues
    """
    pass


# --------------------------------------------------------------------------
# heyamara db psql
# --------------------------------------------------------------------------


@db.command("psql")
@click.argument("environment", required=False, type=ENVIRONMENT)
@click.argument("service", required=False)
@click.option("--as", "db_user", default=None, help="DB user to authenticate as (default: developer — the IAM-enabled read-only role).")
@click.option("--db-name", default=None, help="Override the auto-detected database name.")
@click.option("--local-port", "-p", default=15432, help="Local tunnel port.", show_default=True)
@click.option("--profile", default=None, help="AWS profile.")
@click.option("--region", default=None, help="AWS region.")
def psql_cmd(environment, service, db_user, db_name, local_port, profile, region):
    """Open an interactive psql session (tunnel auto-closes on exit).

    \b
    Examples:
      heyamara db psql staging               pick service interactively
      heyamara db psql staging ats           connect to ats_staging as ats_backend
      heyamara db psql staging ai --as developer
    """
    if not shutil.which("psql"):
        click.secho("psql not found in PATH.", fg="red", bold=True)
        click.echo("  macOS:  brew install libpq && brew link --force libpq")
        click.echo("  Linux:  apt install postgresql-client")
        raise SystemExit(1)

    env, profile, region = _resolve_env_profile_region(environment, profile, region)
    default_db = _pick_service(service, env)
    resolved_db = db_name or default_db
    resolved_user = db_user or DEFAULT_IAM_USER

    local_port = _pick_local_port(local_port)
    caller_arn = require_aws_session(profile)

    instance_id = _find_eks_node(env, profile, region)
    rds_host, rds_port = _find_rds_endpoint(env, profile, region)

    if not preflight_rds_iam_enabled(rds_host, env, profile, region):
        raise SystemExit(1)

    click.echo(f"Opening tunnel → {rds_host}:{rds_port} (local:{local_port})")
    open_tunnel_and_probe(instance_id, rds_host, rds_port, local_port, profile, region)

    click.echo(f"Generating IAM auth token for user '{resolved_user}'...")
    database_url = _generate_token_and_url(
        db_user=resolved_user,
        dbname=resolved_db,
        rds_host=rds_host,
        rds_port=rds_port,
        local_port=local_port,
        profile=profile,
        region=region,
    )

    click.secho(
        f"\n→ psql {resolved_user}@{resolved_db} (staging)  "
        f"— Ctrl+D or \\q to exit\n",
        fg="green",
    )

    # Hand off to psql. When psql exits, atexit handler closes the tunnel.
    proc = subprocess.run(["psql", database_url])
    sys.exit(proc.returncode)


# --------------------------------------------------------------------------
# heyamara db url
# --------------------------------------------------------------------------


@db.command("url")
@click.argument("environment", required=False, type=ENVIRONMENT)
@click.argument("service", required=False)
@click.option("--as", "db_user", default=None, help="DB user to authenticate as (default: developer — the IAM-enabled read-only role).")
@click.option("--db-name", default=None, help="Override the auto-detected database name.")
@click.option("--local-port", "-p", default=15432, help="Local tunnel port.", show_default=True)
@click.option("--profile", default=None, help="AWS profile.")
@click.option("--region", default=None, help="AWS region.")
def url_cmd(environment, service, db_user, db_name, local_port, profile, region):
    """Print a ready-to-use DATABASE_URL and keep the tunnel alive.

    \b
    The tunnel lives as long as this command's process lives. For scripts, pipe
    the URL into the child and let the tunnel terminate with the parent shell.

    \b
    Examples:
      # One-liner for a script:
      DATABASE_URL=$(heyamara db url staging ats) node scripts/seed.js

      # Or export it first:
      export DATABASE_URL=$(heyamara db url staging ats)
      psql $DATABASE_URL
    """
    env, profile, region = _resolve_env_profile_region(environment, profile, region)
    default_db = _pick_service(service, env)
    resolved_db = db_name or default_db
    resolved_user = db_user or DEFAULT_IAM_USER

    local_port = _pick_local_port(local_port)
    require_aws_session(profile)

    instance_id = _find_eks_node(env, profile, region)
    rds_host, rds_port = _find_rds_endpoint(env, profile, region)

    if not preflight_rds_iam_enabled(rds_host, env, profile, region):
        raise SystemExit(1)

    # All diagnostics go to stderr so stdout is pure URL for `$(...)` capture.
    click.echo(f"Opening tunnel → {rds_host}:{rds_port} (local:{local_port})", err=True)
    open_tunnel_and_probe(instance_id, rds_host, rds_port, local_port, profile, region)

    click.echo(f"Generating IAM auth token for user '{resolved_user}'...", err=True)
    database_url = _generate_token_and_url(
        db_user=resolved_user,
        dbname=resolved_db,
        rds_host=rds_host,
        rds_port=rds_port,
        local_port=local_port,
        profile=profile,
        region=region,
    )

    # Print URL cleanly to stdout for command substitution.
    click.echo(database_url)
    click.echo(
        "\nTunnel is live. Token expires in 15 min. Press Ctrl+C to close.",
        err=True,
    )

    # Block until user kills us (or the child process exits for `db run`)
    try:
        # Wait forever; atexit handler cleans up on SIGINT/SIGTERM.
        while True:
            import time
            time.sleep(60)
    except KeyboardInterrupt:
        click.echo("\nTunnel closed.", err=True)


# --------------------------------------------------------------------------
# heyamara db run
# --------------------------------------------------------------------------


@db.command("run", context_settings=dict(ignore_unknown_options=True))
@click.argument("environment", type=ENVIRONMENT)
@click.argument("service")
@click.option("--as", "db_user", default=None, help="DB user to authenticate as (default: developer — the IAM-enabled read-only role).")
@click.option("--db-name", default=None, help="Override the auto-detected database name.")
@click.option("--local-port", "-p", default=15432, help="Local tunnel port.", show_default=True)
@click.option("--profile", default=None, help="AWS profile.")
@click.option("--region", default=None, help="AWS region.")
@click.argument("command", nargs=-1, required=True)
def run_cmd(environment, service, db_user, db_name, local_port, profile, region, command):
    """Run a command with DATABASE_URL exported; tunnel cleans up on exit.

    \b
    Examples:
      heyamara db run staging ats -- node scripts/seed-staging-heyquorra.js
      heyamara db run staging ai -- pnpm db:migrate
      heyamara db run staging ats --as developer -- psql "$DATABASE_URL" -c "SELECT now()"

    \b
    Use `--` before the command so flags pass through to the child.
    """
    default_db = _pick_service(service, environment)
    resolved_db = db_name or default_db
    resolved_user = db_user or DEFAULT_IAM_USER

    local_port = _pick_local_port(local_port)
    require_aws_session(profile or config.get("aws_profile"))

    env_name = environment
    profile_r = profile or config.get("aws_profile")
    region_r = region or config.get("aws_region")

    instance_id = _find_eks_node(env_name, profile_r, region_r)
    rds_host, rds_port = _find_rds_endpoint(env_name, profile_r, region_r)

    if not preflight_rds_iam_enabled(rds_host, env_name, profile_r, region_r):
        raise SystemExit(1)

    click.echo(f"Opening tunnel → {rds_host}:{rds_port} (local:{local_port})", err=True)
    open_tunnel_and_probe(instance_id, rds_host, rds_port, local_port, profile_r, region_r)

    click.echo(f"Generating IAM auth token for user '{resolved_user}'...", err=True)
    database_url = _generate_token_and_url(
        db_user=resolved_user,
        dbname=resolved_db,
        rds_host=rds_host,
        rds_port=rds_port,
        local_port=local_port,
        profile=profile_r,
        region=region_r,
    )

    child_env = os.environ.copy()
    child_env["DATABASE_URL"] = database_url
    child_env["PGPASSWORD"] = database_url.split(":")[2].split("@")[0]  # IAM token

    click.secho(
        f"\n→ Running: {' '.join(command)}\n   DATABASE_URL={resolved_user}@{resolved_db} (staging, IAM)\n",
        fg="green",
        err=True,
    )

    proc = subprocess.run(list(command), env=child_env)
    sys.exit(proc.returncode)


# --------------------------------------------------------------------------
# heyamara db doctor
# --------------------------------------------------------------------------


@db.command("doctor")
@click.argument("environment", required=False, type=ENVIRONMENT)
@click.argument("service", required=False)
@click.option("--as", "db_user", default=None, help="DB user to test (default: developer — the IAM-enabled read-only role).")
@click.option("--profile", default=None, help="AWS profile.")
@click.option("--region", default=None, help="AWS region.")
def doctor(environment, service, db_user, profile, region):
    """Diagnose RDS connection issues end-to-end.

    \b
    Walks through every layer and reports which step fails:
      1. AWS session is valid
      2. RDS cluster discoverable by Environment tag
      3. IAM database authentication enabled on cluster
      4. EKS worker node available for tunneling
      5. SSM session can open
      6. Tunnel reaches RDS (confirms security groups)
      7. IAM auth token generation works
      8. Login with token succeeds
    """
    import socket as _socket

    env, profile, region = _resolve_env_profile_region(environment, profile, region)
    default_db = _pick_service(service, env)
    resolved_user = db_user or DEFAULT_IAM_USER

    ok = lambda msg: click.secho(f"  ✅ {msg}", fg="green")
    warn = lambda msg: click.secho(f"  ⚠️  {msg}", fg="yellow")
    fail = lambda msg: click.secho(f"  ❌ {msg}", fg="red", bold=True)

    click.secho(f"\n=== db doctor: {env} / service={service or default_db} / user={resolved_user} ===\n", fg="cyan")

    # 1. AWS session
    click.echo("[1/8] AWS session")
    try:
        caller_arn = require_aws_session(profile)
        ok(f"Authenticated as {caller_arn}")
    except SystemExit:
        fail("AWS session invalid. Run: heyamara login")
        return

    # 2. RDS discovery
    click.echo("\n[2/8] RDS cluster discovery")
    try:
        rds_host, rds_port = _find_rds_endpoint(env, profile, region)
        ok(f"Found {rds_host}:{rds_port}")
    except SystemExit:
        fail("No RDS cluster tagged Environment={}".format(env))
        return

    # 3. IAM auth enabled
    click.echo("\n[3/8] RDS IAM authentication enabled")
    if preflight_rds_iam_enabled(rds_host, env, profile, region):
        ok("IAM auth is enabled")
    else:
        fail("IAM auth is disabled — see above for fix")
        return

    # 4. EKS node
    click.echo("\n[4/8] EKS worker node for tunnel")
    try:
        instance_id = _find_eks_node(env, profile, region)
        ok(f"Found worker: {instance_id}")
    except SystemExit:
        fail("No EKS workers found. Check the cluster name + region.")
        return

    # 5-6. SSM tunnel + RDS reachability
    click.echo("\n[5/8] SSM session + tunnel open")
    local_port = _pick_local_port(15432)
    try:
        open_tunnel_and_probe(instance_id, rds_host, rds_port, local_port, profile, region, probe_timeout=8.0)
        ok(f"Tunnel opened on localhost:{local_port}")
    except SystemExit:
        fail("Tunnel failed to establish (see above)")
        return

    click.echo("\n[6/8] TCP reachability through tunnel")
    if wait_for_tcp("localhost", local_port, timeout=3):
        ok("RDS port responds through the tunnel")
    else:
        fail(
            "RDS port not reachable. Likely an EKS-node → RDS security group issue."
        )
        return

    # 7. IAM token
    click.echo("\n[7/8] IAM auth token generation")
    try:
        token = generate_rds_auth_token(rds_host, rds_port, resolved_user, profile, region)
        ok(f"Token generated ({len(token)} chars, 15-min TTL)")
    except SystemExit:
        fail(f"Token generation failed for user '{resolved_user}'")
        fail("  → Your IAM role likely lacks rds-db:connect for this DB user")
        return

    # 8. Live login test via psql (if available)
    click.echo(f"\n[8/8] Login test as '{resolved_user}' → {default_db}")
    if not shutil.which("psql"):
        warn("psql not installed — skipping live login test")
        click.secho("\nAll reachable steps passed.", fg="green", bold=True)
        return

    database_url = build_database_url(
        user=resolved_user,
        password=token,
        host="localhost",
        port=local_port,
        dbname=default_db,
        connect_timeout=10,
    )
    result = subprocess.run(
        ["psql", database_url, "-c", "SELECT current_user, current_database()"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        ok(f"Login succeeded")
        click.echo(f"     → {result.stdout.strip().splitlines()[2].strip()}")
    else:
        fail("Login failed:")
        click.echo(f"     {result.stderr.strip()}")
        fail("  → User may not exist in the database")
        fail(f"  → Or rds-db:connect policy doesn't cover this dbuser: {resolved_user}")
        return

    click.secho("\nAll checks passed. You can connect.", fg="green", bold=True)
