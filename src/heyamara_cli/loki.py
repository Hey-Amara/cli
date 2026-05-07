"""Loki log client via Grafana proxy.

Provides LogQL query building, HTTP communication, JSON block reassembly,
and colored terminal output for the `heyamara search` command.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import click

from heyamara_cli.config import NAMESPACES
from heyamara_cli.helpers import debug


# ---- Constants ---------------------------------------------------------------

LOKI_DATASOURCE_UIDS = {
    "production": "loki",
    "staging": "loki-staging",
}
DEFAULT_LOKI_DATASOURCE_UID = "loki-staging"

FOLLOW_POLL_INTERVAL = 3  # seconds between polls
FOLLOW_LOOKBACK_NS = 10_000_000_000  # 10s overlap in nanoseconds

LEVEL_COLORS = {
    "error": "red",
    "err": "red",
    "fatal": "red",
    "warn": "yellow",
    "warning": "yellow",
    "info": "cyan",
    "debug": "bright_black",
    "trace": "bright_black",
}

_DURATION_RE = re.compile(r"^(\d+)([smhd])$")
_DURATION_MULTIPLIERS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_REGEX_META = re.compile(r"[\[\]().*+?{}\\|^$]")


# ---- Exceptions --------------------------------------------------------------


class LokiError(Exception):
    """Raised when Loki/Grafana returns an unexpected response."""

    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"Loki HTTP {status}: {body[:200]}")


# ---- HTTP client (stdlib only) -----------------------------------------------


def resolve_datasource_uid(environment: str, datasource_uid: str | None = None) -> str:
    """Resolve the Grafana Loki datasource UID for an environment."""
    if datasource_uid:
        return datasource_uid
    return LOKI_DATASOURCE_UIDS.get(environment, DEFAULT_LOKI_DATASOURCE_UID)


def loki_paths(datasource_uid: str) -> tuple[str, str]:
    """Return Grafana proxy paths for a Loki datasource UID."""
    uid = urllib.parse.quote(datasource_uid, safe="")
    base = f"/api/datasources/proxy/uid/{uid}/loki/api/v1"
    return f"{base}/query_range", f"{base}/labels"


def _http_get(url: str, token: str, params: dict | None = None) -> dict:
    """GET request to Grafana with Bearer auth. Returns parsed JSON."""
    if params:
        query_string = urllib.parse.urlencode(params)
        full_url = f"{url}?{query_string}"
    else:
        full_url = url

    debug(f"Loki GET: {full_url}")
    req = urllib.request.Request(
        full_url,
        headers={"Authorization": f"Bearer {token}"},
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise LokiError(e.code, body)
    except urllib.error.URLError as e:
        raise LokiError(0, str(e.reason))


def check_connectivity(
    grafana_url: str,
    token: str,
    datasource_uid: str = DEFAULT_LOKI_DATASOURCE_UID,
) -> bool:
    """Quick check that Grafana/Loki is reachable. Returns True on success."""
    _, labels_path = loki_paths(datasource_uid)
    url = grafana_url.rstrip("/") + labels_path
    try:
        data = _http_get(url, token)
        return data.get("status") == "success"
    except LokiError:
        return False


# ---- Time utilities ----------------------------------------------------------


def _now_ns() -> int:
    return int(time.time() * 1_000_000_000)


def _duration_to_seconds(duration: str) -> int:
    m = _DURATION_RE.match(duration)
    if not m:
        raise click.BadParameter(
            f"Invalid duration '{duration}'. Use: 5m, 1h, 2d, 30s",
            param_hint="'--since'",
        )
    return int(m.group(1)) * _DURATION_MULTIPLIERS[m.group(2)]


def _iso_to_ns(value: str) -> int:
    """Parse ISO 8601 timestamp to nanosecond epoch."""
    formats = [
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1_000_000_000)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1_000_000_000)
    except ValueError:
        raise click.BadParameter(
            f"Cannot parse '{value}'. Use ISO 8601: 2024-01-15T10:30:00Z or 2024-01-15",
            param_hint="timestamp",
        )


def _format_timestamp(ts_ns: int) -> str:
    """Nanosecond epoch -> human-readable local time string."""
    dt = datetime.fromtimestamp(ts_ns / 1e9)
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{int(ts_ns % 1_000_000_000) // 1_000_000:03d}"


def parse_time_range(
    since: str | None = None,
    after: str | None = None,
    before: str | None = None,
    between: tuple[str, str] | None = None,
) -> tuple[int, int]:
    """Resolve time options into (start_ns, end_ns)."""
    now_ns = _now_ns()

    if between:
        return _iso_to_ns(between[0]), _iso_to_ns(between[1])
    if after and before:
        return _iso_to_ns(after), _iso_to_ns(before)
    if after:
        return _iso_to_ns(after), now_ns
    if before:
        return now_ns - 3600_000_000_000, _iso_to_ns(before)
    if since:
        secs = _duration_to_seconds(since)
        return now_ns - (secs * 1_000_000_000), now_ns
    # Default: last 1 hour
    return now_ns - 3600_000_000_000, now_ns


# ---- LogQL builder -----------------------------------------------------------


def build_logql(
    environment: str,
    service: str,
    grep: str | None = None,
    level: str | None = None,
) -> str:
    """Build a LogQL query from filter parameters."""
    namespace = NAMESPACES.get(environment, environment)
    selector = f'app="{service}", namespace="{namespace}"'
    if level:
        selector += f', level="{level.lower()}"'
    query = "{" + selector + "}"
    if grep:
        if _REGEX_META.search(grep):
            query += f" |~ `{grep}`"
        else:
            query += f' |= "{grep}"'
    return query


# ---- Query and parse ---------------------------------------------------------


def query_loki(
    grafana_url: str,
    token: str,
    logql: str,
    start_ns: int,
    end_ns: int,
    limit: int = 100,
    datasource_uid: str = DEFAULT_LOKI_DATASOURCE_UID,
) -> list[dict]:
    """Query Loki and return a flat list of parsed entries sorted by time."""
    query_path, _ = loki_paths(datasource_uid)
    url = grafana_url.rstrip("/") + query_path
    params = {
        "query": logql,
        "start": str(start_ns),
        "end": str(end_ns),
        "limit": str(limit),
        "direction": "forward",
    }

    data = _http_get(url, token, params)

    if data.get("status") != "success":
        raise LokiError(0, f"Unexpected status: {data.get('status')}")

    entries = []
    for stream in data.get("data", {}).get("result", []):
        labels = stream.get("stream", {})
        for ts_str, line in stream.get("values", []):
            ts_ns = int(ts_str)
            entries.append({
                "ts_ns": ts_ns,
                "ts_str": _format_timestamp(ts_ns),
                "labels": labels,
                "raw_line": line,
            })

    entries.sort(key=lambda e: e["ts_ns"])

    # Filter out empty/noise lines (bare level prefixes, blank lines)
    _NOISE_RE = re.compile(r"^\s*(TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|ERR)?\s*$", re.IGNORECASE)
    entries = [e for e in entries if not _NOISE_RE.match(e["raw_line"])]

    return entries


# ---- JSON block reassembly ---------------------------------------------------


_STREAM_ID_KEYS = ("app", "namespace", "pod", "container")


def _stream_id(labels: dict) -> tuple:
    """Extract the stable identifying labels for stream grouping.

    Loki auto-detects level/detected_level per line, so those vary within
    the same JSON block. We only compare the stable identifiers.
    """
    return tuple(labels.get(k, "") for k in _STREAM_ID_KEYS)


def reassemble_json_blocks(entries: list[dict]) -> list[dict]:
    """Merge adjacent multi-line JSON entries into single logical entries."""
    if not entries:
        return entries

    result = []
    buf = []
    brace_depth = 0

    for entry in entries:
        line = entry["raw_line"].strip()

        if not buf:
            if line.startswith("{"):
                buf.append(entry)
                brace_depth = line.count("{") - line.count("}")
                if brace_depth <= 0:
                    result.append(_merge_block(buf))
                    buf = []
                    brace_depth = 0
            else:
                result.append(entry)
        else:
            prev_ts = buf[-1]["ts_ns"]
            same_stream = _stream_id(entry["labels"]) == _stream_id(buf[0]["labels"])
            close_in_time = (entry["ts_ns"] - prev_ts) < 500_000_000

            if same_stream and close_in_time:
                buf.append(entry)
                brace_depth += line.count("{") - line.count("}")
                if brace_depth <= 0:
                    result.append(_merge_block(buf))
                    buf = []
                    brace_depth = 0
            else:
                for b in buf:
                    result.append(b)
                buf = []
                brace_depth = 0
                if line.startswith("{"):
                    buf.append(entry)
                    brace_depth = line.count("{") - line.count("}")
                    if brace_depth <= 0:
                        result.append(_merge_block(buf))
                        buf = []
                        brace_depth = 0
                else:
                    result.append(entry)

    for b in buf:
        result.append(b)

    return result


def _merge_block(buf: list[dict]) -> dict:
    """Merge buffered entries into one logical entry."""
    joined = "\n".join(e["raw_line"] for e in buf)
    merged = dict(buf[0])
    merged["raw_line"] = joined
    merged["is_json_block"] = True
    try:
        merged["parsed_json"] = json.loads(joined)
    except json.JSONDecodeError:
        merged["parsed_json"] = None
    return merged


# ---- Output formatting -------------------------------------------------------


def _colorize_level(level_str: str) -> str:
    """Return click.style-colored level string."""
    color = LEVEL_COLORS.get(level_str.lower(), "white")
    return click.style(f"{level_str.upper():5s}", fg=color, bold=level_str.lower() in ("error", "err", "fatal"))


def _highlight_json(obj: dict) -> str:
    """Pretty-print a dict with colored keys and values."""
    lines = ["{"]
    items = list(obj.items())
    for i, (k, v) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        key_str = click.style(f'  "{k}"', fg="cyan")
        if isinstance(v, str):
            val_str = click.style(f'"{v}"', fg="green")
        elif isinstance(v, bool):
            val_str = click.style(str(v).lower(), fg="magenta")
        elif isinstance(v, (int, float)):
            val_str = click.style(str(v), fg="yellow")
        elif v is None:
            val_str = click.style("null", fg="bright_black")
        else:
            val_str = click.style(json.dumps(v, default=str), fg="white")
        lines.append(f"{key_str}: {val_str}{comma}")
    lines.append("}")
    return "\n".join(lines)


def format_entry(entry: dict, use_json: bool = False) -> str:
    """Format a single log entry for terminal output."""
    ts = click.style(entry["ts_str"], dim=True)

    # Extract level from labels or parsed JSON
    level = entry["labels"].get("level", "")
    parsed = entry.get("parsed_json")
    if not level and parsed and isinstance(parsed, dict):
        level = parsed.get("level", "")
    level_str = _colorize_level(level) if level else click.style("     ", dim=True)

    app = click.style(entry["labels"].get("app", ""), dim=True)

    if use_json and parsed and isinstance(parsed, dict):
        body = _highlight_json(parsed)
        return f"{ts}  {level_str}  {app}\n{body}"

    # For non-JSON mode: extract message from parsed JSON, or use raw line
    if parsed and isinstance(parsed, dict) and "message" in parsed:
        msg = parsed["message"]
    else:
        msg = entry["raw_line"]

    return f"{ts}  {level_str}  {app}  {msg}"


# ---- Follow mode (polling) ---------------------------------------------------


def stream_loki(
    grafana_url: str,
    token: str,
    logql: str,
    start_ns: int,
    limit: int,
    use_json: bool = False,
    raw: bool = False,
    datasource_uid: str = DEFAULT_LOKI_DATASOURCE_UID,
) -> None:
    """Tail mode: poll Loki every few seconds for new entries."""
    end_ns = _now_ns()
    entries = query_loki(
        grafana_url,
        token,
        logql,
        start_ns,
        end_ns,
        limit,
        datasource_uid,
    )
    entries = reassemble_json_blocks(entries)

    last_ts_ns = start_ns
    for entry in entries:
        click.echo(format_entry(entry, use_json=use_json))
        last_ts_ns = max(last_ts_ns, entry["ts_ns"])

    try:
        while True:
            time.sleep(FOLLOW_POLL_INTERVAL)
            new_start = last_ts_ns - FOLLOW_LOOKBACK_NS
            new_end = _now_ns()
            new_entries = query_loki(
                grafana_url,
                token,
                logql,
                new_start,
                new_end,
                limit,
                datasource_uid,
            )
            new_entries = reassemble_json_blocks(new_entries)

            for entry in new_entries:
                if entry["ts_ns"] > last_ts_ns:
                    click.echo(format_entry(entry, use_json=use_json))
                    last_ts_ns = entry["ts_ns"]
    except KeyboardInterrupt:
        pass
