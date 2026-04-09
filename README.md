# heyamara

Internal developer CLI for Hey Amara. Cluster access, log search, DB/Redis/RabbitMQ tunneling, and environment management.

```
heyamara search production ats-backend --since 1h --grep "error" --json
heyamara logs dev ai-backend --grep timeout --since 5m
heyamara connect db production --iam -u power_user
```

## Install

```bash
pip install git+https://github.com/Hey-Amara/cli.git
```

Or with pipx (isolated environment):

```bash
pipx install git+https://github.com/Hey-Amara/cli.git
```

After installing, run the first-time setup:

```bash
heyamara setup                        # Install required tools (kubectl, k9s, helm, etc.)
heyamara config set aws_profile       # Pick your AWS SSO profile
heyamara config set grafana_token     # Paste your Grafana service account token
heyamara login                        # Authenticate via AWS SSO
heyamara cluster dev                  # Connect kubectl to dev cluster
```

Update to latest:

```bash
heyamara update
```

Shell completions (zsh, bash, fish):

```bash
heyamara completions
```

## Commands

All commands support **interactive mode** — run without arguments to get dropdown selectors.

### Log Search (Loki)

Search historical logs across all environments via Loki. Supports time ranges, text/regex search, level filtering, and JSON pretty-printing.

```bash
heyamara search dev ats-backend --since 1h
heyamara search production ats-backend --grep "Request error" --level error
heyamara search staging distributed-queue-broker --since 30m --json
heyamara search dev ai-backend --between 2026-04-01 2026-04-02
heyamara search production ats-backend --follow          # Tail mode (polls Loki)
heyamara search dev ats-backend --since 1h --raw         # Show LogQL query
```

| Flag | Description |
|------|-------------|
| `--since 1h` | Relative time window (5m, 1h, 2d) |
| `--after` / `--before` | Absolute time range (ISO 8601) |
| `--between START END` | Time range shortcut |
| `--grep` / `-g` | Text or regex filter (server-side LogQL) |
| `--level` | Filter by level: error, warn, info, debug, trace |
| `--limit` / `-n` | Max entries (default: 100) |
| `--json` | Pretty-print JSON entries with syntax highlighting |
| `--follow` / `-f` | Tail mode — poll for new entries |
| `--raw` | Show the LogQL query being executed |

### Live Logs (kubectl)

Tail live pod logs with time filtering, text search, and JSON-aware grep.

```bash
heyamara logs dev ats-backend                              # Tail + follow
heyamara logs dev ats-backend --since 5m --no-follow       # Last 5 minutes
heyamara logs dev ats-backend --grep ERROR --no-follow     # Search for ERROR
heyamara logs dev ats-backend --grep "timeout" --json      # Full JSON blocks matching "timeout"
heyamara logs dev ats-backend --previous                   # Logs from crashed container
heyamara logs dev ats-backend -t --prefix                  # Timestamps + pod names
heyamara logs dev ats-backend -c api-gateway               # Specific container
```

| Flag | Description |
|------|-------------|
| `--since` | Relative time (5m, 1h, 2d) |
| `--after` / `--before` / `--between` | Absolute time range |
| `--grep` / `-g` | Regex filter (client-side) |
| `--grep-context N` | Lines of context around matches |
| `--json` | JSON-aware grep — matches full JSON blocks |
| `--timestamps` / `-t` | Show timestamps |
| `--container` / `-c` | Target specific container |
| `--previous` | Logs from previous (crashed) container |
| `--prefix` | Prefix lines with pod name |

### Cluster & Pods

```bash
heyamara status dev                    # Pod statuses
heyamara status production -w          # Wide output (node, IP)
heyamara events dev                    # Kubernetes events
heyamara events dev --warnings-only    # Only warnings
heyamara events dev -w                 # Watch mode
heyamara top dev                       # CPU/memory usage
heyamara top dev ats-backend --sort memory
heyamara shell dev ats-backend         # Exec into a pod
heyamara cluster dev                   # Configure kubectl + open k9s
heyamara restart dev ats-backend       # Rolling restart (with confirmation)
heyamara rollout dev ats-backend       # Rollout status
heyamara rollout dev ats-backend --history
```

