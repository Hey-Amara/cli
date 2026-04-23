# heyamara

Internal developer CLI for Hey Amara. Cluster access, log search, DB/Redis/RabbitMQ tunneling, and environment management.

```
heyamara search production ats-backend --since 1h --grep "error" --json
heyamara logs staging ai-backend --grep timeout --since 5m
heyamara db psql staging ats                    # interactive psql, no tunnel juggling
heyamara db run staging ats -- node scripts/seed.js
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
heyamara cluster staging              # Connect kubectl to staging cluster
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
heyamara search staging ats-backend --since 1h
heyamara search production ats-backend --grep "Request error" --level error
heyamara search staging distributed-queue-broker --since 30m --json
heyamara search staging ai-backend --between 2026-04-01 2026-04-02
heyamara search production ats-backend --follow          # Tail mode (polls Loki)
heyamara search staging ats-backend --since 1h --raw     # Show LogQL query
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
heyamara logs staging ats-backend                              # Tail + follow
heyamara logs staging ats-backend --since 5m --no-follow       # Last 5 minutes
heyamara logs staging ats-backend --grep ERROR --no-follow     # Search for ERROR
heyamara logs staging ats-backend --grep "timeout" --json      # Full JSON blocks matching "timeout"
heyamara logs staging ats-backend --previous                   # Logs from crashed container
heyamara logs staging ats-backend -t --prefix                  # Timestamps + pod names
heyamara logs staging ats-backend -c api-gateway               # Specific container
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
heyamara status staging                    # Pod statuses
heyamara status production -w              # Wide output (node, IP)
heyamara events staging                    # Kubernetes events
heyamara events staging --warnings-only    # Only warnings
heyamara events staging -w                 # Watch mode
heyamara top staging                       # CPU/memory usage
heyamara top staging ats-backend --sort memory
heyamara shell staging ats-backend         # Exec into a pod
heyamara cluster staging                   # Configure kubectl + open k9s
heyamara restart staging ats-backend       # Rolling restart (with confirmation)
heyamara rollout staging ats-backend       # Rollout status
heyamara rollout staging ats-backend --history
```

### Connecting to the Database

Staging RDS and production RDS live in private subnets. You can't connect directly from your laptop — traffic has to be tunnelled through an EKS worker node.

The CLI handles all of that. **You don't need to know how SSM tunnels or IAM tokens work** — pick the command that matches your task.

#### Which command do I use?

| I want to… | Use |
|---|---|
| Open an interactive psql session | `heyamara db psql` |
| Get a `DATABASE_URL` I can paste into a script | `heyamara db url` |
| Run a script that needs a database | `heyamara db run` |
| Connect a GUI tool (DBeaver, TablePlus, pgAdmin) | `heyamara connect db` (keeps tunnel open) |
| Figure out why a connection isn't working | `heyamara db doctor` |

#### The one-liners

```bash
# Open psql against the ats database. When you \q out, tunnel closes automatically.
heyamara db psql staging ats

# Run a Node script with DATABASE_URL set. Tunnel closes when script exits.
heyamara db run staging ats -- node scripts/seed-staging.js

# Print a DATABASE_URL for use in another tool or shell.
# (Tunnel stays alive until you close this terminal.)
export DATABASE_URL=$(heyamara db url staging ats)
psql $DATABASE_URL
```

#### What each command does, step by step

**`heyamara db psql staging ats`**

