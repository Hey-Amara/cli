import base64
import gzip
import os

import click

from amara_cli import config
from amara_cli.completions import ENVIRONMENT, SERVICE
from amara_cli.config import SERVICES, SSM_PREFIX
from amara_cli.helpers import require_aws_session, run
from amara_cli.prompts import select

ENVS = ["dev", "staging", "production"]


def _get_ssm_param(ssm_path: str, profile: str, region: str, environment: str = ""):
    """Fetch a single SSM parameter."""
    return run(
        [
            "aws", "ssm", "get-parameter",
            "--name", ssm_path,
            "--with-decryption",
            "--query", "Parameter.Value",
            "--output", "text",
            "--region", region,
            "--profile", profile,
        ],
        capture=True,
        check=False,
        environment=environment,
    )


def _resolve(profile: str) -> tuple[str, str]:
    """Resolve profile and region from args or user config."""
    p = profile or config.get("aws_profile")
    r = config.get("aws_region")
    return p, r


def _decode(raw: str) -> str:
    """Decode a gzip+base64 encoded SSM value. Falls back to plain text."""
    try:
        decoded = base64.b64decode(raw)
        return gzip.decompress(decoded).decode("utf-8")
    except Exception:
        return raw


@click.group()
def env():
    """Manage service environment variables via AWS SSM."""
    pass


@env.command()
@click.argument("service", required=False, type=SERVICE)
@click.argument("environment", required=False, type=ENVIRONMENT)
@click.option("--output", "-o", default=None, help="Output file path. Defaults to ./<service>.<env>.env")
@click.option("--profile", default=None, help="AWS profile. Uses configured default if not set.")
def pull(service, environment, output, profile):
    """Download .env file for a service from SSM.

    \b
    Examples:
      heyamara env pull                          # Interactive
      heyamara env pull ats-backend              # Interactive env selection
      heyamara env pull ats-backend dev          # Direct
      heyamara env pull ats-backend dev -o .env  # Custom output path
    """
    if not service:
        service = select("Select service:", SERVICES)
    elif service not in SERVICES:
        click.secho(f"Unknown service: {service}. Choose from: {', '.join(SERVICES)}", fg="red")
        raise SystemExit(1)

    if not environment:
        environment = select("Select environment:", ENVS)
    env_name = environment

    profile, region = _resolve(profile)
    require_aws_session(profile)

    ssm_path = f"{SSM_PREFIX}/{env_name}/{service}"
    click.echo(f"Fetching {ssm_path} ...")

    result = _get_ssm_param(ssm_path, profile, region, environment=env_name)

    if result.returncode != 0:
        click.secho(f"Parameter not found: {ssm_path}", fg="red")
        if "ParameterNotFound" in (result.stderr or ""):
            click.echo("Run the sync-env-to-ssm workflow first to populate SSM.")
        raise SystemExit(1)

    env_content = _decode(result.stdout.strip())

    out_path = output or f"{service}.{env_name}.env"
    with open(out_path, "w") as f:
        f.write(env_content)
        if not env_content.endswith("\n"):
            f.write("\n")

    line_count = len([line for line in env_content.splitlines() if line.strip()])
    click.secho(f"Written {out_path} ({line_count} vars)", fg="green")


@env.command("pull-all")
@click.argument("environment", required=False, type=ENVIRONMENT)
@click.option("--output-dir", "-d", default=".env-files", help="Output directory.", show_default=True)
@click.option("--profile", default=None, help="AWS profile. Uses configured default if not set.")
def pull_all(environment, output_dir, profile):
    """Download .env files for all services from SSM.

    \b
    Examples:
      heyamara env pull-all              # Interactive
      heyamara env pull-all dev          # Pull all dev env files
      heyamara env pull-all dev -d ./envs  # Custom output directory
    """
    if not environment:
        environment = select("Select environment:", ENVS)
    env_name = environment

    profile, region = _resolve(profile)
    require_aws_session(profile)
    os.makedirs(output_dir, exist_ok=True)

    success = 0
    for service in SERVICES:
        ssm_path = f"{SSM_PREFIX}/{env_name}/{service}"
        result = _get_ssm_param(ssm_path, profile, region, environment=env_name)

        if result.returncode != 0:
            click.secho(f"  {service}: not found, skipping", fg="yellow")
            continue

        env_content = _decode(result.stdout.strip())

        out_path = os.path.join(output_dir, f"{service}.{env_name}.env")
        with open(out_path, "w") as f:
            f.write(env_content)
            if not env_content.endswith("\n"):
                f.write("\n")

        line_count = len([line for line in env_content.splitlines() if line.strip()])
        click.echo(f"  {service}: {line_count} vars")
        success += 1

    click.secho(f"\nPulled {success}/{len(SERVICES)} services to {output_dir}/", fg="green")


@env.command()
@click.argument("service", required=False, type=SERVICE)
@click.argument("environment", required=False, type=ENVIRONMENT)
@click.option("--profile", default=None, help="AWS profile. Uses configured default if not set.")
def show(service, environment, profile):
    """Display current SSM env vars for a service (without saving to file).

    \b
    Examples:
      heyamara env show                         # Interactive
      heyamara env show ats-backend             # Interactive env selection
      heyamara env show ai-backend production   # Direct
    """
    if not service:
        service = select("Select service:", SERVICES)
    elif service not in SERVICES:
        click.secho(f"Unknown service: {service}. Choose from: {', '.join(SERVICES)}", fg="red")
        raise SystemExit(1)

    if not environment:
        environment = select("Select environment:", ENVS)
    env_name = environment

    profile, region = _resolve(profile)
    require_aws_session(profile)

    ssm_path = f"{SSM_PREFIX}/{env_name}/{service}"
    result = _get_ssm_param(ssm_path, profile, region, environment=env_name)

    if result.returncode != 0:
        click.secho(f"Parameter not found: {ssm_path}", fg="red")
        raise SystemExit(1)

    env_content = _decode(result.stdout.strip())

    click.secho(f"# {ssm_path}", fg="cyan")
    click.echo(env_content)
