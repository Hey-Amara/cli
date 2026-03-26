import click

from amara_cli.completions import ENVIRONMENT, SERVICE
from amara_cli.config import NAMESPACES, SERVICES
from amara_cli.helpers import require_tool, run
from amara_cli.prompts import select


ENVS = list(NAMESPACES.keys())


@click.command()
@click.argument("environment", required=False, type=ENVIRONMENT)
@click.argument("service", required=False, type=SERVICE)
@click.option("--tail", "-n", default=100, help="Number of lines to tail.", show_default=True)
@click.option("--follow/--no-follow", "-f", default=True, help="Follow log output.", show_default=True)
def logs(environment, service, tail, follow):
    """Tail logs for a service.

    \b
    Examples:
      heyamara logs dev ats-backend
      heyamara logs dev ai-backend -n 50
      heyamara logs dev ats-backend --no-follow
    """
    if not environment:
        environment = select("Select environment:", ENVS)
    if not service:
        service = select("Select service:", SERVICES)

    require_tool("kubectl")
    namespace = NAMESPACES[environment]

    cmd = [
        "kubectl", "logs",
        "-n", namespace,
        "--context", environment,
        "-l", f"app.kubernetes.io/name={service}",
        "--tail", str(tail),
    ]
    if follow:
        cmd.append("-f")

    click.echo(f"Tailing logs for {service} in {environment}...")
    run(cmd)


@click.command()
@click.argument("environment", required=False, type=ENVIRONMENT)
@click.argument("service", required=False, type=SERVICE)
def shell(environment, service):
    """Exec into a running pod.

    \b
    Examples:
      heyamara shell dev ats-backend
    """
    if not environment:
        environment = select("Select environment:", ENVS)
    if not service:
        service = select("Select service:", SERVICES)

    require_tool("kubectl")
    namespace = NAMESPACES[environment]

    result = run(
        [
            "kubectl", "get", "pods",
            "-n", namespace,
            "--context", environment,
            "-l", f"app.kubernetes.io/name={service}",
            "--field-selector", "status.phase=Running",
            "-o", "jsonpath={.items[0].metadata.name}",
        ],
        capture=True,
        check=False,
    )

    if result.returncode != 0 or not result.stdout.strip():
        click.secho(f"No running pod found for {service} in {environment}", fg="red")
        raise SystemExit(1)

    pod_name = result.stdout.strip()
    click.echo(f"Connecting to {pod_name}...")
    run(["kubectl", "exec", "-it", pod_name, "-n", namespace, "--context", environment, "--", "sh"])


@click.command()
@click.argument("environment", required=False, type=ENVIRONMENT)
@click.option("--wide", "-w", is_flag=True, help="Show extra details (node, IP).")
def status(environment, wide):
    """Show pod statuses for an environment.

    \b
    Examples:
      heyamara status dev
      heyamara status production -w
    """
    if not environment:
        environment = select("Select environment:", ENVS)

    require_tool("kubectl")
    namespace = NAMESPACES[environment]

    cmd = [
        "kubectl", "get", "pods",
        "-n", namespace,
        "--context", environment,
    ]
    if wide:
        cmd.extend(["-o", "wide"])

    run(cmd)
