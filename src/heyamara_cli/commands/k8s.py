import re
import subprocess
from collections import deque
from datetime import datetime, timezone

import click

from heyamara_cli.completions import ENVIRONMENT, SERVICE
from heyamara_cli.config import NAMESPACES, SERVICES, SUB_SERVICES
from heyamara_cli.helpers import debug, require_tool, run
from heyamara_cli.prompts import confirm, select


ENVS = list(NAMESPACES.keys())

_DURATION_RE = re.compile(r"^\d+[smhd]$")


def _parse_duration(value: str) -> str:
    """Validate and return a kubectl-compatible duration string (e.g. '5m', '1h', '2d')."""
    if not _DURATION_RE.match(value):
        raise click.BadParameter(
            f"Invalid duration '{value}'. Use format: 5m, 1h, 2d, 30s",
            param_hint="'--since'",
        )
    return value


def _parse_timestamp(value: str) -> str:
    """Parse an ISO 8601 timestamp and return RFC 3339 UTC string for kubectl."""
    formats = [
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    # Try fromisoformat for +HH:MM offset variants
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise click.BadParameter(
            f"Cannot parse '{value}'. Use ISO 8601: 2024-01-15T10:30:00Z or 2024-01-15",
            param_hint="timestamp",
        )


def _to_utc_dt(value: str) -> datetime:
    """Convert an ISO 8601 string to a timezone-aware UTC datetime."""
    ts = _parse_timestamp(value)
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _stream_filtered(cmd, before_dt=None, grep_pattern=None, grep_context=0, json_mode=False):
    """Stream kubectl output with optional before-timestamp cutoff and grep filtering.

    Reads line-by-line from the subprocess stdout, applying:
    - before_dt: stop printing once a line's timestamp >= this datetime
    - grep_pattern: only print lines matching this regex (with optional context)
    - json_mode: when True with grep, buffer JSON blocks and grep against the full block
    """
    compiled = re.compile(grep_pattern) if grep_pattern else None
    context_buf = deque(maxlen=grep_context) if grep_context > 0 else None
    pending_after = 0
    last_was_match = False

    debug(f"Streaming with filters: before={before_dt}, grep={grep_pattern}, context={grep_context}, json={json_mode}")

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    try:
        if json_mode and compiled:
            # JSON block-aware grep path
            json_buf = []
            brace_depth = 0
            found_any = False

            for line in proc.stdout:
                line = line.rstrip("\n")

                # --before cutoff
                if before_dt:
                    ts_match = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[^\s]*)\s", line)
                    if ts_match:
                        try:
                            line_dt = datetime.fromisoformat(
                                ts_match.group(1).replace("Z", "+00:00")
                            )
                            if line_dt >= before_dt:
                                break
                        except ValueError:
                            pass

                stripped = line.strip()

                if not json_buf:
                    if stripped.startswith("{"):
                        json_buf.append(line)
                        brace_depth = stripped.count("{") - stripped.count("}")
                        if brace_depth <= 0:
                            block_text = "\n".join(json_buf)
                            if compiled.search(block_text):
                                if found_any:
                                    click.echo("--")
                                click.echo(block_text)
                                found_any = True
                            json_buf = []
                            brace_depth = 0
                    else:
                        # Plain text line — grep directly
                        if compiled.search(line):
                            click.echo(line)
                            found_any = True
                else:
                    json_buf.append(line)
                    brace_depth += stripped.count("{") - stripped.count("}")
                    if brace_depth <= 0:
                        block_text = "\n".join(json_buf)
                        if compiled.search(block_text):
                            if found_any:
                                click.echo("--")
                            click.echo(block_text)
                            found_any = True
                        json_buf = []
                        brace_depth = 0
        else:
            # Standard line-by-line filter path
            for line in proc.stdout:
                line = line.rstrip("\n")

                # --before: parse timestamp from line and stop if past cutoff
                if before_dt:
                    ts_match = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[^\s]*)\s", line)
                    if ts_match:
                        try:
                            line_dt = datetime.fromisoformat(
                                ts_match.group(1).replace("Z", "+00:00")
                            )
                            if line_dt >= before_dt:
                                break
                        except ValueError:
                            pass

                # --grep with optional context
                if compiled:
                    if compiled.search(line):
                        # Print separator between non-contiguous match groups
                        if context_buf and not last_was_match and context_buf:
                            click.echo("--")
                        # Print buffered before-context
                        if context_buf:
                            for ctx_line in context_buf:
                                click.echo(ctx_line)
                            context_buf.clear()
                        click.echo(line)
                        pending_after = grep_context
                        last_was_match = True
                    elif pending_after > 0:
                        click.echo(line)
                        pending_after -= 1
                        last_was_match = pending_after > 0
                    else:
                        if context_buf is not None:
                            context_buf.append(line)
                        last_was_match = False
                else:
                    click.echo(line)
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


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
# Time filtering
@click.option("--since", "since", default=None, help="Show logs since a relative duration (e.g. 5m, 1h, 2d).")
@click.option("--after", default=None, metavar="TIMESTAMP", help="Show logs after an absolute timestamp (ISO 8601).")
@click.option("--before", default=None, metavar="TIMESTAMP", help="Show logs before an absolute timestamp (client-side filter).")
@click.option("--between", nargs=2, default=None, metavar="START END", help="Show logs between two timestamps.")
# Search
@click.option("--grep", "-g", default=None, help="Filter log lines matching a regex pattern.")
@click.option("--grep-context", default=0, metavar="N", type=int, help="Show N lines around each grep match.")
# Additional kubectl flags
@click.option("--timestamps/--no-timestamps", "-t", default=False, help="Show timestamps on each line.")
@click.option("--container", "-c", default=None, help="Target a specific container in multi-container pods.")
@click.option("--previous/--no-previous", default=False, help="Show logs from previous (crashed) container.")
@click.option("--prefix/--no-prefix", default=False, help="Prefix each line with the pod name.")
@click.option("--json", "json_mode", is_flag=True, help="JSON-aware grep: match full JSON blocks, not individual lines.")
def logs(environment, service, tail, follow,
         since, after, before, between,
         grep, grep_context,
         timestamps, container, previous, prefix, json_mode):
    """Tail logs for a service.

    \b
    Examples:
      heyamara logs dev ats-backend
      heyamara logs dev ai-backend -n 50
      heyamara logs dev ats-backend --no-follow
      heyamara logs dev ats-backend --since 5m --no-follow
      heyamara logs dev ats-backend --after 2024-01-15T00:00:00Z
      heyamara logs dev ats-backend --between 2024-01-15T00:00:00Z 2024-01-15T01:00:00Z
      heyamara logs dev ats-backend --grep ERROR --no-follow
      heyamara logs dev ats-backend --grep "timeout|refused" --grep-context 3
      heyamara logs dev ats-backend --previous
      heyamara logs dev ats-backend -t --prefix
    """
    # ---- Validation ----
    time_flags = [since, after, between]
    if sum(v is not None for v in time_flags) > 1:
        raise click.UsageError("Use only one of --since, --after, or --between.")
    if before and since:
        raise click.UsageError("--before cannot be combined with --since (use --between instead).")

    # --between expands to after + before
    if between:
        after, before = between[0], between[1]

    # --before needs timestamps to parse line times, and cannot follow
    before_dt = None
    if before:
        before_dt = _to_utc_dt(before)
        if after:
            after_dt = _to_utc_dt(after)
            if after_dt >= before_dt:
                raise click.UsageError("--between start must be before end.")
        if not timestamps:
            timestamps = True
        if follow:
            click.secho("Note: --before implies --no-follow.", fg="yellow")
            follow = False

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
    if timestamps:
        cmd.append("--timestamps")
    if container:
        cmd.extend(["-c", container])
    if previous:
        cmd.append("--previous")
    if prefix:
        cmd.append("--prefix")

    # Time filters
    if since:
        cmd.append(f"--since={_parse_duration(since)}")
    elif after:
        cmd.append(f"--since-time={_parse_timestamp(after)}")

    click.echo(f"Tailing logs for {display_name} in {environment}...")

    # Route to filtered stream or direct run
    needs_filter = before_dt is not None or grep is not None
    if needs_filter:
        _stream_filtered(cmd, before_dt=before_dt, grep_pattern=grep, grep_context=grep_context, json_mode=json_mode)
    else:
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


