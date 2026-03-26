import configparser
import os

import click

from amara_cli import config
from amara_cli.prompts import select


def _list_aws_profiles() -> list[str]:
    """Read available profiles from ~/.aws/config and ~/.aws/credentials."""
    profiles = set()

    # Parse ~/.aws/config (profiles are [profile xxx] or [default])
    # Skip [sso-session xxx] sections — those aren't usable profiles
    aws_config = os.path.expanduser("~/.aws/config")
    if os.path.exists(aws_config):
        parser = configparser.ConfigParser()
        parser.read(aws_config)
        for section in parser.sections():
            if section.startswith("sso-session "):
                continue
            elif section == "default":
                profiles.add("default")
            elif section.startswith("profile "):
                profiles.add(section.removeprefix("profile "))

    # Parse ~/.aws/credentials (profiles are [xxx])
    aws_creds = os.path.expanduser("~/.aws/credentials")
    if os.path.exists(aws_creds):
        parser = configparser.ConfigParser()
        parser.read(aws_creds)
        for section in parser.sections():
            profiles.add(section)

    return sorted(profiles) if profiles else []


@click.group("config")
def config_cmd():
    """View or set CLI configuration (stored in ~/.heyamara/config.json)."""
    pass


@config_cmd.command("set")
@click.argument("key", required=False)
@click.argument("value", required=False)
def set_config(key, value):
    """Set a config value.

    \b
    Examples:
      heyamara config set                         # Interactive
      heyamara config set aws_profile             # Select from AWS profiles
      heyamara config set aws_profile myprofile   # Direct set
    """
    keys = list(config.DEFAULTS.keys())

    if not key:
        key = select("Select setting:", keys)
    elif key not in keys:
        click.secho(f"Unknown key: {key}. Choose from: {', '.join(keys)}", fg="red")
        raise SystemExit(1)

    if not value:
        if key == "aws_profile":
            profiles = _list_aws_profiles()
            if profiles:
                value = select("Select AWS profile:", profiles)
            else:
                click.secho("No AWS profiles found in ~/.aws/config or ~/.aws/credentials", fg="yellow")
                value = click.prompt("Enter profile name")
        else:
            value = click.prompt(f"Enter value for {key}")

    cfg = config.load_user_config()
    cfg[key] = value
    config.save_user_config(cfg)
    click.secho(f"{key} = {value}", fg="green")


@config_cmd.command("get")
@click.argument("key", required=False)
def get_config(key):
    """Show config values.

    \b
    Examples:
      heyamara config get              # Show all
      heyamara config get aws_profile  # Show one
    """
    cfg = config.load_user_config()
    if key:
        if key in cfg:
            click.echo(f"{key} = {cfg[key]}")
        else:
            click.secho(f"Unknown key: {key}", fg="red")
            raise SystemExit(1)
    else:
        click.secho(f"Config file: {config.CONFIG_FILE}", fg="cyan")
        for k, v in sorted(cfg.items()):
            default = " (default)" if k in config.DEFAULTS and v == config.DEFAULTS[k] else ""
            click.echo(f"  {k} = {v}{default}")
