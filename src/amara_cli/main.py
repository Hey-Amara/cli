import importlib.metadata

import click

from amara_cli.commands.cluster import cluster
from amara_cli.commands.completions import completions
from amara_cli.commands.config_cmd import config_cmd
from amara_cli.commands.connect import connect
from amara_cli.commands.env import env
from amara_cli.commands.k8s import logs, shell, status
from amara_cli.commands.login import login
from amara_cli.commands.setup import setup
from amara_cli.commands.update import update


class AmaraCLI(click.Group):
    """Custom group that uses 'help' and 'version' as subcommands instead of --flags."""

    def format_usage(self, ctx, formatter):
        formatter.write_usage(ctx.command_path, "[COMMAND] [ARGS]...")


@click.group(cls=AmaraCLI, invoke_without_command=True)
@click.pass_context
def cli(ctx):
    """Hey Amara developer CLI.

    \b
    Getting started:
      heyamara setup                     Install required tools
      heyamara config set aws_profile X  Set your AWS profile
      heyamara env pull ats-backend      Download .env for a service
      heyamara cluster dev               Connect to dev cluster via k9s
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command()
def version():
    """Show the CLI version."""
    ver = importlib.metadata.version("amara-cli")
    click.echo(f"heyamara-cli {ver}")


@cli.command()
@click.pass_context
def help(ctx):
    """Show this help message."""
    click.echo(ctx.parent.get_help())


cli.add_command(setup)
cli.add_command(login)
cli.add_command(env)
cli.add_command(cluster)
cli.add_command(logs)
cli.add_command(shell)
cli.add_command(status)
cli.add_command(connect)
cli.add_command(config_cmd)
cli.add_command(completions)
cli.add_command(update)


if __name__ == "__main__":
    cli()
