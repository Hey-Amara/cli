from InquirerPy import inquirer


def select(message: str, choices: list[str]) -> str:
    """Show an interactive dropdown selector."""
    return inquirer.select(
        message=message,
        choices=choices,
        pointer=">"
    ).execute()


def confirm(message: str, default: bool = True) -> bool:
    """Show a yes/no confirmation prompt."""
    return inquirer.confirm(message=message, default=default).execute()
