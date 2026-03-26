import platform
import shutil

import click

from amara_cli.helpers import run

TOOLS = {
    "aws": {
        "brew": "awscli",
        "desc": "AWS CLI",
    },
    "kubectl": {
        "brew": "kubectl",
        "desc": "Kubernetes CLI",
    },
    "k9s": {
        "brew": "derailed/k9s/k9s",
        "desc": "Terminal UI for Kubernetes",
    },
    "helm": {
        "brew": "helm",
        "desc": "Kubernetes package manager",
    },
    "helmfile": {
        "brew": "helmfile",
        "desc": "Declarative Helm chart manager",
    },
    "sops": {
        "brew": "sops",
        "desc": "Secrets encryption tool",
    },
    "yq": {
        "brew": "yq",
        "desc": "YAML processor",
    },
    "jq": {
        "brew": "jq",
        "desc": "JSON processor",
    },
}


@click.command()
@click.option("--check", is_flag=True, help="Only check tools, don't install.")
def setup(check):
    """Install or check all required developer tools."""
    is_mac = platform.system() == "Darwin"
    has_brew = shutil.which("brew") is not None

    if not check and is_mac and not has_brew:
        click.secho("Homebrew is not installed. Install it from https://brew.sh", fg="red")
        raise SystemExit(1)

    missing = []
    for name, info in TOOLS.items():
        installed = shutil.which(name) is not None
        status = click.style("installed", fg="green") if installed else click.style("missing", fg="red")
        click.echo(f"  {info['desc']:.<40s} {status}")
        if not installed:
            missing.append(name)

    if not missing:
        click.echo()
        click.secho("All tools are installed.", fg="green")
        return

    if check:
        click.echo()
        click.secho(f"{len(missing)} tool(s) missing. Run 'heyamara setup' to install.", fg="yellow")
        return

    if not is_mac:
        click.echo()
        click.secho("Auto-install is only supported on macOS (Homebrew).", fg="yellow")
        click.echo("Please install the missing tools manually:")
        for name in missing:
            click.echo(f"  - {name}: {TOOLS[name]['desc']}")
        return

    click.echo()
    click.echo(f"Installing {len(missing)} missing tool(s)...")
    for name in missing:
        formula = TOOLS[name]["brew"]
        click.echo(f"  Installing {name}...")
        run(["brew", "install", formula])

    click.echo()
    click.secho("Setup complete.", fg="green")
