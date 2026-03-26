import click

from heyamara_cli import config
from heyamara_cli.completions import ENVIRONMENT
from heyamara_cli.config import CLUSTERS, NAMESPACES
from heyamara_cli.helpers import check_tool, require_aws_session, run
from heyamara_cli.prompts import select


@click.command()
@click.argument("environment", required=False, type=ENVIRONMENT)
@click.option("--profile", default=None, help="AWS profile. Uses configured default if not set.")
@click.option("--no-k9s", is_flag=True, help="Only configure kubectl, don't open k9s.")
def cluster(environment, profile, no_k9s):
    """Configure kubectl and open k9s for a cluster.

    \b
    Examples:
      heyamara cluster dev          # Configure + open k9s for dev
      heyamara cluster production --no-k9s  # Only configure kubectl for production
    """
    if not environment:
        environment = select("Select environment:", list(CLUSTERS.keys()))
    elif environment not in CLUSTERS:
        click.secho(f"Unknown environment: {environment}", fg="red")
        raise SystemExit(1)

    profile = profile or config.get("aws_profile")
    region = config.get("aws_region")
    require_aws_session(profile)

    cluster_name = CLUSTERS[environment]
    namespace = NAMESPACES[environment]

    click.echo(f"Configuring kubectl for {environment} ({cluster_name})...")
    run([
        "aws", "eks", "update-kubeconfig",
        "--name", cluster_name,
        "--region", region,
        "--profile", profile,
        "--alias", environment,
    ])
    click.secho(f"kubectl configured. Context alias: {environment}", fg="green")

    if no_k9s:
        click.echo(f"  kubectl get pods -n {namespace} --context {environment}")
        return

    if not check_tool("k9s"):
        click.secho("k9s is not installed. Run 'heyamara setup' to install.", fg="yellow")
        click.echo(f"  kubectl get pods -n {namespace} --context {environment}")
        return

    click.echo(f"Opening k9s for {environment}...")
    run(["k9s", "--context", environment, "--namespace", namespace])
