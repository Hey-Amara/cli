# heyamara

Internal developer CLI for Hey Amara. Cluster access, log search, DB/Redis/RabbitMQ tunneling, and environment management.

```
heyamara search production ats-backend --since 1h --grep "error" --json
heyamara logs staging ai-backend --grep timeout --since 5m
heyamara connect db staging --env-for ats-backend  # run a service locally against staging
heyamara db psql staging ats                       # interactive psql, no tunnel juggling
heyamara db run staging ats -- node scripts/seed.js
heyamara connect db production --iam -u power_user # GUI tool, 15-min IAM token
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

### Connecting to AWS Infra (DB, Redis, RabbitMQ)

Staging and production RDS / ElastiCache / AmazonMQ live in private subnets. You can't connect directly from your laptop — traffic has to be tunnelled through an EKS worker node.

The CLI handles all of that. **You don't need to know how SSM tunnels or IAM tokens work** — pick the command that matches your task.

#### Which command do I use?

| I want to… | Use | Auth | Expires? |
|---|---|---|---|
| Run a service locally (ats-backend, ae-backend, etc.) against staging | `heyamara connect db staging --env-for <svc>` | Service password (same as prod pods) | No |
| Open an interactive psql session | `heyamara db psql` | IAM (your SSO identity) | 15 min |
| Connect a GUI tool (DBeaver, TablePlus, pgAdmin) | `heyamara connect db staging --iam` | IAM | 15 min |
| Run a one-off script that needs a DB | `heyamara db run` | IAM | Script lifetime |
| Figure out why a connection isn't working | `heyamara db doctor` | — | — |

---

#### Running a service locally against staging (the common case)

You're working on `ats-backend` and you want to run it locally pointed at the staging database, redis, and rabbitmq. One command per service:

```bash
# Terminal 1 — DB tunnel + writes .env.local
heyamara connect db staging --env-for ats-backend

# Terminal 2 — Redis tunnel (append to same .env.local)
heyamara connect redis staging --env-for ats-backend -p 6380

# Terminal 3 — RabbitMQ (AMQPS) tunnel
heyamara connect rabbitmq staging --env-for ats-backend

# Terminal 4 — run the service
cd ats-backend
npm run dev   # reads .env.local automatically
```

What `--env-for` does:

1. Pulls the service's real env vars from SSM (the same ones prod pods use)
2. Rewrites `DATABASE_URL` / `REDIS_URL` / `AMQP_URL` to `localhost:<port>`
3. Writes everything to `.env.local`
4. Opens the SSM tunnel and keeps it alive until you Ctrl+C

Because it uses the **service-account password** (not an IAM token), the connection **stays valid all day**. Connection pools can churn freely. No 15-minute refreshes.

**Customizing the output path:** `-o .env.staging` or any path you want. Defaults to `.env.local` in your cwd.

**RabbitMQ TLS gotcha:** remote AmazonMQ uses TLS, and the broker's certificate CN won't match `localhost`. Most clients need TLS hostname verification disabled for local dev. Alternatively, alias the broker hostname to `127.0.0.1` in `/etc/hosts` so the cert validates normally.

---

#### Interactive psql (human querying)

```bash
# Opens psql against the ats database. When you \q out, tunnel closes automatically.
heyamara db psql staging ats

# Against production, as the read-only developer role
heyamara db psql production ats --as developer
```

**Steps under the hood:**

1. Finds a staging EKS node to tunnel through
2. Opens the SSM tunnel in the background
3. Confirms the tunnel can reach RDS (so you don't hit a silent hang later)
4. Generates a temporary IAM auth token (no password required)
5. Opens psql against the `ats_staging` database as your SSO identity
6. When you quit psql, it closes the tunnel for you

Session stays alive as long as psql stays open (days, if you want). Only opening a *new* connection after the 15-min token TTL requires re-running the command.

---

#### Persistent tunnel for GUI tools (DBeaver, TablePlus, pgAdmin)

```bash
heyamara connect db staging --iam -u developer
```

Opens a tunnel, prints a ready-to-use `DATABASE_URL`, keeps the tunnel alive in the foreground. Point your GUI at `localhost:5432` with the printed URL. Ctrl+C when done.

> The token expires in 15 min. For a long-running GUI session, connect once (token is consumed at connect time) and leave the connection open. If you disconnect after 15 min and try to reconnect, re-run the command.

---

#### One-off scripts

```bash
heyamara db run staging ats -- node scripts/seed-staging.js
```

Sets `DATABASE_URL` in the child environment, runs your command, cleans up the tunnel on exit. Good for migrations, ad-hoc dumps, data backfills.

---

#### Doctor — "why isn't it working?"

```bash
heyamara db doctor staging
```

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

---

#### Service → database mapping

When you pass a short service name (like `ats`) to `db psql/run`, the CLI picks the right database and login user:

| Service arg | Database | Login user |
|---|---|---|
| `ats` | `ats_staging` | `ats_backend` |
| `ae` | `ae_staging` | `ae_backend` |
| `ai` | `ai_staging` | `ai_backend` |
| `memory` | `memory_staging` | `memory_service` |
| `profile` | `profile_staging` | `profile_service` |

Override with `--as <user>` (e.g. `--as developer` for read-only access) or `--db-name <name>`.

For `--env-for`, use the **full service name** as it appears in SSM (`ats-backend`, `ai-backend`, etc.).

---

#### Management UIs

```bash
heyamara connect rabbitmq staging    # opens tunnel to AmazonMQ Management UI on localhost:15672
```

Then open `https://localhost:15672` in your browser. (For AMQPS app connections, use `--env-for` instead — it tunnels port 5671 and writes the env file.)

---

#### Flags reference

| Flag | Applies to | Description |
|---|---|---|
| `--env-for <service>` | `connect db/redis/rabbitmq` | Write `.env.local` with that service's real creds, host rewritten to localhost |
| `-o` / `--output <path>` | `connect db/redis/rabbitmq` | Output path for `--env-for` (default: `.env.local`) |
| `--iam` | `connect db` | Generate IAM auth token for interactive/GUI use (15-min TTL) |
| `-u` / `--db-user` | `connect db` | Database user for IAM auth (default: `developer`) |
| `--as <user>` | `db psql/run/doctor` | Login as this DB user (default: service owner) |
| `--db-name <name>` | `db psql/run`, `connect db` | Override auto-detected database |
| `-p` / `--local-port` | All | Custom local port |
| `--profile` | All | Override AWS profile |
| `--region` | All | Override AWS region |
| `--dry-run` | `connect *` | Print what would happen, don't connect |
| `--no-copy` | `connect db --iam` | Don't copy URL to clipboard |

> `--iam` and `--env-for` are mutually exclusive — they're for different audiences. Humans querying → `--iam`. Apps running → `--env-for`.

> All generated URLs include `connect_timeout=10` so clients fail fast with a clear error instead of hanging forever.

#### Deprecated commands

| Command | Status | Use instead |
|---|---|---|
| `heyamara db url` | Soft-deprecated in 1.7.0 | `heyamara connect db <env> --env-for <service>` — gives you a **non-expiring** URL via service-account password instead of a 15-min IAM token. For IAM URLs (scripting), `heyamara db run` is still supported. |

`db url` still works but emits a deprecation notice. It will be removed in 2.0.

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
