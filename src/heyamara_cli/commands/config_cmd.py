import configparser
import os

import click

from heyamara_cli import config
from heyamara_cli.config import SECRET_KEYS
from heyamara_cli.prompts import select
from heyamara_cli.secret_files import UnsafeSecretFileError


def _mask_secret(value: object, *, show_unset_marker: bool = False) -> str:
    """Return a stable masked display value for a configured secret."""
    text = "" if value is None else str(value)
    if not text:
        return "(not set)" if show_unset_marker else ""
    if len(text) <= 4:
        return "********"
    return f"********{text[-4:]}"


def _display_value(key: str, value: object, *, show_unset_marker: bool = False) -> str:
    """Display config values without leaking secret material."""
    if key in SECRET_KEYS:
        return _mask_secret(value, show_unset_marker=show_unset_marker)
    return "" if value is None else str(value)


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
        elif key == "grafana_url":
            current = config.get("grafana_url")
            value = click.prompt("Grafana URL", default=current)
        elif key == "grafana_token":
            value = click.prompt("Grafana service account token", hide_input=True)
        else:
            value = click.prompt(f"Enter value for {key}")

    cfg = config.load_user_config()
    cfg[key] = value
    try:
        config.save_user_config(cfg)
    except UnsafeSecretFileError as exc:
        click.secho(str(exc), fg="red", err=True)
        raise SystemExit(1) from exc
    click.secho(f"{key} = {_display_value(key, value)}", fg="green")


@config_cmd.command("get")
@click.argument("key", required=False)
def get_config(key):
    """Show config values.

    \b
    Examples:
      heyamara config get              # Show all
      heyamara config get aws_profile  # Show one
      heyamara config get grafana_token  # Show masked token
    """
    cfg = config.load_user_config()
    if key:
        if key in cfg:
            click.echo(f"{key} = {_display_value(key, cfg[key])}")
        else:
            click.secho(f"Unknown key: {key}", fg="red")
            raise SystemExit(1)
    else:
        click.secho(f"Config file: {config.CONFIG_FILE}", fg="cyan")
        for k, v in sorted(cfg.items()):
            display_v = _display_value(k, v, show_unset_marker=True)
            default = ""
            if k not in SECRET_KEYS and k in config.DEFAULTS and v == config.DEFAULTS[k]:
                default = " (default)"
            click.echo(f"  {k} = {display_v}{default}")
