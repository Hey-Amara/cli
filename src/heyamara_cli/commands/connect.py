from __future__ import annotations

import base64
import gzip
import json
import shutil
import subprocess
import urllib.parse

import click

from heyamara_cli import config
from heyamara_cli.completions import ENVIRONMENT
from heyamara_cli.config import CLUSTERS, NAMESPACES, SERVICES as APP_SERVICES, SSM_PREFIX
from heyamara_cli.helpers import check_port_free, debug, detect_iam_role, require_aws_session, run
from heyamara_cli.prompts import select
from heyamara_cli.tunnel import (
    CONNECT_TIMEOUT_SECONDS,
    build_database_url,
    discover_databases,
    generate_rds_auth_token as _generate_rds_auth_token_new,
    open_tunnel_and_probe,
    preflight_rds_iam_enabled,
    probe_iam_auth,
)


ENVS = list(NAMESPACES.keys())

SERVICES = ["db", "redis", "rabbitmq"]

DB_NAMES = {
    "staging": "ats_staging",
    "production": "heyamara_prod",
}


def _resolve_profile(profile: str, region: str | None = None) -> tuple[str, str]:
    """Resolve profile and region. region param overrides config."""
    p = profile or config.get("aws_profile")
    r = region or config.get("aws_region")
    return p, r


def _find_eks_node(environment: str, profile: str, region: str) -> str:
    """Find an SSM-enabled EKS worker node to use as tunnel target."""
    cluster_name = CLUSTERS.get(environment)
    if not cluster_name:
        click.secho(f"Unknown environment: {environment}", fg="red")
        raise SystemExit(1)

    click.echo("Finding EKS node for tunnel...")
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


def _start_tunnel(instance_id: str, remote_host: str, remote_port: int, local_port: int, profile: str, region: str):
    """Start SSM port-forwarding session through an EKS node."""
    params = json.dumps({
        "host": [remote_host],
        "portNumber": [str(remote_port)],
        "localPortNumber": [str(local_port)],
    })

    run([
        "aws", "ssm", "start-session",
        "--target", instance_id,
        "--document-name", "AWS-StartPortForwardingSessionToRemoteHost",
        "--parameters", params,
        "--region", region,
        "--profile", profile,
    ])


