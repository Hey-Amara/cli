from __future__ import annotations

import json
import shutil
import subprocess
import urllib.parse

import click

from heyamara_cli import config
from heyamara_cli.completions import ENVIRONMENT
from heyamara_cli.config import CLUSTERS, NAMESPACES
from heyamara_cli.helpers import check_port_free, debug, detect_iam_role, require_aws_session, run
from heyamara_cli.prompts import select


ENVS = list(NAMESPACES.keys())

SERVICES = ["db", "redis", "rabbitmq"]

DB_NAMES = {
    "staging": "heyamara_staging",
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


@connect.command()
@click.argument("environment", required=False, type=ENVIRONMENT)
@click.option("--local-port", "-p", default=5432, help="Local port.", show_default=True)
@click.option("--profile", default=None, help="AWS profile.")
@click.option("--region", default=None, help="AWS region (overrides config).")
@click.option("--iam", is_flag=True, help="Generate IAM auth token for passwordless login.")
@click.option("--db-user", "-u", default="developer", help="Database user for IAM auth.", show_default=True)
@click.option("--db-name", default=None, help="Database name (overrides auto-detect).")
@click.option("--no-copy", is_flag=True, help="Do not copy DATABASE_URL to clipboard.")
@click.option("--dry-run", is_flag=True, help="Show what would happen without executing.")
def db(environment, local_port, profile, region, iam, db_user, db_name, no_copy, dry_run):
    """Connect to RDS (PostgreSQL).

    \b
    Examples:
      heyamara connect db dev
      heyamara connect db production --iam
      heyamara connect db production --iam -u power_user -p 5433
      heyamara connect db production --iam --dry-run
      heyamara connect db dev --db-name my_other_db

    \b
    With --iam, generates an IAM auth token and prints
    ready-to-use psql / DATABASE_URL connection strings.
    """
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

    if iam:
        resolved_db_name = db_name or DB_NAMES.get(environment, f"heyamara_{environment}")

        # Show detected IAM role as a hint
        detected_role = detect_iam_role(caller_arn)
        if detected_role and db_user == "developer" and detected_role != "developer":
            click.secho(
                f"Hint: your IAM role is '{detected_role}'. "
                f"Use -u {detected_role} if that's your DB user.",
                fg="cyan",
            )

        click.echo(f"Generating IAM auth token for user '{db_user}'...")

        if not dry_run:
            token = _generate_rds_auth_token(rds_host, rds_port, db_user, profile, region)
            encoded_token = urllib.parse.quote(token, safe="")
            database_url = (
                f"postgresql://{db_user}:{encoded_token}"
                f"@localhost:{local_port}/{resolved_db_name}?sslmode=require"
            )
        else:
            token = "<token>"
            database_url = (
                f"postgresql://{db_user}:<token>"
                f"@localhost:{local_port}/{resolved_db_name}?sslmode=require"
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
        click.echo(f"  # Or without export:")
        click.echo(f"  PGPASSWORD='{token}' \\")
        click.echo(
            f"  psql \"host=localhost port={local_port} dbname={resolved_db_name}"
            f" user={db_user} sslmode=require\""
        )
        click.echo()

        if not dry_run and not no_copy:
            if _copy_to_clipboard(database_url):
                click.secho("DATABASE_URL copied to clipboard.", fg="green")

        click.secho("Token expires in 15 minutes. Re-run to get a new one.", fg="yellow")
    else:
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
@click.option("--dry-run", is_flag=True, help="Show what would happen without executing.")
def redis(environment, local_port, profile, region, dry_run):
    """Connect to Redis (ElastiCache).

    \b
    Examples:
      heyamara connect redis dev
      heyamara connect redis dev -p 6380
      heyamara connect redis dev --dry-run

    Then connect with:
      redis-cli -h localhost -p <local-port>
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
@click.option("--local-port", "-p", default=15672, help="Local port.", show_default=True)
@click.option("--profile", default=None, help="AWS profile.")
@click.option("--region", default=None, help="AWS region (overrides config).")
@click.option("--dry-run", is_flag=True, help="Show what would happen without executing.")
def rabbitmq(environment, local_port, profile, region, dry_run):
    """Connect to RabbitMQ Management UI.

    \b
    Examples:
      heyamara connect rabbitmq dev
      heyamara connect rabbitmq dev --dry-run

    Then open in browser:
      https://localhost:15672
    """
    if not environment:
        environment = select("Select environment:", ENVS)
    profile, region = _resolve_profile(profile, region)

    if not dry_run and not check_port_free(local_port):
        click.secho(f"Port {local_port} is already in use. Use -p to choose another.", fg="red")
        raise SystemExit(1)

    require_aws_session(profile)

    instance_id = _find_eks_node(environment, profile, region)
    mq_host, mq_port = _find_rabbitmq_endpoint(environment, profile, region)

    click.secho(f"Tunneling localhost:{local_port} -> {mq_host}:{mq_port}", fg="green")
    click.secho(f"Open in browser: https://localhost:{local_port}", fg="cyan")

    if dry_run:
        click.echo()
        click.secho("[dry-run] Would start SSM tunnel:", fg="yellow")
        click.secho(f"  Instance: {instance_id}", fg="yellow")
        click.secho(f"  Remote:   {mq_host}:{mq_port}", fg="yellow")
        click.secho(f"  Local:    localhost:{local_port}", fg="yellow")
        return

    click.echo("Press Ctrl+C to stop.\n")

    _start_tunnel(instance_id, mq_host, mq_port, local_port, profile, region)
