# openclaw_scripts

Scripts for the OpenClaw agent system. Primarily automates a daily morning brief delivered via Telegram, plus supporting utilities for environment setup and model management.

**Location context:** Sunnyvale, CA (ZIP 94086, NWS grid MTR/94,85, PurpleAir sensor 13987)

---

## Scripts

### Morning Brief

| Script | Purpose |
|---|---|
| `morning_brief_send.py` | **Primary.** Fetches all data and sends the brief via Telegram. Run this directly. |
| `morning_brief_fetch.py` | Outputs the brief as plain text to stdout — designed for an LLM to summarize before sending. |
| `morning_brief_fetch.sh` | Shell equivalent of `morning_brief_fetch.py`. Lighter weight; no Python dependencies beyond inlined snippets. |
| `brief_fetcher.py` | Writes brief data to `/data/workspace/brief_data.txt` for downstream agent consumption. |

### Utilities

| Script | Purpose |
|---|---|
| `update_free_model.sh` | Picks a random free-tier model from a hardcoded list, updates `/data/.openclaw/openclaw.json`, and restarts the gateway. |
| `brew_env.sh` | Sources the Linuxbrew environment. Source this at the top of scripts that need `brew`. |
| `hello_test.sh` | Smoke test — confirms the agent can execute a script. |

---

## Setup

### Required environment variables

```
ALPHA_VANTAGE_API_KEY      # Stock quotes (alphavantage.co)
BRAVE_SEARCH_API_KEY       # News headlines (brave.com/search/api)
PURPLE_AIR_API_KEY_READ    # Air quality (purpleair.com)
```

Telegram delivery is handled by the `openclaw` CLI — no separate token needed in the environment.

### Running the morning brief

```bash
python3 morning_brief_send.py
```

---

## Architecture

```
Data sources                 Scripts                    Output
─────────────────────────────────────────────────────────────────
Alpha Vantage (stocks)  ──┐
Brave Search (news)     ──┤
weather.gov (forecast)  ──┼─► morning_brief_send.py ──► Telegram
EPA Envirofacts (UV)    ──┤
PurpleAir (AQI)         ──┘

                           morning_brief_fetch.py  ──► stdout (LLM input)
                           brief_fetcher.py        ──► /data/workspace/brief_data.txt
```

---

## Coding Standards for Agents

These conventions keep scripts maintainable when multiple agents read and modify them.

### Python

**Use the standard library.** No third-party packages. All network requests use `urllib.request`. This keeps the environment dependency-free — no `pip install`, no virtualenv.

**One function per data source.** Each external API call lives in its own function (`fetch_weather`, `fetch_aqi`, etc.). Functions return clean data structures, not formatted strings. Formatting happens in `main()`.

**Function return types should be simple and consistent.** Prefer returning a plain value, tuple, or dict over raising exceptions for expected failures. Return `None` to signal unavailability.

```python
# Good — caller can check for None
def fetch_aqi():
    ...
    return pm, aqi, category  # or None on failure

# Avoid — forces caller to catch
def fetch_aqi():
    ...
    raise ValueError("AQI unavailable")
```

**Wrap every external call in try/except in `main()`.** Individual fetch functions may raise — that's fine. The caller in `main()` should catch so one failed source never blocks the rest.

```python
try:
    aqi_data = fetch_aqi()
    if aqi_data:
        pm, aqi, cat = aqi_data
        lines.append(f"💨 Air quality: AQI {aqi} ({cat})")
except:
    pass  # skip silently; brief sends without AQI
```

**Timezone handling.** Always compute Pacific time from UTC. Use the manual DST check already established in the codebase rather than importing `zoneinfo` or `pytz`:

```python
utc = datetime.now(timezone.utc)
dst = (utc.month > 3 or (utc.month == 3 and utc.day >= 8)) and utc.month < 11
now = utc.astimezone(timezone(timedelta(hours=-7 if dst else -8)))
```

**API keys come from environment variables only.** Never hardcode keys. Use `os.environ.get("KEY_NAME", "")` and check for empty string before making the call.

**Constants at the top.** API URLs, sensor IDs, and other config values are module-level constants, not buried in functions.

```python
EPA_UV_API_URL = "https://enviro.epa.gov/enviro/efservice/getEnvirofactsUVHOURLY/ZIP/94086/JSON"
PURPLE_SENSOR_ID = 13987
```

### Shell

**Use `#!/usr/bin/env bash` shebangs** (not `/bin/sh`) and `set -e` for scripts that modify system state (configs, services). Omit `set -e` for scripts that are expected to partially fail.

**Source `brew_env.sh` before using brew.** Don't assume Linuxbrew is on `PATH`.

**Inline Python for JSON parsing.** Use `python3 -c "..."` rather than `jq` or `awk` to parse API responses — Python is guaranteed to be present, `jq` is not.

```bash
curl -s "$URL" | python3 -c "import sys, json; print(json.load(sys.stdin)['key'])"
```

**Log system-level scripts.** Scripts that touch configs or restart services (like `update_free_model.sh`) should write to `/var/log/` with timestamps using a `log()` function.

### General

**No LLM dependencies in data-fetch scripts.** `morning_brief_send.py` and the shell scripts run standalone. LLM calls belong to the agent layer above, not here.

**Prefer explicit over clever.** Readable code that an agent can parse and modify safely is better than compact code. Avoid list comprehensions that span more than one logical operation.

**Paths use environment-aware constants.** Config and workspace paths (`/data/.openclaw/`, `/data/workspace/`) are defined as constants, not repeated inline.

**Adding a new data source:** create a `fetch_<source>()` function, add a call in `main()` wrapped in try/except, append a formatted line to `lines`. Don't restructure the rest of the script.