@click.command()
@click.argument("environment", required=False, type=ENVIRONMENT)
@click.option("--watch", "-w", is_flag=True, help="Watch for new events continuously.")
@click.option("--warnings-only", is_flag=True, help="Show only Warning-type events.")
def events(environment, watch, warnings_only):
    """Show Kubernetes events for an environment.

    \b
    Examples:
      heyamara events dev
      heyamara events production --warnings-only
      heyamara events dev -w
    """
    if not environment:
        environment = select("Select environment:", ENVS)

    require_tool("kubectl")
    namespace = NAMESPACES[environment]

    cmd = [
        "kubectl", "get", "events",
        "-n", namespace,
        "--context", environment,
        "--sort-by=.lastTimestamp",
    ]

    if watch:
        cmd.append("-w")
    if warnings_only:
        cmd.extend(["--field-selector", "type=Warning"])

    click.echo(f"Events in {environment}...")
    run(cmd)


@click.command()
@click.argument("environment", required=False, type=ENVIRONMENT)
@click.argument("service", required=False, type=SERVICE)
@click.option("--sort", default="cpu", type=click.Choice(["cpu", "memory"]), help="Sort by resource.", show_default=True)
def top(environment, service, sort):
    """Show CPU and memory usage for pods.

    \b
    Examples:
      heyamara top dev
      heyamara top dev ats-backend
      heyamara top production --sort memory
    """
    if not environment:
        environment = select("Select environment:", ENVS)

    require_tool("kubectl")
    namespace = NAMESPACES[environment]

    cmd = [
        "kubectl", "top", "pods",
        "-n", namespace,
        "--context", environment,
        "--sort-by", sort,
    ]

    if service:
        label_selector, display_name = _resolve_service(service)
        cmd.extend(["-l", label_selector])
        click.echo(f"Resource usage for {display_name} in {environment}...")
    else:
        click.echo(f"Resource usage in {environment}...")

    run(cmd)