### Service Connectivity

Connect to AWS infrastructure via SSM tunnel through EKS worker nodes. No bastion needed. Endpoints are auto-discovered.

```bash
# PostgreSQL (RDS)
heyamara connect db dev                           # Forward localhost:5432 -> RDS
heyamara connect db production --iam              # With IAM auth token
heyamara connect db production --iam -u power_user -p 5433
heyamara connect db dev --db-name other_db        # Override database name
heyamara connect db dev --dry-run                 # Show details without connecting

# Redis (ElastiCache)
heyamara connect redis dev                        # Forward localhost:6379 -> Redis

# RabbitMQ
heyamara connect rabbitmq dev                     # Forward localhost:15672 -> RabbitMQ UI
```

With `--iam`, the DATABASE_URL is automatically copied to your clipboard.

| Flag | Description |
|------|-------------|
| `-p` / `--local-port` | Custom local port |
| `--profile` | Override AWS profile |
| `--region` | Override AWS region |
| `--iam` | Generate IAM auth token (db only) |
| `-u` / `--db-user` | Database user for IAM auth (default: developer) |
| `--db-name` | Override database name |
| `--dry-run` | Show what would happen without connecting |
| `--no-copy` | Don't copy DATABASE_URL to clipboard |

### Environment Variables

```bash
heyamara env pull ats-backend dev              # Download .env from SSM
heyamara env pull ats-backend dev -o .env      # Custom output path
heyamara env pull-all dev                      # Download all service .env files
heyamara env show ats-backend production       # Print without saving
```

### Auth & Diagnostics

```bash
heyamara whoami                        # Show AWS identity, profile, region, role
heyamara switch amara-prod             # Quick profile switch
heyamara switch                        # Interactive profile picker
heyamara doctor                        # Check tools, AWS session, kubectl, Grafana/Loki
heyamara login                         # AWS SSO login
heyamara login --profile amara-prod
```

### Configuration

```bash
heyamara config get                    # Show all settings (token masked)
heyamara config set                    # Interactive setting picker
heyamara config set aws_profile        # Pick from available AWS profiles
heyamara config set grafana_token      # Masked input for Grafana token
```

Config file: `~/.heyamara/config.json`

| Key | Default | Description |
|-----|---------|-------------|
| `aws_profile` | `dev` | AWS SSO profile |
| `aws_region` | `ap-southeast-2` | AWS region |
| `grafana_url` | `https://grafana.heyamara.com` | Grafana base URL |
| `grafana_token` | (empty) | Grafana service account token (Viewer role) |

### Other

```bash
heyamara version                       # Show version
heyamara update                        # Update to latest release
heyamara update --check                # Just check for updates
heyamara docs                          # Full command reference (paged)
heyamara help logs                     # Help for a specific command
heyamara completions                   # Install shell completions
```

## Services

| Service | Dev | Staging | Production |
|---------|-----|---------|------------|
| ats-backend | yes | yes | yes |
| ats-frontend | yes | yes | yes |
| ae-backend | yes | -- | -- |
| ai-backend | yes | -- | yes |
| memory-service | yes | yes | yes |
| profile-service | yes | -- | -- |
| distributed-queue-broker | yes | yes | yes |
| meeting-bot | yes | -- | yes |

Multi-container services (ai-backend, meeting-bot) use the `--container` / `-c` flag or the interactive picker to select a specific component.

## Architecture

