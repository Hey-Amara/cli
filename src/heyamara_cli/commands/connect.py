import json

import click

from heyamara_cli import config
from heyamara_cli.completions import ENVIRONMENT
from heyamara_cli.config import CLUSTERS, NAMESPACES
from heyamara_cli.helpers import require_aws_session, run
from heyamara_cli.prompts import select


ENVS = list(NAMESPACES.keys())

SERVICES = ["db", "redis", "rabbitmq"]


def _resolve_profile(profile: str) -> tuple[str, str]:
    """Resolve profile and region."""
    p = profile or config.get("aws_profile")
    r = config.get("aws_region")
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
    """Auto-discover the RDS endpoint for the environment."""
    result = run(
        [
            "aws", "rds", "describe-db-instances",
            "--query", f"DBInstances[?contains(DBInstanceIdentifier, '{environment}')].[Endpoint.Address, Endpoint.Port] | [0]",
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
        click.secho(f"No RDS instance found for {environment}", fg="red")
        raise SystemExit(1)


def _find_redis_endpoint(environment: str, profile: str, region: str) -> tuple[str, int]:
    """Auto-discover the Redis endpoint for the environment."""
    result = run(
        [
            "aws", "elasticache", "describe-replication-groups",
            "--query", f"ReplicationGroups[?contains(ReplicationGroupId, '{environment}')].NodeGroups[0].PrimaryEndpoint.[Address, Port] | [0]",
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
    result = run(
        [
            "aws", "mq", "list-brokers",
            "--query", f"BrokerSummaries[?contains(BrokerName, '{environment}')].[BrokerId, BrokerName] | [0]",
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
        broker_id = data[0]
    except (json.JSONDecodeError, IndexError, TypeError):
        click.secho(f"No RabbitMQ broker found for {environment}", fg="red")
        raise SystemExit(1)

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


@connect.command()
@click.argument("environment", required=False, type=ENVIRONMENT)
@click.option("--local-port", "-p", default=5432, help="Local port.", show_default=True)
@click.option("--profile", default=None, help="AWS profile.")
def db(environment, local_port, profile):
    """Connect to RDS (PostgreSQL).

    \b
    Examples:
      heyamara connect db dev
      heyamara connect db dev -p 5433

    Then connect with:
      psql -h localhost -p <local-port> -U <user> <database>
    """
    if not environment:
        environment = select("Select environment:", ENVS)
    profile, region = _resolve_profile(profile)
    require_aws_session(profile)

    instance_id = _find_eks_node(environment, profile, region)
    rds_host, rds_port = _find_rds_endpoint(environment, profile, region)

    click.secho(f"Tunneling localhost:{local_port} -> {rds_host}:{rds_port}", fg="green")
    click.secho(f"Connect with: psql -h localhost -p {local_port} -U <user> <database>", fg="cyan")
    click.echo("Press Ctrl+C to stop.\n")

    _start_tunnel(instance_id, rds_host, rds_port, local_port, profile, region)


@connect.command()
@click.argument("environment", required=False, type=ENVIRONMENT)
@click.option("--local-port", "-p", default=6379, help="Local port.", show_default=True)
@click.option("--profile", default=None, help="AWS profile.")
def redis(environment, local_port, profile):
    """Connect to Redis (ElastiCache).

    \b
    Examples:
      heyamara connect redis dev
      heyamara connect redis dev -p 6380

    Then connect with:
      redis-cli -h localhost -p <local-port>
    """
    if not environment:
        environment = select("Select environment:", ENVS)
    profile, region = _resolve_profile(profile)
    require_aws_session(profile)

    instance_id = _find_eks_node(environment, profile, region)
    redis_host, redis_port = _find_redis_endpoint(environment, profile, region)

    click.secho(f"Tunneling localhost:{local_port} -> {redis_host}:{redis_port}", fg="green")
    click.secho(f"Connect with: redis-cli -h localhost -p {local_port}", fg="cyan")
    click.echo("Press Ctrl+C to stop.\n")

    _start_tunnel(instance_id, redis_host, redis_port, local_port, profile, region)


@connect.command()
@click.argument("environment", required=False, type=ENVIRONMENT)
@click.option("--local-port", "-p", default=15672, help="Local port.", show_default=True)
@click.option("--profile", default=None, help="AWS profile.")
def rabbitmq(environment, local_port, profile):
    """Connect to RabbitMQ Management UI.

    \b
    Examples:
      heyamara connect rabbitmq dev

    Then open in browser:
      https://localhost:15672
    """
    if not environment:
        environment = select("Select environment:", ENVS)
    profile, region = _resolve_profile(profile)
    require_aws_session(profile)

    instance_id = _find_eks_node(environment, profile, region)
    mq_host, mq_port = _find_rabbitmq_endpoint(environment, profile, region)

    click.secho(f"Tunneling localhost:{local_port} -> {mq_host}:{mq_port}", fg="green")
    click.secho(f"Open in browser: https://localhost:{local_port}", fg="cyan")
    click.echo("Press Ctrl+C to stop.\n")

    _start_tunnel(instance_id, mq_host, mq_port, local_port, profile, region)