@click.command()
@click.argument("environment", required=False, type=ENVIRONMENT)
@click.argument("service", required=False, type=SERVICE)
def restart(environment, service):
    """Restart a deployment (rolling restart).

    \b
    Examples:
      heyamara restart dev ats-backend
      heyamara restart production ai-backend
    """
    if not environment:
        environment = select("Select environment:", ENVS)
    if not service:
        service = select("Select service:", SERVICES)
    label_selector, display_name = _resolve_service(service)

    require_tool("kubectl")
    namespace = NAMESPACES[environment]

    # Resolve deployment name(s) from label selector
    result = run(
        [
            "kubectl", "get", "deployments",
            "-n", namespace,
            "--context", environment,
            "-l", label_selector,
            "-o", "jsonpath={.items[*].metadata.name}",
        ],
        capture=True,
        check=False,
    )

    if result.returncode != 0 or not result.stdout.strip():
        click.secho(f"No deployments found for {display_name} in {environment}", fg="red")
        raise SystemExit(1)

    deployment_names = result.stdout.strip().split()

    for deployment in deployment_names:
        if not confirm(f"Restart {deployment} in {environment}?"):
            click.echo("Skipped.")
            continue
        click.echo(f"Restarting {deployment}...")
        run([
            "kubectl", "rollout", "restart", f"deployment/{deployment}",
            "-n", namespace, "--context", environment,
        ])
        click.secho(f"{deployment} restart initiated.", fg="green")


@click.command()
@click.argument("environment", required=False, type=ENVIRONMENT)
@click.argument("service", required=False, type=SERVICE)
@click.option("--history", is_flag=True, help="Show rollout history instead of current status.")
def rollout(environment, service, history):
    """Show deployment rollout status or history.

    \b
    Examples:
      heyamara rollout dev ats-backend
      heyamara rollout dev ats-backend --history
    """
    if not environment:
        environment = select("Select environment:", ENVS)
    if not service:
        service = select("Select service:", SERVICES)
    label_selector, display_name = _resolve_service(service)

    require_tool("kubectl")
    namespace = NAMESPACES[environment]

    # Resolve deployment name(s) from label selector
    result = run(
        [
            "kubectl", "get", "deployments",
            "-n", namespace,
            "--context", environment,
            "-l", label_selector,
            "-o", "jsonpath={.items[*].metadata.name}",
        ],
        capture=True,
        check=False,
    )

    if result.returncode != 0 or not result.stdout.strip():
        click.secho(f"No deployments found for {display_name} in {environment}", fg="red")
        raise SystemExit(1)

    deployment_names = result.stdout.strip().split()

    for deployment in deployment_names:
        if history:
            click.echo(f"\nRollout history for {deployment}:")
            run([
                "kubectl", "rollout", "history", f"deployment/{deployment}",
                "-n", namespace, "--context", environment,
            ])
        else:
            click.echo(f"\nRollout status for {deployment}:")
            run([
                "kubectl", "rollout", "status", f"deployment/{deployment}",
                "-n", namespace, "--context", environment,
            ])