```
Developer Machine              AWS
+--------------+              +----------------------------------+
|              |    SSM       |  EKS Worker Node                 |
| localhost:   | ============>|  (SSM Agent) --> RDS      :5432  |
|  5432/6379/  |   tunnel     |              --> Redis    :6379  |
|  15672       |              |              --> RabbitMQ :443   |
+--------------+              +----------------------------------+

+--------------+              +----------------------------------+
|              |    HTTPS     |  Grafana (grafana.heyamara.com)  |
| heyamara     | ----------->|  --> Loki (log search)            |
|  search      |   API       |  --> Prometheus (metrics)         |
+--------------+              +----------------------------------+
```

- SSM tunnels use EKS worker nodes — no bastion, no SSH keys
- Log search queries Loki via the Grafana API proxy
- All environments (dev, staging, production) feed into Loki

## Environments

| Name | Cluster | Namespace |
|------|---------|-----------|
| dev | heyamara-dev-cluster | dev |
| staging | heyamara-production-cluster | staging |
| production | heyamara-production-cluster | production |

## Local Development

### Setup

```bash
git clone https://github.com/Hey-Amara/cli.git
cd cli

# Install in editable mode (changes take effect immediately)
pip install -e .

# Verify
heyamara version
```

### Project Structure

```
src/heyamara_cli/
  main.py              # CLI root, command registration, help categories
  config.py            # Static config (clusters, services) + user config
  helpers.py           # Subprocess runner, AWS session, port check, IAM detection
  loki.py              # Loki HTTP client, LogQL builder, JSON reassembly
  prompts.py           # InquirerPy wrappers (select, confirm)
  completions.py       # Shell completion types (ServiceType, EnvironmentType)
  commands/
    cluster.py         # heyamara cluster
    config_cmd.py      # heyamara config get/set
    connect.py         # heyamara connect db/redis/rabbitmq
    env.py             # heyamara env pull/pull-all/show
    k8s.py             # heyamara logs/shell/status/events/top/restart/rollout
    login.py           # heyamara login
    search.py          # heyamara search
    setup.py           # heyamara setup
    update.py          # heyamara update
    completions.py     # heyamara completions
```

### Dependencies

Runtime (only 2):
- `click>=8.0` — CLI framework
- `InquirerPy>=0.3.4` — Interactive prompts

Everything else uses stdlib (`urllib.request` for HTTP, `subprocess` for AWS CLI/kubectl, `socket` for port checks).

### Adding a New Command

1. Create `src/heyamara_cli/commands/mycommand.py`
2. Define a Click command following existing patterns (interactive pickers, `require_tool()`, etc.)
3. Import and register in `main.py`: `from ... import mycommand` + `cli.add_command(mycommand)`
4. Add to `COMMAND_CATEGORIES` in `main.py` for help grouping

### Adding a New Service

Add the service name to the `SERVICES` list in `config.py`. If it has sub-deployments, add a `SUB_SERVICES` entry.

### Adding a New Environment

Add entries to `CLUSTERS`, `NAMESPACES`, and the service deployment matrix in `config.py`.

### Testing Changes

```bash
# Editable install means changes are live immediately
heyamara --help
heyamara -v logs dev ats-backend --since 1m --no-follow   # -v for debug output
```

### Releasing

```bash
# 1. Bump version in pyproject.toml
# 2. Commit and push to main
# 3. Create a GitHub release tag:
gh release create v1.5.0 --title "v1.5.0" --generate-notes

# Existing users update with:
heyamara update
```

## Troubleshooting

**"AWS session expired"** — All commands auto-trigger SSO login. If it persists: `heyamara login`

**"No EKS worker nodes found"** — Configure kubectl: `heyamara cluster dev --no-k9s`

**"Port already in use"** — Use a custom port: `heyamara connect db dev -p 5433`

**"Grafana token not configured"** — Run: `heyamara config set grafana_token`

**"No log entries found"** — Check the time range. Default is 1 hour. Try `--since 6h` or `--since 1d`.

**Shell completions not working** — Run `heyamara completions` and restart your terminal.

**Full diagnostics** — Run `heyamara doctor` to check all tools, sessions, and connectivity.
