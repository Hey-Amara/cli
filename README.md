# HeyAmara CLI

Developer CLI for Hey Amara infrastructure — setup, environment management, cluster access, and service connectivity.

## Installation

### Prerequisites
- Python 3.9+
- pip or pipx

### One-line install

```bash
curl -fsSL https://raw.githubusercontent.com/heyamara/cli/main/install.sh | bash
```

### Or install manually from GitHub Release

```bash
# Using pipx (recommended — isolated install)
pipx install https://github.com/heyamara/cli/releases/latest/download/heyamara-cli.tar.gz

# Or using pip
pip install https://github.com/heyamara/cli/releases/latest/download/heyamara-cli.tar.gz
```

### Upgrade

```bash
# Re-run the install script (auto-fetches latest)
curl -fsSL https://raw.githubusercontent.com/heyamara/cli/main/install.sh | bash

# Or manually
pipx upgrade heyamara-cli
```

### Enable Shell Completions

```bash
heyamara completions
```

Supports zsh, bash, fish, and PowerShell. Auto-detects your shell.

---

## Quick Start

```bash
# 1. Install required tools (kubectl, k9s, helm, etc.)
heyamara setup

# 2. Set your AWS profile
heyamara config set aws_profile

# 3. Pull environment variables for a service
heyamara env pull ats-backend

# 4. Connect to the dev cluster
heyamara cluster dev
```

All commands support **interactive mode** — run without arguments to get dropdown selectors.

---

## Commands

### `heyamara setup`

Install or check all required developer tools (aws-cli, kubectl, k9s, helm, helmfile, sops, yq, jq).

```bash
heyamara setup          # Install missing tools (macOS via Homebrew)
heyamara setup --check  # Only check what's installed
```

### `heyamara login`

Login to AWS via SSO. This is also triggered automatically when any command detects an expired session.

```bash
heyamara login
heyamara login --profile myprofile
```

### `heyamara config`

Manage CLI settings stored in `~/.heyamara/config.json`.

```bash
heyamara config get                        # Show all settings
heyamara config get aws_profile            # Show one setting

heyamara config set                        # Interactive — pick setting + value
heyamara config set aws_profile            # Dropdown of available AWS profiles
heyamara config set aws_profile myprofile  # Direct set
heyamara config set aws_region us-east-1   # Change region
```

**Available settings:**

| Key | Default | Description |
|-----|---------|-------------|
| `aws_profile` | `dev` | AWS CLI profile to use |
| `aws_region` | `ap-southeast-2` | AWS region |

---

## Environment Management

### `heyamara env pull`

Download a service's `.env` file from AWS SSM Parameter Store.

```bash
heyamara env pull                              # Interactive — select service + env
heyamara env pull ats-backend                  # Interactive env selection
heyamara env pull ats-backend dev              # Direct
heyamara env pull ats-backend dev -o .env      # Custom output path
```

### `heyamara env pull-all`

Download `.env` files for all services at once.

```bash
heyamara env pull-all                     # Interactive — select environment
heyamara env pull-all dev                 # Pull all dev env files
heyamara env pull-all dev -d ./envs       # Custom output directory
```

### `heyamara env show`

View environment variables without saving to a file.

```bash
heyamara env show                         # Interactive
heyamara env show ats-backend             # Interactive env selection
heyamara env show ai-backend production   # Direct
```

**Available services:** `ats-backend`, `ats-frontend`, `ae-backend`, `ai-backend`, `memory-service`, `profile-service`, `distributed-queue-broker`, `meeting-bot`

---

## Cluster Access

### `heyamara cluster`

Configure kubectl and open k9s for a cluster.

```bash
heyamara cluster            # Interactive — select environment
heyamara cluster dev        # Configure kubectl + open k9s for dev
heyamara cluster production # Open k9s for production
heyamara cluster dev --no-k9s  # Only configure kubectl, don't open k9s
```

### `heyamara status`

Show pod statuses for an environment.

```bash
heyamara status         # Interactive
heyamara status dev     # List all pods in dev
heyamara status dev -w  # Wide output (node, IP)
```

### `heyamara logs`

Tail logs for a service.

```bash
heyamara logs                          # Interactive — select env + service
heyamara logs dev ats-backend          # Tail last 100 lines, follow
heyamara logs dev ats-backend -n 50    # Tail last 50 lines
heyamara logs dev ats-backend --no-follow  # Print and exit
```

### `heyamara shell`

Exec into a running pod.

```bash
heyamara shell                    # Interactive
heyamara shell dev ats-backend    # Open shell in ats-backend pod
```

---

## Service Connectivity

Connect to AWS infrastructure services via SSM tunnel through EKS worker nodes. **No bastion host needed.**

All endpoints are auto-discovered — just specify the environment.

### `heyamara connect db`

Port-forward to RDS (PostgreSQL).

```bash
heyamara connect db             # Interactive — select environment
heyamara connect db dev         # Forward localhost:5432 -> RDS
heyamara connect db dev -p 5433 # Use custom local port
```

Then connect with your preferred client:
```bash
psql -h localhost -p 5432 -U <user> <database>
```

### `heyamara connect redis`

Port-forward to Redis (ElastiCache).

```bash
heyamara connect redis          # Interactive
heyamara connect redis dev      # Forward localhost:6379 -> Redis
heyamara connect redis dev -p 6380
```

Then connect:
```bash
redis-cli -h localhost -p 6379
```

### `heyamara connect rabbitmq`

Port-forward to RabbitMQ Management UI.

```bash
heyamara connect rabbitmq       # Interactive
heyamara connect rabbitmq dev   # Forward localhost:15672 -> RabbitMQ
```

Then open in browser:
```
https://localhost:15672
```

---

## How Connectivity Works

```
Developer Machine          AWS VPC
┌──────────────┐          ┌────────────────────────────────┐
│              │   SSM    │  EKS Worker Node               │
│ localhost:── │ ════════>│  (SSM Agent) ──> RDS :5432     │
│  5432/6379/  │  tunnel  │               ──> Redis :6379  │
│  15672       │          │               ──> RabbitMQ:443 │
└──────────────┘          └────────────────────────────────┘
```

- Uses existing EKS worker nodes as tunnel targets
- No dedicated bastion host, no SSH keys, no extra cost
- Each developer gets an independent SSM session
- Multiple developers can connect simultaneously
- Sessions are encrypted end-to-end by AWS SSM

---

## Troubleshooting

**"AWS session expired"**
All commands auto-trigger SSO login when the session expires. If it still fails:
```bash
heyamara login
```

**"No EKS worker nodes found"**
Ensure kubectl is configured for the environment:
```bash
heyamara cluster dev --no-k9s
```

**"Port already in use"**
Another tunnel is using that port. Use a custom port:
```bash
heyamara connect db dev -p 5433
```

**Shell completions not working**
```bash
heyamara completions
# Restart your terminal
```
