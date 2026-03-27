import click

from heyamara_cli.completions import ENVIRONMENT, SERVICE
from heyamara_cli.config import NAMESPACES, SERVICES, SUB_SERVICES
from heyamara_cli.helpers import require_tool, run
from heyamara_cli.prompts import select


ENVS = list(NAMESPACES.keys())


def _resolve_service(service):
    """If service has sub-services, show a picker. Returns (label_selector, display_name).

    Label strategies vary by service:
    - ai-backend: all sub-services share app.kubernetes.io/name=ai-backend,
      distinguished by app.kubernetes.io/instance (release name)
    - meeting-bot: both deployments share app.kubernetes.io/name=meeting-bot,
      distinguished by app.kubernetes.io/component (api/worker)
    """
    if service not in SUB_SERVICES:
        return f"app.kubernetes.io/name={service}", service

    subs = SUB_SERVICES[service]
    chosen = select(f"Select {service} component:", subs)

    if service == "meeting-bot":
        component = chosen.replace("meeting-bot-", "")
        selector = f"app.kubernetes.io/name=meeting-bot,app.kubernetes.io/component={component}"
    elif service == "ai-backend":
        # AI sub-services all use chart name ai-backend, but have unique instance (release) names
        selector = f"app.kubernetes.io/instance={chosen}"
    else:
        selector = f"app.kubernetes.io/name={chosen}"

    return selector, chosen


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
    label_selector, display_name = _resolve_service(service)

    require_tool("kubectl")
    namespace = NAMESPACES[environment]

    # Check if any pods exist for this service first
    check = run(
        [
            "kubectl", "get", "pods",
            "-n", namespace,
            "--context", environment,
            "-l", label_selector,
            "-o", "jsonpath={.items[0].metadata.name}",
        ],
        capture=True,
        check=False,
    )
    if check.returncode != 0 or not check.stdout.strip():
        click.secho(f"{display_name} is not deployed in {environment}", fg="yellow")
        raise SystemExit(1)

    cmd = [
        "kubectl", "logs",
        "-n", namespace,
        "--context", environment,
        "-l", label_selector,
        "--tail", str(tail),
    ]
    if follow:
        cmd.append("-f")

    click.echo(f"Tailing logs for {display_name} in {environment}...")
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
    label_selector, display_name = _resolve_service(service)

    require_tool("kubectl")
    namespace = NAMESPACES[environment]

    result = run(
        [
            "kubectl", "get", "pods",
            "-n", namespace,
            "--context", environment,
            "-l", label_selector,
            "--field-selector", "status.phase=Running",
            "-o", "jsonpath={.items[0].metadata.name}",
        ],
        capture=True,
        check=False,
    )

    if result.returncode != 0 or not result.stdout.strip():
        click.secho(f"No running pod found for {display_name} in {environment}", fg="red")
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
