import configparser
import os

import click

from heyamara_cli import config
from heyamara_cli.config import SECRET_KEYS
from heyamara_cli.prompts import select
from heyamara_cli.secret_files import UnsafeSecretFileError

SECRET_PROMPTS = {
    "grafana_token": "Grafana service account token",
}


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
@click.option(
    "--from-env",
    "from_env",
    metavar="ENV_VAR",
    help="Read the config value from an environment variable instead of argv or a prompt.",
)
@click.argument("key", required=False)
@click.argument("value", required=False)
def set_config(key, value, from_env):
    """Set a config value.

    \b
    Examples:
      heyamara config set                         # Interactive
      heyamara config set aws_profile             # Select from AWS profiles
      heyamara config set aws_profile myprofile   # Direct set
      heyamara config set grafana_token --from-env GRAFANA_TOKEN
    """
    keys = list(config.DEFAULTS.keys())

    if from_env and not key:
        click.secho("--from-env requires an explicit config key.", fg="red", err=True)
        raise SystemExit(1)

    if not key:
        key = select("Select setting:", keys)
    elif key not in keys:
        click.secho(f"Unknown key: {key}. Choose from: {', '.join(keys)}", fg="red")
        raise SystemExit(1)

    if from_env:
        if value is not None:
            click.secho("Pass either a positional value or --from-env, not both.", fg="red", err=True)
            raise SystemExit(1)
        if from_env not in os.environ:
            click.secho(f"Environment variable not set: {from_env}", fg="red", err=True)
            raise SystemExit(1)
        value = os.environ[from_env]

    if key in SECRET_KEYS and value is not None:
        if not from_env:
            click.secho(
                f"{key} is secret; enter it at the hidden prompt or use --from-env instead of argv.",
                fg="red",
                err=True,
            )
            raise SystemExit(1)

    if value is None:
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
        elif key in SECRET_KEYS:
            value = click.prompt(SECRET_PROMPTS.get(key, key.replace("_", " ")), hide_input=True)
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
