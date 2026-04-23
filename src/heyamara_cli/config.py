import json
import os
from pathlib import Path

# ---- User config file -------------------------------------------------------

CONFIG_DIR = Path.home() / ".heyamara"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULTS = {
    "aws_profile": "default",
    "aws_region": "ap-southeast-2",
    "grafana_url": "https://grafana.heyamara.com",
    "grafana_token": "",
}


def load_user_config() -> dict:
    """Load user config from ~/.heyamara/config.json, merged with defaults."""
    config = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                config.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    return config


def save_user_config(config: dict) -> None:
    """Save user config to ~/.heyamara/config.json."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def get(key: str) -> str:
    """Get a config value.

    Precedence (highest → lowest):
      1. Matching env var (AWS_PROFILE overrides aws_profile, AWS_REGION overrides aws_region)
      2. User config file (~/.heyamara/config.json)
      3. Built-in default

    This lets `AWS_PROFILE=poweruser heyamara db psql staging` work as expected
    even when the user has a different default saved in the config file.
    """
    env_var_map = {
        "aws_profile": "AWS_PROFILE",
        "aws_region": "AWS_REGION",
    }
    env_var = env_var_map.get(key)
    if env_var:
        from os import environ
        env_val = environ.get(env_var)
        if env_val:
            return env_val
    return load_user_config().get(key, DEFAULTS.get(key, ""))


# ---- Static config -----------------------------------------------------------

SSM_PREFIX = "/amara"

CLUSTERS = {
    "staging": "heyamara-staging-cluster",
    "production": "heyamara-production-cluster",
}

NAMESPACES = {
    "staging": "staging",
    "production": "production",
}

SERVICES = [
    "ats-backend",
    "ats-frontend",
    "ae-backend",
    "ai-backend",
    "memory-service",
    "profile-service",
    "distributed-queue-broker",
    "meeting-bot",
]

# Services with multiple deployments — shown as sub-picker in logs/shell
SUB_SERVICES = {
    "ai-backend": [
        "ai-api-gateway",
        "ai-orchestrator",
        "ai-conversation-service",
        "ai-company-research",
        "ai-voice-agent",
    ],
    "meeting-bot": [
        "meeting-bot-api",
        "meeting-bot-worker",
    ],
}

REQUIRED_TOOLS = ["aws", "kubectl"]
OPTIONAL_TOOLS = ["k9s", "helm", "helmfile", "sops"]
