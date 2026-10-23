#!/usr/bin/env python3
"""
tech_digest_summarize.py — summarize + send.

Reads /data/.hermes/scripts/tech_digest_data_<DATE>.json (written by
tech_digest.py), calls the configured LLM to write a tight brief following
the morning_brief_send.py style, and sends the result to Telegram via the
Bot API.

Outputs:
  - /data/.hermes/scripts/tech_digest_digest_<DATE>.txt  (saved copy)
  - Telegram message

Exit codes:
  0 = success
  1 = fetch file missing
  2 = LLM call failed
  3 = Telegram send failed
"""

import json
import os
import re
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OUTPUT_DIR = Path("/data/.hermes/scripts")
TELEGRAM_TARGET = "8438066154"
TELEGRAM_BOT_TOKEN = ""
OPENCODE_API_KEY = ""
MODEL = "minimax-m3"

# OpenCode Zen's OpenAI-compatible endpoint (from your docs lookup):
#   https://opencode.ai/zen/v1/chat/completions
LLM_ENDPOINT = "https://opencode.ai/zen/v1/chat/completions"


def _load_secret(env_path, key):
    """Read a single KEY=value line from a file. Returns '' if not found."""
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
                if line.startswith("#"):
                    continue
    except Exception:
        pass
    return ""


def _load_secrets():
    global TELEGRAM_BOT_TOKEN, OPENCODE_API_KEY
    if TELEGRAM_BOT_TOKEN and OPENCODE_API_KEY:
        return
    env_path = os.environ.get("HERMES_ENV_FILE", "/data/.hermes/.env")
    if not TELEGRAM_BOT_TOKEN:
        TELEGRAM_BOT_TOKEN = _load_secret(env_path, "TELEGRAM_BOT_TOKEN")
    if not OPENCODE_API_KEY:
        OPENCODE_API_KEY = _load_secret(env_path, "OPENCODE_GO_API_KEY")


_load_secrets()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_data_file():
    """Find the most recent tech_digest_data_*.json file."""
    candidates = sorted(OUTPUT_DIR.glob("tech_digest_data_*.json"), reverse=True)
    return candidates[0] if candidates else None


def _find_trending_file():
    """Find the most recent trending_data_*.json file (may be None)."""
    candidates = sorted(OUTPUT_DIR.glob("trending_data_*.json"), reverse=True)
    return candidates[0] if candidates else None


def _out_path_for(data_path):
    """Return the corresponding .txt path for a given data file."""
    return data_path.with_name(data_path.name.replace("tech_digest_data_", "tech_digest_digest_")).with_suffix(".txt")


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a tech-news editor writing a personal morning brief. Your job is to \
take raw news items and produce a tight, scannable digest in the voice of \
a friend sending their morning notes.

