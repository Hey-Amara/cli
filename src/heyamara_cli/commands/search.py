"""Search historical logs via Loki/Grafana."""

import re

import click

from heyamara_cli import config
from heyamara_cli import loki as loki_client
from heyamara_cli.completions import ENVIRONMENT, SERVICE
from heyamara_cli.config import NAMESPACES, SERVICES
from heyamara_cli.prompts import select


ENVS = list(NAMESPACES.keys())


@click.command()
@click.argument("environment", required=False, type=ENVIRONMENT)
@click.argument("service", required=False, type=SERVICE)
# Time filters
@click.option("--since", default=None, help="Relative time window (e.g. 1h, 30m, 2d). Default: 1h.")
@click.option("--after", default=None, metavar="TIMESTAMP", help="Start of time range (ISO 8601).")
@click.option("--before", default=None, metavar="TIMESTAMP", help="End of time range (ISO 8601).")
@click.option("--between", nargs=2, default=None, metavar="START END", help="Absolute time range.")
# Filters
@click.option("--grep", "-g", default=None, help="Filter by substring or regex (server-side LogQL).")
@click.option("--level", default=None,
              type=click.Choice(["error", "warn", "info", "debug", "trace"], case_sensitive=False),
              help="Filter by log level.")
@click.option("--limit", "-n", default=100, show_default=True, help="Maximum entries to return.")
# Output
@click.option("--json", "use_json", is_flag=True, help="Pretty-print JSON entries with syntax highlighting.")
@click.option("--raw", is_flag=True, help="Show the LogQL query being executed.")
@click.option("--follow", "-f", is_flag=True, help="Tail mode: poll Loki for new entries.")
def search(environment, service, since, after, before, between, grep, level, limit, use_json, raw, follow):
    """Search historical logs via Loki.

    \b
    Examples:
      heyamara search dev ats-backend --since 1h
      heyamara search production ats-backend --grep "Request error" --level error
      heyamara search dev ai-backend --between 2026-04-01 2026-04-02
      heyamara search dev ats-backend --since 30m --json
      heyamara search dev ats-backend --follow
    """
    # Validate time options
    time_flags = [since, after, between]
    if sum(v is not None for v in time_flags) > 1:
        raise click.UsageError("Use only one of --since, --after, or --between.")

    if not environment:
        environment = select("Select environment:", ENVS)
    if not service:
        service = select("Select service:", SERVICES)

    # Check Grafana config
    grafana_url = config.get("grafana_url")
    grafana_token = config.get("grafana_token")
    if not grafana_token:
        click.secho("Grafana token not configured.", fg="red")
        click.secho("Run: heyamara config set grafana_token", fg="yellow")
        raise SystemExit(1)

    # When --json + --grep: query WITHOUT grep (to get full blocks), reassemble,
    # then filter blocks client-side. Otherwise, use server-side LogQL grep.
    client_grep = None
    query_limit = limit
    if use_json and grep:
        client_grep = grep
        logql = loki_client.build_logql(environment, service, grep=None, level=level)
        # Need more raw entries since client-side filtering reduces the count
        query_limit = limit * 50
    else:
        logql = loki_client.build_logql(environment, service, grep=grep, level=level)

    start_ns, end_ns = loki_client.parse_time_range(since, after, before, between)

    if raw:
        click.secho(f"LogQL: {logql}", fg="bright_black", err=True)

    # Follow mode
    if follow:
        click.secho(f"Following {service} in {environment}... (Ctrl+C to stop)", fg="cyan", err=True)
        try:
            loki_client.stream_loki(grafana_url, grafana_token, logql, start_ns, limit, use_json, raw)
        except loki_client.LokiError as e:
            _handle_loki_error(e)
        return

    # One-shot query
    click.secho(f"Searching {service} in {environment}...", fg="cyan", err=True)

    try:
        entries = loki_client.query_loki(grafana_url, grafana_token, logql, start_ns, end_ns, query_limit)
    except loki_client.LokiError as e:
        _handle_loki_error(e)
        raise SystemExit(1)

    if not entries:
        click.secho("No log entries found.", fg="yellow", err=True)
        return

    entries = loki_client.reassemble_json_blocks(entries)

    # Client-side grep on reassembled blocks (for --json + --grep)
    if client_grep:
        pattern = re.compile(client_grep, re.IGNORECASE)
        entries = [e for e in entries if pattern.search(e["raw_line"])][:limit]
        if not entries:
            click.secho("No matching entries found.", fg="yellow", err=True)
            return

    for entry in entries:
        click.echo(loki_client.format_entry(entry, use_json=use_json))

    click.secho(f"\n{len(entries)} entries.", dim=True, err=True)


def _handle_loki_error(e: loki_client.LokiError) -> None:
    """Print user-friendly Loki error messages."""
    if e.status == 401:
        click.secho("Grafana auth failed. Check your token:", fg="red")
        click.secho("  heyamara config set grafana_token", fg="yellow")
    elif e.status == 400:
        click.secho(f"Bad query: {e.body}", fg="red")
    else:
        click.secho(f"Loki error: {e}", fg="red")
