import importlib.metadata

import click

from heyamara_cli import helpers as _helpers
from heyamara_cli.commands.cluster import cluster
from heyamara_cli.commands.completions import completions
from heyamara_cli.commands.config_cmd import config_cmd
from heyamara_cli.commands.connect import connect
from heyamara_cli.commands.env import env
from heyamara_cli.commands.k8s import events, logs, restart, rollout, shell, status, top
from heyamara_cli.commands.login import login
from heyamara_cli.commands.setup import setup
from heyamara_cli.commands.update import update


# ---- Command categories for help display ------------------------------------

COMMAND_CATEGORIES = [
    ("Auth & Setup", ["login", "setup", "whoami", "switch", "doctor"]),
    ("Cluster & Pods", ["cluster", "status", "shell", "logs", "events", "top", "restart", "rollout"]),
    ("Infrastructure", ["connect", "env"]),
    ("Configuration", ["config", "completions", "update", "version", "help"]),
]


class AmaraCLI(click.Group):
    """Custom group with categorized help output."""

    def format_usage(self, ctx, formatter):
        formatter.write_usage(ctx.command_path, "[COMMAND] [ARGS]...")

    def format_commands(self, ctx, formatter):
        """Override to show commands grouped by category with quick workflows."""
        commands = {}
        for subcommand in self.list_commands(ctx):
            cmd = self.get_command(ctx, subcommand)
            if cmd is None or cmd.hidden:
                continue
            commands[subcommand] = cmd

        if not commands:
            return

        # Build categorized output
        categorized = set()
        for category, cmd_names in COMMAND_CATEGORIES:
            rows = []
            for name in cmd_names:
                if name in commands:
                    help_text = commands[name].get_short_help_str(limit=55)
                    rows.append((name, help_text))
                    categorized.add(name)
            if rows:
                with formatter.section(category):
                    formatter.write_dl(rows)

        # Show any uncategorized commands (safety net for new commands)
        uncategorized = [(n, commands[n].get_short_help_str(limit=55))
                         for n in sorted(commands) if n not in categorized]
        if uncategorized:
            with formatter.section("Other"):
                formatter.write_dl(uncategorized)

        # Quick workflows at the bottom
        formatter.write("\n")
        with formatter.section("Quick Workflows"):
            formatter.write_text(
                "First time setup:\n"
                "  heyamara setup && heyamara config set aws_profile\n"
                "\n"
                "Tail logs with search:\n"
                "  heyamara logs dev ats-backend --grep ERROR --since 1h\n"
                "\n"
                "Connect to production DB:\n"
                "  heyamara connect db production --iam -u power_user\n"
                "\n"
                "Check what's failing:\n"
                "  heyamara events dev --warnings-only"
            )


@click.group(cls=AmaraCLI, invoke_without_command=True)
@click.option("--verbose", "-v", is_flag=True, default=False, help="Enable debug output.")
@click.pass_context
def cli(ctx, verbose):
    """Hey Amara developer CLI — cluster access, logs, tunnels, and env management."""
    _helpers.set_verbose(verbose)
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command()
def version():
    """Show the CLI version."""
    ver = importlib.metadata.version("heyamara-cli")
    click.echo(f"heyamara-cli {ver}")


@cli.command("help")
@click.argument("command", required=False)
@click.pass_context
def help_cmd(ctx, command):
    """Show help for a command.

    \b
    Examples:
      heyamara help
      heyamara help logs
      heyamara help connect
    """
    if command:
        # Look up the subcommand and print its help with correct context
        cmd = cli.get_command(ctx, command)
        if cmd:
            sub_ctx = click.Context(cmd, info_name=command, parent=ctx.parent)
            click.echo(cmd.get_help(sub_ctx))
        else:
            click.secho(f"Unknown command: {command}", fg="red")
            click.echo(f"Run 'heyamara help' to see available commands.")
            raise SystemExit(1)
    else:
        click.echo(ctx.parent.get_help())