Format rules:
- Section 1: "📈 Trending" — the top 2-3 items from the trending candidates \
(pre-scored by cross-source mention velocity). For each: 1 short sentence \
summary (the "why this matters"), then a stats line citing only the \
non-zero/non-unavailable sources, e.g. \
"   HN: 1 thread, 555 pts, 524 comments · GitHub: 5 repos, 6.5k★ · HF: 132 upvotes". \
If an item has days_seen > 1, append "(trending N days straight)" to the title line. \
If rising is true, prefix the title with 🔥. Then a 🔗 link line.
- Section 2: "🐙 GitHub" — top 2-3 repos from github_trending: repo name, \
1 short sentence (what it does / why it's gaining stars fast), star count \
formatted like "2.3k★ this week", and a 🔗 link line. If the list is empty, \
omit this section entirely.
- Section 3: "💬 Community" (Hacker News top stories)
- Section 4: "🔬 Research" (academic papers + top RSS pick)
- Community/Research items: 1 short sentence summary + a 🔗 link line
- The top RSS pick from a curated source list goes at the top of the Research section as a 5th item
- Footer: "Emerging themes:" — assign 1-3 topic tags to every item in the \
digest, count tag frequency weighted by engagement, and list the top 2-4 \
as "• theme (N items)". Themes should be specific ("agent benchmarks", \
"small-model distillation"), not generic ("AI", "tech").
- Skip items with no useful content
- If a section has no usable items, write "—" for that section
- If there is no trending data, omit the Trending section entirely
- Total length: aim for ~300 words — short, fast to scan
- Voice: casual, peer-to-peer, not clickbait
- No emoji in body text; only the section headers and the 🔥/📈 markers above
- Always include the 🔗 URL on its own line

If the data has only 3 HN stories and 1 RSS item and 0 papers, that's a valid \
digest — produce 4 items total and call it a day.
"""


USER_PROMPT_TEMPLATE = """\
Date: {date}
Source data: {data}
Trending candidates (pre-scored; may be empty): {trending}

Write the morning brief.
"""


def _trim_trending(trending):
    """Compact view of trending data for the prompt — the full JSON with all
    16 candidates' raw signal dicts bloats the reasoning model's token use."""
    if not trending:
        return {}
    keep = []
    for c in (trending.get("candidates") or [])[:8]:
        sig = c.get("signals") or {}
        hn, gh = sig.get("hn") or {}, sig.get("github") or {}
        rd, bs = sig.get("reddit") or {}, sig.get("bsky") or {}
        keep.append({
            "title": c.get("title"),
            "kind": c.get("kind"),
            "url": c.get("url"),
            "hn_link": c.get("hn_link") or None,
            "score": c.get("score"),
            "days_seen": c.get("days_seen", 1),
            "rising": c.get("rising", False),
            "hn": {"threads": hn.get("threads", 0), "points": hn.get("points", 0),
                   "comments": hn.get("comments", 0)},
            "github": {"repos": gh.get("repos", 0), "stars": gh.get("stars", 0)},
            "reddit": None if rd.get("unavailable") else
                      {"posts": rd.get("posts", 0), "score": rd.get("score", 0)},
            "bsky": None if bs.get("unavailable") else
                    {"posts": bs.get("posts", 0), "likes": bs.get("likes", 0)},
            "hf_upvotes": c.get("hf_upvotes", 0),
        })
    return {"date": trending.get("date"), "candidates": keep,
            "github_trending": trending.get("github_trending") or []}


def call_llm(data, trending=None):
    """Call OpenCode Zen with the configured model and return the assistant text."""
    raw_json = json.dumps(data, ensure_ascii=False, indent=2)
    trending_json = json.dumps(_trim_trending(trending), ensure_ascii=False, indent=2)
    prompt = USER_PROMPT_TEMPLATE.format(
        date=data.get("date", ""), data=raw_json, trending=trending_json)

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
        # minimax-m3 reasons first; needs headroom beyond the digest text
        "max_tokens": 16000,
        "temperature": 0.5,
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        LLM_ENDPOINT,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENCODE_API_KEY}",
            "User-Agent": "Hermes/1.0 tech-digest",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read())
    msg = resp["choices"][0]["message"]
    content = msg.get("content") or ""
    if not content:
        raise RuntimeError(
            f"LLM returned no content (finish_reason="
            f"{resp['choices'][0].get('finish_reason')}); "
            f"reasoning tokens may have exhausted max_tokens")
    return content


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN:
        print("WARN: TELEGRAM_BOT_TOKEN not loaded; printing digest to stdout instead.")
        print("---")
        print(message)
        print("---")
        return 0
    payload = json.dumps({
        "chat_id": TELEGRAM_TARGET,
        "text": message,
        "disable_web_page_preview": True,
    })
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data=payload.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read())
        if not body.get("ok"):
            print(f"Send failed: {body}", file=sys.stderr)
            return 1
        return 0
    except Exception as e:
        print(f"Send error: {e}", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Discover data file
    data_path = _find_data_file()
    if not data_path:
        print("No data file found. Run tech_digest.py first.", file=sys.stderr)
        sys.exit(1)
    with open(data_path) as f:
        data = json.load(f)
    print(f"Using {data_path}")

    # Load trending data (optional — digest works without it)
    trending = None
    trending_path = _find_trending_file()
    if trending_path:
        try:
            with open(trending_path) as f:
                trending = json.load(f)
            print(f"Using trending data from {trending_path}")
        except Exception as e:
            print(f"WARN: could not read {trending_path}: {e}")

    # Call LLM
    try:
        digest = call_llm(data, trending)
        digest = digest.strip()
    except Exception as e:
        print(f"LLM call failed: {e}", file=sys.stderr)
        sys.exit(2)
    if not digest:
        print("LLM returned empty digest.", file=sys.stderr)
        sys.exit(2)

    # Save the digest next to the data file
    out_path = _out_path_for(data_path)
    with open(out_path, "w") as f:
        f.write(digest + "\n")
    print(f"Saved digest to {out_path}")

    # Send
    code = send_telegram(digest)
    if code == 0:
        print("✅ Digest sent.")
    else:
        print(f"⚠ Send failed (code {code}).", file=sys.stderr)
    sys.exit(code)


if __name__ == "__main__":
    main()
