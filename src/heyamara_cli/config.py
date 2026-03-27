import json
import os
from pathlib import Path

# ---- User config file -------------------------------------------------------

CONFIG_DIR = Path.home() / ".heyamara"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULTS = {
    "aws_profile": "dev",
    "aws_region": "ap-southeast-2",
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
    """Get a config value (user override > default)."""
    return load_user_config().get(key, DEFAULTS.get(key, ""))


# ---- Static config -----------------------------------------------------------

SSM_PREFIX = "/amara"

CLUSTERS = {
    "dev": "heyamara-dev-cluster",
    "staging": "heyamara-production-cluster",
    "production": "heyamara-production-cluster",
}

NAMESPACES = {
    "dev": "dev",
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
