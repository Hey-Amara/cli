import os
import platform
import subprocess

import click


def _get_powershell_profile():
    """Get the PowerShell profile path."""
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    for ps_dir in ["PowerShell", "WindowsPowerShell"]:
        profile = os.path.join(docs, ps_dir, "Microsoft.PowerShell_profile.ps1")
        parent = os.path.dirname(profile)
        if os.path.isdir(parent):
            return profile
    return os.path.join(docs, "PowerShell", "Microsoft.PowerShell_profile.ps1")


SHELL_CONFIGS = {
    "zsh": {
        "env_var": "_HEYAMARA_COMPLETE=zsh_source",
        "rc_file": os.path.expanduser("~/.zshrc"),
        "line": 'if command -v heyamara &>/dev/null; then eval "$(_HEYAMARA_COMPLETE=zsh_source heyamara)"; fi',
    },
    "bash": {
        "env_var": "_HEYAMARA_COMPLETE=bash_source",
        "rc_file": os.path.expanduser("~/.bashrc"),
        "line": 'if command -v heyamara &>/dev/null; then eval "$(_HEYAMARA_COMPLETE=bash_source heyamara)"; fi',
    },
    "fish": {
        "env_var": "_HEYAMARA_COMPLETE=fish_source",
        "rc_file": os.path.expanduser("~/.config/fish/completions/heyamara.fish"),
        "line": "_HEYAMARA_COMPLETE=fish_source heyamara | source",
    },
    "powershell": {
        "env_var": "_HEYAMARA_COMPLETE=powershell_source",
        "rc_file": _get_powershell_profile(),
        "line": '$env:_HEYAMARA_COMPLETE="powershell_source"; heyamara | Invoke-Expression',
    },
}


def _detect_shell() -> str:
    if platform.system() == "Windows":
        return "powershell"
    shell = os.environ.get("SHELL", "")
    if "zsh" in shell:
        return "zsh"
    elif "fish" in shell:
        return "fish"
    return "bash"


@click.command()
@click.option("--shell", "shell_name", type=click.Choice(list(SHELL_CONFIGS.keys())), default=None,
              help="Shell type. Auto-detected if not set.")
@click.option("--print-only", is_flag=True, help="Print the completion script instead of installing.")
def completions(shell_name, print_only):
    """Enable shell auto-completions for the heyamara CLI.

    \b
    Examples:
      heyamara completions             # Auto-detect shell and install
      heyamara completions --shell zsh
      heyamara completions --shell powershell
      heyamara completions --print-only
    """
    shell_name = shell_name or _detect_shell()
    cfg = SHELL_CONFIGS[shell_name]

    if print_only:
        env_key, env_val = cfg["env_var"].split("=", 1)
        result = subprocess.run(
            ["heyamara"],
            env={**os.environ, env_key: env_val},
            capture_output=True,
            text=True,
        )
        click.echo(result.stdout)
        return

    rc_file = cfg["rc_file"]
    line = cfg["line"]

    # Check if already installed
    if os.path.exists(rc_file):
        with open(rc_file) as f:
            if line in f.read():
                click.secho(f"Completions already installed in {rc_file}", fg="green")
                return

    # Ensure parent directory exists
    os.makedirs(os.path.dirname(rc_file), exist_ok=True)

    if shell_name == "fish":
        with open(rc_file, "w") as f:
            f.write(line + "\n")
    else:
        with open(rc_file, "a") as f:
            f.write(f"\n# HeyAmara CLI completions\n{line}\n")

    click.secho(f"Completions installed in {rc_file}", fg="green")
    if shell_name == "powershell":
        click.echo("Restart PowerShell to activate completions.")
    else:
        click.echo(f"Restart your shell or run: source {rc_file}")