@cli.command()
@click.option("--profile", default=None, help="AWS profile (overrides config).")
def whoami(profile):
    """Show current AWS identity, profile, and region.

    \b
    Examples:
      heyamara whoami
      heyamara whoami --profile amara-prod
    """
    from heyamara_cli import config

    p = profile or config.get("aws_profile")
    r = config.get("aws_region")

    click.secho("=== CLI Config ===", fg="cyan")
    click.echo(f"  Profile:  {p}")
    click.echo(f"  Region:   {r}")
    click.echo()

    _helpers.require_tool("aws")
    result = _helpers.run(
        ["aws", "sts", "get-caller-identity", "--profile", p, "--output", "json"],
        capture=True,
        check=False,
    )

    if result.returncode != 0:
        click.secho("AWS session is not active. Run: heyamara login", fg="yellow")
        return

    import json
    try:
        identity = json.loads(result.stdout)
    except (json.JSONDecodeError, AttributeError):
        click.secho("Could not parse AWS identity.", fg="red")
        return

    click.secho("=== AWS Identity ===", fg="cyan")
    click.echo(f"  Account:  {identity.get('Account', 'unknown')}")
    click.echo(f"  ARN:      {identity.get('Arn', 'unknown')}")
    click.echo(f"  User ID:  {identity.get('UserId', 'unknown')}")

    role = _helpers.detect_iam_role(identity.get("Arn", ""))
    if role:
        click.echo(f"  Role:     {role}")


@cli.command()
@click.argument("profile", required=False)
def switch(profile):
    """Switch AWS profile (shortcut for config set aws_profile).

    \b
    Examples:
      heyamara switch              # Interactive picker
      heyamara switch amara-prod   # Direct switch
    """
    from heyamara_cli import config
    from heyamara_cli.commands.config_cmd import _list_aws_profiles

    if not profile:
        from heyamara_cli.prompts import select
        profiles = _list_aws_profiles()
        if not profiles:
            click.secho("No AWS profiles found in ~/.aws/config", fg="yellow")
            return
        profile = select("Select AWS profile:", profiles)

    cfg = config.load_user_config()
    old = cfg.get("aws_profile", config.DEFAULTS["aws_profile"])
    cfg["aws_profile"] = profile
    config.save_user_config(cfg)
    click.secho(f"Switched: {old} -> {profile}", fg="green")


@cli.command()
def doctor():
    """Check that all required tools are installed and working.

    \b
    Examples:
      heyamara doctor
    """
    from heyamara_cli.config import REQUIRED_TOOLS, OPTIONAL_TOOLS, CLUSTERS
    from heyamara_cli import config

    click.secho("=== Tool Check ===", fg="cyan")
    all_ok = True
    for tool in REQUIRED_TOOLS:
        if _helpers.check_tool(tool):
            click.secho(f"  {tool:16s} OK", fg="green")
        else:
            click.secho(f"  {tool:16s} MISSING (required)", fg="red")
            all_ok = False

    for tool in OPTIONAL_TOOLS:
        if _helpers.check_tool(tool):
            click.secho(f"  {tool:16s} OK", fg="green")
        else:
            click.secho(f"  {tool:16s} not installed (optional)", fg="yellow")

    click.echo()
    click.secho("=== AWS Session ===", fg="cyan")
    p = config.get("aws_profile")
    click.echo(f"  Profile: {p}")
    result = _helpers.run(
        ["aws", "sts", "get-caller-identity", "--profile", p],
        capture=True,
        check=False,
    )
    if result.returncode == 0:
        click.secho("  Session:  active", fg="green")
    else:
        click.secho("  Session:  expired or invalid", fg="yellow")
        click.echo("  Fix: heyamara login")
        all_ok = False

    click.echo()
    click.secho("=== Kubectl Contexts ===", fg="cyan")
    for env_name, cluster_name in CLUSTERS.items():
        result = _helpers.run(
            ["kubectl", "config", "get-contexts", env_name, "--no-headers"],
            capture=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            click.secho(f"  {env_name:16s} configured ({cluster_name})", fg="green")
        else:
            click.secho(f"  {env_name:16s} not configured", fg="yellow")
            click.echo(f"  Fix: heyamara cluster {env_name}")

    click.echo()
    if all_ok:
        click.secho("All checks passed.", fg="green", bold=True)
    else:
        click.secho("Some checks failed. See above for fixes.", fg="yellow", bold=True)


cli.add_command(setup)
cli.add_command(login)
cli.add_command(env)
cli.add_command(cluster)
cli.add_command(logs)
cli.add_command(shell)
cli.add_command(status)
cli.add_command(events)
cli.add_command(top)
cli.add_command(restart)
cli.add_command(rollout)
cli.add_command(connect)
cli.add_command(config_cmd)
cli.add_command(completions)
cli.add_command(update)


if __name__ == "__main__":
    cli()