1. Finds a staging EKS node to tunnel through.
2. Opens the SSM tunnel in the background.
3. Confirms the tunnel can actually reach RDS (so you don't hit a silent hang later).
4. Generates a temporary IAM auth token (no password required).
5. Opens psql against the `ats_staging` database as the `ats_backend` user.
6. When you quit psql, it closes the tunnel for you.

**`heyamara db url staging ats`**

Same as `psql` steps 1–4, but instead of opening psql it prints the `DATABASE_URL` and stays running. Useful when you want to paste the URL into DBeaver, pgAdmin, or a shell variable. Press Ctrl+C to close the tunnel.

**`heyamara db run staging ats -- <command>`**

Same as `psql`, but instead of opening psql it runs **your command** with `DATABASE_URL` already set as an environment variable. When your command exits, the tunnel is cleaned up. This is the command you want for one-off scripts.

**`heyamara db doctor staging`**

Runs 8 checks end-to-end and tells you exactly which layer is broken:

```
[1/8] AWS session                  ✅ Authenticated
[2/8] RDS cluster discovery        ✅ Found heyamara-staging-cluster
[3/8] RDS IAM authentication       ✅ Enabled
[4/8] EKS worker node              ✅ Found i-05b7d292279cd40f4
[5/8] SSM tunnel opens             ✅ Tunnel ready
[6/8] TCP reachability             ✅ RDS port responds
[7/8] IAM auth token               ✅ Token generated
[8/8] Live login test              ❌ user does not exist
      → Create the user or use --as <existing-user>
```

Use this first if anything seems off.

#### Service → database mapping

When you pass a service name (like `ats`), the CLI picks the right database and login user:

| Service arg | Database | Login user |
|---|---|---|
| `ats` | `ats_staging` | `ats_backend` |
| `ae` | `ae_staging` | `ae_backend` |
| `ai` | `ai_staging` | `ai_backend` |
| `memory` | `memory_staging` | `memory_service` |
| `profile` | `profile_staging` | `profile_service` |

Override with `--as <user>` (e.g. `--as developer` for read-only access) or `--db-name <name>`.

#### The persistent tunnel (GUI tools)

If you want a tunnel that stays open all day so your DBeaver/TablePlus can use it, use the older command:

```bash
heyamara connect db staging --iam -u developer   # keeps tunnel open until Ctrl+C
```

This prints a ready-to-use `DATABASE_URL` and keeps the tunnel alive in the foreground. Open DBeaver pointed at `localhost:5432`, do your work, Ctrl+C when done.

#### Redis and RabbitMQ

```bash
heyamara connect redis staging            # forward localhost:6379 -> ElastiCache
heyamara connect rabbitmq staging         # forward localhost:15672 -> RabbitMQ UI
```

#### Flags reference

| Flag | Applies to | Description |
|---|---|---|
| `--as <user>` | `db psql/url/run/doctor` | Login as this DB user (default: service owner) |
| `--db-name <name>` | `db psql/url/run` | Override auto-detected database |
| `-p` / `--local-port` | All | Custom local port (default auto-picks a free one near 15432) |
| `--profile` | All | Override AWS profile |
| `--region` | All | Override AWS region |
| `--iam` | `connect db` | Generate IAM auth token (default on `db *` commands) |
| `-u` / `--db-user` | `connect db` | Database user for IAM auth |
| `--dry-run` | `connect *` | Print what would happen, don't connect |
| `--no-copy` | `connect db` | Don't copy URL to clipboard |

> All generated URLs include `connect_timeout=10` so clients fail in 10 seconds with a clear error instead of hanging forever.

### Environment Variables

```bash
heyamara env pull ats-backend staging          # Download .env from SSM
heyamara env pull ats-backend staging -o .env  # Custom output path
heyamara env pull-all staging                  # Download all service .env files
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
| `aws_profile` | `default` | AWS SSO profile |
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

| Service | Staging | Production |
|---------|---------|------------|
| ats-backend | yes | yes |
| ats-frontend | yes | yes |
| ae-backend | yes | yes |
| ai-backend | yes | yes |
| memory-service | yes | yes |
| profile-service | yes | yes |
| distributed-queue-broker | yes | yes |
| meeting-bot | yes | yes |

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
- All environments (staging, production) feed into Loki

## Environments

| Name | Cluster | Namespace |
|------|---------|-----------|
| staging | heyamara-staging-cluster | staging |
| production | heyamara-production-cluster | production |

> The `dev` environment has been retired. Use staging for integration testing and preview environments for per-PR testing.

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
  tunnel.py            # SSM tunnel + RDS IAM helpers (shared by connect/db commands)
  loki.py              # Loki HTTP client, LogQL builder, JSON reassembly
  prompts.py           # InquirerPy wrappers (select, confirm)
  completions.py       # Shell completion types (ServiceType, EnvironmentType)
  version_check.py     # Non-blocking update notification
  commands/
    cluster.py         # heyamara cluster
    config_cmd.py      # heyamara config get/set
    connect.py         # heyamara connect db/redis/rabbitmq (persistent tunnels)
    db.py              # heyamara db psql/url/run/doctor (one-shot wrappers)
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
heyamara -v logs staging ats-backend --since 1m --no-follow   # -v for debug output
```

### Releasing

```bash
# 1. Bump version in pyproject.toml
# 2. Commit and push to main
# 3. Create a GitHub release tag:
gh release create v1.6.0 --title "v1.6.0" --generate-notes

# Existing users update with:
heyamara update
```

## Troubleshooting

**"AWS session expired"** — All commands auto-trigger SSO login. If it persists: `heyamara login`

**"No EKS worker nodes found"** — Configure kubectl: `heyamara cluster staging --no-k9s`

**"Port already in use"** — The `db *` commands auto-pick a free port near 15432. For `connect db`, use `-p 5433` or another free port.

**"Grafana token not configured"** — Run: `heyamara config set grafana_token`

**"No log entries found"** — Check the time range. Default is 1 hour. Try `--since 6h` or `--since 1d`.

**Shell completions not working** — Run `heyamara completions` and restart your terminal.

**DB connection seems to hang** — The CLI bakes `connect_timeout=10` into all generated URLs, so this shouldn't happen. If it does, run `heyamara db doctor staging` — it walks every layer (AWS session, RDS discovery, IAM auth, EKS node, tunnel, TCP reachability, token, login) and tells you which one is broken.

**DB login fails with "user does not exist"** — Each service has its own DB user (`ats_backend`, `ae_backend`, etc.). Use `--as developer` for read-only, or ask an admin to create a user for you.

**Full diagnostics** — Run `heyamara doctor` for general tools/sessions, or `heyamara db doctor <env>` specifically for database issues.
