"""Shell completion helpers for Click arguments."""

import click

from heyamara_cli.config import NAMESPACES, SERVICES


class ServiceType(click.ParamType):
    """Click type with shell completion for services."""
    name = "service"

    def shell_complete(self, ctx, param, incomplete):
        return [
            click.shell_completion.CompletionItem(s)
            for s in SERVICES
            if s.startswith(incomplete)
        ]


class EnvironmentType(click.ParamType):
    """Click type with shell completion for environments."""
    name = "environment"

    def shell_complete(self, ctx, param, incomplete):
        return [
            click.shell_completion.CompletionItem(e)
            for e in NAMESPACES
            if e.startswith(incomplete)
        ]


SERVICE = ServiceType()
ENVIRONMENT = EnvironmentType()