def _find_rds_endpoint(environment: str, profile: str, region: str) -> tuple[str, int]:
    """Auto-discover the RDS cluster endpoint for the environment."""
    result = run(
        [
            "aws", "rds", "describe-db-clusters",
            "--query", f"DBClusters[?TagList[?Key=='Environment' && Value=='{environment}']].[Endpoint, Port] | [0]",
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
        return data[0], int(data[1])
    except (json.JSONDecodeError, IndexError, TypeError):
        click.secho(f"No RDS cluster found for {environment}", fg="red")
        click.secho(
            f"  Ensure the RDS cluster has an 'Environment' tag set to '{environment}'.",
            fg="yellow",
        )
        raise SystemExit(1)


def _find_redis_endpoint(environment: str, profile: str, region: str) -> tuple[str, int]:
    """Auto-discover the Redis endpoint for the environment."""
    # ElastiCache describe-replication-groups doesn't return tags inline,
    # so use resourcegroupstaggingapi to find the replication group by Environment tag.
    result = run(
        [
            "aws", "resourcegroupstaggingapi", "get-resources",
            "--tag-filters", f"Key=Environment,Values={environment}",
            "--resource-type-filters", "elasticache:replicationgroup",
            "--query", "ResourceTagMappingList[0].ResourceARN",
            "--output", "text",
            "--region", region,
            "--profile", profile,
        ],
        capture=True,
        check=False,
        environment=environment,
    )

    arn = result.stdout.strip()
    if not arn or arn == "None":
        click.secho(f"No Redis cluster found for {environment}", fg="red")
        raise SystemExit(1)

    group_id = arn.rsplit(":", 1)[-1]

    result = run(
        [
            "aws", "elasticache", "describe-replication-groups",
            "--replication-group-id", group_id,
            "--query", "ReplicationGroups[0].NodeGroups[0].PrimaryEndpoint.[Address, Port]",
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
        return data[0], int(data[1])
    except (json.JSONDecodeError, IndexError, TypeError):
        click.secho(f"No Redis cluster found for {environment}", fg="red")
        raise SystemExit(1)


def _find_rabbitmq_endpoint(environment: str, profile: str, region: str) -> tuple[str, int]:
    """Auto-discover the RabbitMQ broker endpoint for the environment."""
    # AmazonMQ list-brokers doesn't return tags inline,
    # so use resourcegroupstaggingapi to find the broker by Environment tag.
    result = run(
        [
            "aws", "resourcegroupstaggingapi", "get-resources",
            "--tag-filters", f"Key=Environment,Values={environment}",
            "--resource-type-filters", "mq:broker",
            "--query", "ResourceTagMappingList[0].ResourceARN",
            "--output", "text",
            "--region", region,
            "--profile", profile,
        ],
        capture=True,
        check=False,
        environment=environment,
    )

    arn = result.stdout.strip()
    if not arn or arn == "None":
        click.secho(f"No RabbitMQ broker found for {environment}", fg="red")
        raise SystemExit(1)

    broker_id = arn.rsplit(":", 1)[-1]

    # Get the broker host from broker ID (format: b-xxx.mq.region.on.aws)
    host = f"{broker_id}.mq.{region}.on.aws"
    # Management console is on port 443
    return host, 443


@click.group()
def connect():
    """Connect to AWS infrastructure services via SSM tunnel.

    \b
    Uses EKS worker nodes as tunnel targets — no bastion needed.
    Auto-discovers service endpoints for the selected environment.
    """
    pass


def _generate_rds_auth_token(rds_host: str, rds_port: int, db_user: str, profile: str, region: str) -> str:
    """Generate an IAM auth token for RDS."""
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
        click.secho("Failed to generate IAM auth token.", fg="red")
        click.secho("Make sure your IAM role has rds-db:connect permission.", fg="yellow")
        raise SystemExit(1)

    return token


def _copy_to_clipboard(text: str) -> bool:
    """Copy text to system clipboard. Returns True on success."""
    for tool in ("pbcopy", "xclip"):
        if shutil.which(tool):
            try:
                cmd = [tool] if tool == "pbcopy" else [tool, "-selection", "clipboard"]
                subprocess.run(cmd, input=text.encode(), check=True)
                return True
            except Exception:
                pass
    return False


# ---- --env-for helpers ------------------------------------------------------
# Support for `connect db --env-for <service>`: fetches the service's real
# env vars from SSM (populated by amara-k8s sync-env-to-ssm workflow), rewrites
# DATABASE_URL host to localhost so apps connect through the SSM tunnel using
# the service account *password* (no 15-min IAM token expiry).


def _fetch_service_env(service: str, environment: str, profile: str, region: str) -> str:
    """Fetch + decode the SSM env blob for a service.

    The sync-env-to-ssm workflow stores env files as gzip+base64 SecureString
    parameters at /amara/<env>/<service>. Returns the decoded plaintext.
    """
    ssm_path = f"{SSM_PREFIX}/{environment}/{service}"
    result = run(
        [
            "aws", "ssm", "get-parameter",
            "--name", ssm_path,
            "--with-decryption",
            "--query", "Parameter.Value",
            "--output", "text",
            "--region", region,
            "--profile", profile,
        ],
        capture=True,
        check=False,
        environment=environment,
    )
    if result.returncode != 0:
        click.secho(f"Failed to fetch {ssm_path}", fg="red")
        if "ParameterNotFound" in (result.stderr or ""):
            click.secho(
                "  Run the sync-env-to-ssm workflow in amara-k8s to populate SSM.",
                fg="yellow",
            )
        raise SystemExit(1)

    raw = result.stdout.strip()
    try:
        return gzip.decompress(base64.b64decode(raw)).decode("utf-8")
    except Exception:
        # Fallback: value was stored as plain text (pre-compression era)
        return raw


def _rewrite_db_url_host(url: str, new_host: str, new_port: int, db_name: str | None = None) -> str:
    """Rewrite a postgres URL to point at new_host:new_port, preserve user:password.

    Preserves the original user:password portion verbatim so an already
    URL-encoded password (e.g. P%40ssw0rd) is not double-encoded.
    """
    parsed = urllib.parse.urlparse(url)

    # netloc is `[userinfo@]host[:port]`. Pull userinfo straight from the
    # original string so we don't decode+re-encode it.
    userinfo, at, _ = parsed.netloc.rpartition("@")
    userinfo_prefix = f"{userinfo}@" if at else ""

    new_netloc = f"{userinfo_prefix}{new_host}:{new_port}"
    path = f"/{db_name.lstrip('/')}" if db_name else parsed.path

    return urllib.parse.urlunparse(parsed._replace(netloc=new_netloc, path=path))


def _rewrite_urls_in_env(
    env_content: str,
    keys: set[str],
    new_host: str,
    new_port: int,
    db_name: str | None = None,
) -> tuple[str, list[str]]:
    """Rewrite the host:port of any env var whose key is in `keys`.

    Returns (new_content, list_of_rewritten_keys). Handles both `KEY=val`
    and `export KEY=val` forms. Preserves quoting.
    """
    out_lines: list[str] = []
    rewritten: list[str] = []

    for line in env_content.splitlines():
        stripped = line.lstrip()
        body = stripped[len("export "):] if stripped.startswith("export ") else stripped
        key = body.split("=", 1)[0] if "=" in body else ""

        if key in keys:
            prefix, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            if value:
                out_lines.append(f"{prefix}={_rewrite_db_url_host(value, new_host, new_port, db_name)}")
                rewritten.append(key)
                continue
        out_lines.append(line)

    return "\n".join(out_lines), rewritten


# URL-var key sets per service type. Kept permissive so we catch the usual
# aliases apps use in their configs.
DB_URL_KEYS = {"DATABASE_URL", "POSTGRES_URL", "DB_URL"}
REDIS_URL_KEYS = {"REDIS_URL", "CACHE_URL", "CELERY_RESULT_BACKEND"}
MQ_URL_KEYS = {"RABBITMQ_URL", "AMQP_URL", "BROKER_URL", "CELERY_BROKER_URL"}


def _write_env_for(
    output: str,
    env_content: str,
    rewritten_keys: list[str],
    service: str,
    local_port: int,
    extra_hint: str | None = None,
) -> None:
    """Write the rewritten env file and print a status block."""
    with open(output, "w") as f:
        f.write(env_content)
        if not env_content.endswith("\n"):
            f.write("\n")

    non_empty = [
        ln for ln in env_content.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]

    click.echo()
    click.secho(f"✓ Wrote {output} ({len(non_empty)} vars)", fg="green")
    if rewritten_keys:
        click.secho(
            f"  Rewrote to localhost:{local_port} → {', '.join(rewritten_keys)}",
            fg="green",
        )
    else:
        click.secho(
            f"  Warning: no matching URL env vars found — file written as-is.",
            fg="yellow",
        )
    if extra_hint:
        click.secho(f"  {extra_hint}", fg="cyan")
    click.echo()
    click.secho("In another terminal, run your app:", fg="cyan")
    click.echo(f"  cd {service} && <your dev command>   # reads {output}")
    click.echo()


@connect.command()
@click.argument("environment", required=False, type=ENVIRONMENT)
@click.option("--local-port", "-p", default=5432, help="Local port.", show_default=True)
@click.option("--profile", default=None, help="AWS profile.")
@click.option("--region", default=None, help="AWS region (overrides config).")
@click.option("--iam", is_flag=True, help="Generate IAM auth token for interactive use (15-min TTL).")
@click.option("--db-user", "-u", default="developer", help="Database user for IAM auth.", show_default=True)
@click.option("--db-name", default=None, help="Database name (overrides auto-detect).")
@click.option("--no-copy", is_flag=True, help="Do not copy DATABASE_URL to clipboard.")
@click.option(
    "--env-for",
    "env_for",
    default=None,
    type=click.Choice(APP_SERVICES),
    help=(
        "Write a .env file with the service's real credentials (service-account password, "
        "no expiry) and open the tunnel. Use this to run apps locally against staging/production."
    ),
)
@click.option(
    "--output", "-o",
    default=".env.local",
    show_default=True,
    help="Output path for --env-for.",
)
@click.option("--dry-run", is_flag=True, help="Show what would happen without executing.")
def db(environment, local_port, profile, region, iam, db_user, db_name, no_copy, env_for, output, dry_run):
    """Connect to RDS (PostgreSQL).

    \b
    Three modes, pick what you need:

    \b
      1. Just open a tunnel (pair with `heyamara db psql` or your own tool):
         heyamara connect db staging

    \b
      2. Interactive query session as your SSO identity (IAM token, 15-min TTL):
         heyamara connect db staging --iam
         heyamara connect db production --iam -u power_user

    \b
      3. Run an app locally against staging/production RDS (no expiry):
         heyamara connect db staging --env-for ats-backend
         # writes .env.local; keeps tunnel alive; use the service password
         # that prod pods already use — no 15-min refresh needed.

    \b
    --iam and --env-for are mutually exclusive. Pick the mode that matches
    what you're doing: humans querying → --iam; apps running → --env-for.
    """
    if iam and env_for:
        click.secho("--iam and --env-for are mutually exclusive.", fg="red")
        click.secho("  --iam: 15-min token, for humans running psql/queries.", fg="yellow")
        click.secho("  --env-for: service password, for apps with pooled connections.", fg="yellow")
        raise SystemExit(1)

    if not environment:
        environment = select("Select environment:", ENVS)
    profile, region = _resolve_profile(profile, region)

    # Port conflict detection
    if not dry_run and not check_port_free(local_port):
        click.secho(f"Port {local_port} is already in use. Use -p to choose another.", fg="red")
        raise SystemExit(1)

    caller_arn = require_aws_session(profile)

    instance_id = _find_eks_node(environment, profile, region)
    rds_host, rds_port = _find_rds_endpoint(environment, profile, region)

    if env_for:
        click.echo(f"Fetching {env_for} env from SSM ({SSM_PREFIX}/{environment}/{env_for}) ...")
        env_content = _fetch_service_env(env_for, environment, profile, region)
        new_content, rewritten = _rewrite_urls_in_env(
            env_content, DB_URL_KEYS, "localhost", local_port, db_name
        )

        if dry_run:
            click.secho(f"[dry-run] Would write {output} (rewrite keys: {rewritten or 'none'})", fg="yellow")
            click.secho(f"[dry-run] Would start SSM tunnel:", fg="yellow")
            click.secho(f"  Instance: {instance_id}", fg="yellow")
            click.secho(f"  Remote:   {rds_host}:{rds_port}", fg="yellow")
            click.secho(f"  Local:    localhost:{local_port}", fg="yellow")
            return

        _write_env_for(
            output, new_content, rewritten, env_for, local_port,
            extra_hint="Service-account password — no 15-min expiry.",
        )
        click.secho(f"Tunneling localhost:{local_port} → {rds_host}:{rds_port}", fg="green")
        click.echo("Press Ctrl+C to stop.\n")

        _start_tunnel(instance_id, rds_host, rds_port, local_port, profile, region)
        return

    if iam:
        # Pre-flight: IAM auth must be enabled on the RDS cluster, otherwise
        # psql would silently hang on authentication.
        if not dry_run and not preflight_rds_iam_enabled(rds_host, environment, profile, region):
            raise SystemExit(1)

        env_default_db = DB_NAMES.get(environment, f"heyamara_{environment}")

        # Show detected IAM role as a hint
        detected_role = detect_iam_role(caller_arn)
        if detected_role and db_user == "developer" and detected_role != "developer":
            click.secho(
                f"Hint: your IAM role is '{detected_role}'. "
                f"Use -u {detected_role} if that's your DB user.",
                fg="cyan",
            )

        click.echo(f"Generating IAM auth token for user '{db_user}'...")

        if dry_run:
            # Dry-run can't open a tunnel or discover databases, so fall back to
            # the env's default DB (or whatever was passed) for the printed URL.
            resolved_db_name = db_name or env_default_db
            token = "<token>"
            database_url = build_database_url(
                user=db_user, password="<token>", host="localhost",
                port=local_port, dbname=resolved_db_name,
            )
            click.echo()
            click.secho("=== Connection Details ===", fg="green")
            click.secho(f"Remote:  {rds_host}:{rds_port}", fg="green")
            click.secho(f"Local:   localhost:{local_port}", fg="green")
            click.secho(f"User:    {db_user}", fg="green")
            click.secho(f"DB:      {resolved_db_name}", fg="green")
            click.echo()
            click.secho("Run this in another terminal to connect:", fg="cyan")
            click.echo()
            click.echo(f"  export DATABASE_URL=\"{database_url}\"")
            click.echo(f"  psql -d $DATABASE_URL")
            click.echo()
            click.echo("[dry-run] Would start SSM tunnel:", )
            click.secho(f"  Instance: {instance_id}", fg="yellow")
            click.secho(f"  Remote:   {rds_host}:{rds_port}", fg="yellow")
            click.secho(f"  Local:    localhost:{local_port}", fg="yellow")
            return

        token = _generate_rds_auth_token_new(rds_host, rds_port, db_user, profile, region)

        # Open the tunnel before resolving the DB name. We need a working
        # connection to enumerate databases when --db-name wasn't passed.
        click.echo()
        proc = open_tunnel_and_probe(
            instance_id, rds_host, rds_port, local_port, profile, region
        )

        # Resolve the database name. Order:
        #   1. --db-name explicit (skip discovery; user knows what they want)
        #   2. interactive picker over discovered databases
        #   3. env default from DB_NAMES (fallback if discovery failed)
        if db_name:
            resolved_db_name = db_name
        else:
            click.echo("Discovering databases on the cluster...")
            databases, disc_err = discover_databases(local_port, db_user, token)
            if not databases:
                resolved_db_name = env_default_db
                if disc_err:
                    click.secho(
                        f"  Discovery failed ({disc_err}); "
                        f"falling back to default DB '{resolved_db_name}'.",
                        fg="yellow",
                    )
            elif len(databases) == 1:
                resolved_db_name = databases[0]
                click.secho(
                    f"  Only one database on the cluster: {resolved_db_name}",
                    fg="cyan",
                )
            else:
                # Reorder so the env's default DB (if it exists in the list) is
                # the first option — InquirerPy preselects the first choice.
                ordered = list(databases)
                if env_default_db in ordered:
                    ordered.remove(env_default_db)
                    ordered.insert(0, env_default_db)
                resolved_db_name = select(
                    f"Select database for {environment}:", ordered
                )

        database_url = build_database_url(
            user=db_user, password=token, host="localhost",
            port=local_port, dbname=resolved_db_name,
        )

        click.echo()
        click.secho("=== Connection Details ===", fg="green")
        click.secho(f"Remote:  {rds_host}:{rds_port}", fg="green")
        click.secho(f"Local:   localhost:{local_port}", fg="green")
        click.secho(f"User:    {db_user}", fg="green")
        click.secho(f"DB:      {resolved_db_name}", fg="green")
        click.echo()
        click.secho("Run this in another terminal to connect:", fg="cyan")
        click.echo()
        click.echo(f"  export DATABASE_URL=\"{database_url}\"")
        click.echo(f"  psql -d $DATABASE_URL")
        click.echo()
        click.echo("  # Or without export:")
        click.echo(f"  PGPASSWORD='{token}' \\")
        click.echo(
            f"  psql \"host=localhost port={local_port} dbname={resolved_db_name}"
            f" user={db_user} sslmode=require connect_timeout={CONNECT_TIMEOUT_SECONDS}\""
        )
        click.echo()

        if not no_copy and _copy_to_clipboard(database_url):
            click.secho("DATABASE_URL copied to clipboard.", fg="green")

        click.secho(
            f"Tip: connect_timeout={CONNECT_TIMEOUT_SECONDS}s is baked into the URL — "
            f"psql will fail fast instead of hanging.",
            fg="cyan",
        )
        click.secho("Token expires in 15 minutes. Re-run to get a new one.", fg="yellow")

        ok, err = probe_iam_auth(local_port, db_user, resolved_db_name, token)
        if not ok:
            click.echo()
            click.secho(
                "WARN: tunnel is up but IAM auth probe failed.",
                fg="yellow", bold=True,
            )
            for line in str(err).splitlines():
                click.secho(f"  {line}", fg="yellow")
            click.echo()

            category = getattr(err, "category", "unknown")

            if category == "db_missing":
                click.secho(
                    f"The cluster doesn't have a database named '{resolved_db_name}'. "
                    f"Either:",
                    fg="cyan",
                )
                click.secho(
                    "  - re-run without --db-name to pick from the list of "
                    "databases that actually exist on this cluster, or",
                    fg="cyan",
                )
                click.secho(
                    f"  - as a superuser: CREATE DATABASE {resolved_db_name};",
                    fg="cyan",
                )
            elif category == "role_missing":
                click.secho(
                    f"The cluster doesn't have a role named '{db_user}'. "
                    f"As a superuser:",
                    fg="cyan",
                )
                click.secho(
                    f"  CREATE USER {db_user};\n"
                    f"  GRANT rds_iam TO {db_user};",
                    fg="bright_white",
                )
            elif category == "auth_failed":
                click.secho("Most common causes (in order):", fg="cyan")
                click.secho(
                    f"  1. The DB role '{db_user}' isn't granted rds_iam. "
                    f"As a superuser:",
                    fg="cyan",
                )
                click.secho(f"       GRANT rds_iam TO {db_user};", fg="bright_white")
                click.secho(
                    f"  2. Your IAM principal lacks rds-db:connect on "
                    f"dbuser:<cluster-id>/{db_user} for {environment}.",
                    fg="cyan",
                )
            elif category == "ssl_required":
                click.secho(
                    "RDS rejected the connection because SSL is required. "
                    "Connect via the URL the CLI emits (it sets sslmode=require) "
                    "or pass sslmode=require to your client.",
                    fg="cyan",
                )
            elif category == "timeout":
                click.secho(
                    "Probe timed out. The tunnel reports ready but the auth "
                    "handshake didn't return in time. Re-run psql manually with "
                    "the URL above; if it hangs, check RDS->EKS-node security "
                    "group rules.",
                    fg="cyan",
                )
            else:
                click.secho(
                    "Possible causes: missing rds_iam grant, missing role, "
                    "missing rds-db:connect IAM permission, or cluster-side "
                    "rejection. Re-run psql manually with the URL above for the "
                    "exact libpq error.",
                    fg="cyan",
                )

            click.echo()
            click.secho(
                "The tunnel is still open — fix the cause in another session "
                "and retry psql; you don't need to restart this command unless "
                "the IAM token expires.",
                fg="cyan",
            )

        click.echo()
        click.echo("Press Ctrl+C to stop.\n")
        try:
            proc.wait()
        except KeyboardInterrupt:
            pass
        return

    click.secho(f"Tunneling localhost:{local_port} -> {rds_host}:{rds_port}", fg="green")
    click.secho(f"Connect with: psql -h localhost -p {local_port} -U <user> <database>", fg="cyan")
    click.secho("Tip: use --iam to auto-generate an IAM auth token.", fg="yellow")

    if dry_run:
        click.echo()
        click.secho("[dry-run] Would start SSM tunnel:", fg="yellow")
        click.secho(f"  Instance: {instance_id}", fg="yellow")
        click.secho(f"  Remote:   {rds_host}:{rds_port}", fg="yellow")
        click.secho(f"  Local:    localhost:{local_port}", fg="yellow")
        return

    click.echo("Press Ctrl+C to stop.\n")

    _start_tunnel(instance_id, rds_host, rds_port, local_port, profile, region)


@connect.command()
@click.argument("environment", required=False, type=ENVIRONMENT)
@click.option("--local-port", "-p", default=6379, help="Local port.", show_default=True)
@click.option("--profile", default=None, help="AWS profile.")
@click.option("--region", default=None, help="AWS region (overrides config).")
@click.option(
    "--env-for",
    "env_for",
    default=None,
    type=click.Choice(APP_SERVICES),
    help="Write a .env file with the service's REDIS_URL pointed at localhost and open the tunnel.",
)
@click.option("--output", "-o", default=".env.local", show_default=True, help="Output path for --env-for.")
@click.option("--dry-run", is_flag=True, help="Show what would happen without executing.")
def redis(environment, local_port, profile, region, env_for, output, dry_run):
    """Connect to Redis (ElastiCache).

    \b
    Examples:
      heyamara connect redis staging
      heyamara connect redis staging -p 6380
      heyamara connect redis staging --env-for ats-backend
      heyamara connect redis staging --dry-run

    \b
    Then connect interactively:
      redis-cli -h localhost -p <local-port>

    \b
    Or run an app locally with --env-for: writes .env.local with REDIS_URL
    rewritten to localhost, keeps the tunnel alive for the session.
    """
    if not environment:
        environment = select("Select environment:", ENVS)
    profile, region = _resolve_profile(profile, region)

    if not dry_run and not check_port_free(local_port):
        click.secho(f"Port {local_port} is already in use. Use -p to choose another.", fg="red")
        raise SystemExit(1)

    require_aws_session(profile)

    instance_id = _find_eks_node(environment, profile, region)
    redis_host, redis_port = _find_redis_endpoint(environment, profile, region)

    if env_for:
        click.echo(f"Fetching {env_for} env from SSM ({SSM_PREFIX}/{environment}/{env_for}) ...")
        env_content = _fetch_service_env(env_for, environment, profile, region)
        new_content, rewritten = _rewrite_urls_in_env(
            env_content, REDIS_URL_KEYS, "localhost", local_port
        )

        if dry_run:
            click.secho(f"[dry-run] Would write {output} (rewrite keys: {rewritten or 'none'})", fg="yellow")
            click.secho(f"[dry-run] Would tunnel localhost:{local_port} → {redis_host}:{redis_port}", fg="yellow")
            return

        tls_hint = None
        if redis_port == 6380 or any("rediss://" in ln for ln in env_content.splitlines()):
            tls_hint = (
                "Remote Redis uses TLS (rediss://). The tunnel forwards raw TCP — "
                "your client may need to disable hostname verification against 'localhost'."
            )

        _write_env_for(output, new_content, rewritten, env_for, local_port, extra_hint=tls_hint)
        click.secho(f"Tunneling localhost:{local_port} → {redis_host}:{redis_port}", fg="green")
        click.echo("Press Ctrl+C to stop.\n")

        _start_tunnel(instance_id, redis_host, redis_port, local_port, profile, region)
        return

    click.secho(f"Tunneling localhost:{local_port} -> {redis_host}:{redis_port}", fg="green")
    click.secho(f"Connect with: redis-cli -h localhost -p {local_port}", fg="cyan")

    if dry_run:
        click.echo()
        click.secho("[dry-run] Would start SSM tunnel:", fg="yellow")
        click.secho(f"  Instance: {instance_id}", fg="yellow")
        click.secho(f"  Remote:   {redis_host}:{redis_port}", fg="yellow")
        click.secho(f"  Local:    localhost:{local_port}", fg="yellow")
        return

    click.echo("Press Ctrl+C to stop.\n")

    _start_tunnel(instance_id, redis_host, redis_port, local_port, profile, region)


@connect.command()
@click.argument("environment", required=False, type=ENVIRONMENT)
@click.option("--local-port", "-p", default=None, type=int, help="Local port (default 15672 for UI, 5671 with --env-for).")
@click.option("--profile", default=None, help="AWS profile.")
@click.option("--region", default=None, help="AWS region (overrides config).")
@click.option(
    "--env-for",
    "env_for",
    default=None,
    type=click.Choice(APP_SERVICES),
    help="Write a .env file with AMQP_URL rewritten to localhost:5671 and open AMQPS tunnel instead of UI.",
)
@click.option("--output", "-o", default=".env.local", show_default=True, help="Output path for --env-for.")
@click.option("--dry-run", is_flag=True, help="Show what would happen without executing.")
def rabbitmq(environment, local_port, profile, region, env_for, output, dry_run):
    """Connect to RabbitMQ (AmazonMQ).

    \b
    Two modes:

    \b
      1. Management UI (default — port 15672 → remote 443):
         heyamara connect rabbitmq staging
         # then open https://localhost:15672 in your browser

    \b
      2. App-side AMQPS (--env-for — port 5671):
         heyamara connect rabbitmq staging --env-for ats-backend
         # writes .env.local with AMQP_URL/RABBITMQ_URL pointing at localhost:5671

    \b
    Heads up: remote AmazonMQ uses TLS and the broker's certificate CN won't
    match 'localhost'. Most clients need TLS hostname verification disabled
    for local dev. Alternatively, alias the broker hostname to 127.0.0.1
    in /etc/hosts so the cert validates.
    """
    if not environment:
        environment = select("Select environment:", ENVS)
    profile, region = _resolve_profile(profile, region)

    # Port defaults: 15672 for UI, 5671 for AMQPS.
    if local_port is None:
        local_port = 5671 if env_for else 15672

    if not dry_run and not check_port_free(local_port):
        click.secho(f"Port {local_port} is already in use. Use -p to choose another.", fg="red")
        raise SystemExit(1)

    require_aws_session(profile)

    instance_id = _find_eks_node(environment, profile, region)
    mq_host, _mq_ui_port = _find_rabbitmq_endpoint(environment, profile, region)
    # _find_rabbitmq_endpoint always returns (host, 443) for the UI.
    # For --env-for we tunnel to AMQPS (5671) instead.
    remote_port = 5671 if env_for else 443

    if env_for:
        click.echo(f"Fetching {env_for} env from SSM ({SSM_PREFIX}/{environment}/{env_for}) ...")
        env_content = _fetch_service_env(env_for, environment, profile, region)
        new_content, rewritten = _rewrite_urls_in_env(
            env_content, MQ_URL_KEYS, "localhost", local_port
        )

        if dry_run:
            click.secho(f"[dry-run] Would write {output} (rewrite keys: {rewritten or 'none'})", fg="yellow")
            click.secho(f"[dry-run] Would tunnel localhost:{local_port} → {mq_host}:{remote_port}", fg="yellow")
            return

        _write_env_for(
            output, new_content, rewritten, env_for, local_port,
            extra_hint=(
                "TLS note: remote broker cert is bound to its real hostname, not 'localhost'. "
                "Disable hostname verification in your client (or alias the broker hostname to "
                "127.0.0.1 in /etc/hosts to keep verification on)."
            ),
        )
        click.secho(f"Tunneling localhost:{local_port} → {mq_host}:{remote_port} (AMQPS)", fg="green")
        click.echo("Press Ctrl+C to stop.\n")

        _start_tunnel(instance_id, mq_host, remote_port, local_port, profile, region)
        return

    click.secho(f"Tunneling localhost:{local_port} -> {mq_host}:{remote_port}", fg="green")
    click.secho(f"Open in browser: https://localhost:{local_port}", fg="cyan")

    if dry_run:
        click.echo()
        click.secho("[dry-run] Would start SSM tunnel:", fg="yellow")
        click.secho(f"  Instance: {instance_id}", fg="yellow")
        click.secho(f"  Remote:   {mq_host}:{remote_port}", fg="yellow")
        click.secho(f"  Local:    localhost:{local_port}", fg="yellow")
        return

    click.echo("Press Ctrl+C to stop.\n")

    _start_tunnel(instance_id, mq_host, remote_port, local_port, profile, region)
