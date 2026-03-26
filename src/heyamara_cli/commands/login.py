import click

from heyamara_cli import config
from heyamara_cli.helpers import require_tool, run


@click.command()
@click.option("--profile", default=None, help="AWS profile name. Uses configured default if not set.")
def login(profile):
    """Login to AWS via SSO."""
    profile = profile or config.get("aws_profile")
    require_tool("aws", "brew install awscli")
    click.echo(f"Logging in with AWS profile: {profile}")
    run(["aws", "sso", "login", "--profile", profile])
    click.secho("AWS login successful.", fg="green")
