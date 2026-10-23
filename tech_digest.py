#!/usr/bin/env python3
"""
tech_digest.py — fetcher stage.

Fetches curated RSS, Hacker News, and ArXiv items and writes the raw
data to a JSON file. The summarization stage (tech_digest_summarize.py)
reads this file, asks the LLM to write a tight brief, and sends to Telegram.

For fresh news, the Hermes-side web_search tool is used during summarization
or interactive use; we don't depend on a Brave News API key here.

Outputs:
  /data/.hermes/scripts/tech_digest_data_<YYYY-MM-DD>.json

Exit codes:
  0 = success (file written)
  1 = partial success (some sections failed but file still written)
  2 = fatal (couldn't write output file)
"""

import json
import os
import re
import sys
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Hermes has built-in web_search; we don't need a Brave News API key here.
BRAVE_KEY = ""
OUTPUT_DIR      = Path("/data/.hermes/scripts")

# --- RSS feeds (filtered by keyword) ---
RSS_FEEDS = [
    ("MIT Technology Review", "https://www.technologyreview.com/feed/"),
    ("Wired",                 "https://www.wired.com/feed/rss"),
    ("IEEE Spectrum",         "https://spectrum.ieee.org/feeds/feed.rss"),
    ("Ars Technica",          "https://feeds.arstechnica.com/arstechnica/technology-lab"),
]

RSS_KEYWORDS = [
    "ai", "artificial intelligence", "machine learning", "llm", "language model",
    "agent", "autonomous", "self-driving", "robot", "neural", "quantum",
    "semiconductor", "biotech", "drone",
]


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

def strip_html(text):
    return re.sub(r"<[^>]+>", "", text or "").strip()


def truncate(text, n=140):
    text = text.strip()
    return text[:n] + "…" if len(text) > n else text


def fetch_rss_items(url, source_name):
    req = urllib.request.Request(url, headers={"User-Agent": "Hermes/1.0 tech-digest"})
    with urllib.request.urlopen(req, timeout=10) as r:
        root = ET.fromstring(r.read())
    items = []
    channel = root.find("channel")
    entries = channel.findall("item") if channel is not None else root.findall(
        "{http://www.w3.org/2005/Atom}entry"
    )
    for entry in entries[:20]:
        def get(tag, atom_tag=None):
            el = entry.find(tag)
            if el is None and atom_tag:
                el = entry.find(atom_tag)
            return (el.text or "").strip() if el is not None else ""

        title = get("title", "{http://www.w3.org/2005/Atom}title")
        link  = get("link",  "{http://www.w3.org/2005/Atom}link")
        desc  = strip_html(get("description", "{http://www.w3.org/2005/Atom}summary"))

        if not link:
            el = entry.find("{http://www.w3.org/2005/Atom}link")
            if el is not None:
                link = el.get("href", "")

        items.append({"title": title, "summary": truncate(desc), "url": link, "source": source_name})
    return items


# Word-boundary matching: plain substring lets "ai" hit "said"/"available".
_RSS_KW_RES = [
    re.compile(r"\b" + re.escape(kw) + (r"\b" if len(kw) <= 3 else ""))
    for kw in RSS_KEYWORDS
]


def rss_matches_keywords(item):
    text = (item["title"] + " " + item["summary"]).lower()
    return any(rx.search(text) for rx in _RSS_KW_RES)


def fetch_top_rss_item():
    for name, url in RSS_FEEDS:
        try:
            items = fetch_rss_items(url, name)
            for item in items:
                if rss_matches_keywords(item):
                    return item
        except Exception:
            continue
    return None


def fetch_hn(count=3):
    now_ts = int(datetime.now(timezone.utc).timestamp())
    cutoff_ts = now_ts - (48 * 3600)

    url = (
        "https://hn.algolia.com/api/v1/search"
        f"?query=&tags=story&hitsPerPage=100&numericFilters=created_at_i>{cutoff_ts}"
    )
    with urllib.request.urlopen(req_orig(url), timeout=10) as r:
        hits = json.loads(r.read()).get("hits", [])

    sorted_hits = sorted(hits, key=lambda x: x.get("num_comments", 0), reverse=True)
    results = []
    for h in sorted_hits[:count]:
        title = h.get("title", "")
        if not title:
            continue
        hn_url  = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        hn_link = f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        results.append({
            "title":    title,
            "url":      hn_url,
            "hn_link":  hn_link,
            "points":   h.get("points", 0),
            "comments": h.get("num_comments", 0),
        })
    return results


def req_orig(url, headers=None):
    """Build a urllib.request.Request with default UA headers."""
    h = {"User-Agent": "Hermes/1.0 tech-digest"}
    if headers:
        h.update(headers)
    return urllib.request.Request(url, headers=h)


def fetch_arxiv(count=3):
    query = urllib.parse.quote("cat:cs.AI OR cat:cs.LG OR cat:cs.RO")
    url = (
        "http://export.arxiv.org/api/query"
        f"?search_query={query}"
        "&sortBy=submittedDate&sortOrder=descending"
        f"&max_results={count}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Hermes/1.0 tech-digest"})
    with urllib.request.urlopen(req, timeout=15) as r:
        root = ET.fromstring(r.read())
    ns = {"a": "http://www.w3.org/2005/Atom"}
    papers = []
    for entry in root.findall("a:entry", ns):
        title   = entry.find("a:title",   ns).text.strip().replace("\n", " ")
        summary = entry.find("a:summary", ns).text.strip().replace("\n", " ")
        link    = ""
        for lnk in entry.findall("a:link", ns):
            if lnk.get("type") == "text/html":
                link = lnk.get("href", "")
                break
        if not link:
            id_text = entry.find("a:id", ns).text
            link = id_text.replace("http://", "https://")
        papers.append({"title": title, "summary": truncate(summary, 160), "url": link})
    return papers


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    utc = datetime.now(timezone.utc)
    dst = (utc.month > 3 or (utc.month == 3 and utc.day >= 8)) and utc.month < 11
    now = utc.astimezone(timezone(timedelta(hours=-7 if dst else -8)))
    date_str = now.strftime("%B %-d, %Y")
    iso_date = now.strftime("%Y-%m-%d")

    data = {
        "date": date_str,
        "fetched_at_utc": utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "news": [],       # reserved; Hermes-side web_search handles fresh news
        "rss_top": None,
        "hn": [],         # list of {"title", "url", "hn_link", "points", "comments"}
        "arxiv": [],      # list of {"title", "summary", "url"}
        "errors": [],
    }

    # (Brave News removed — Hermes itself uses web_search for fresh news.)

    # RSS top pick
    try:
        rss = fetch_top_rss_item()
        if rss:
            data["rss_top"] = rss
    except Exception as e:
        data["errors"].append(f"RSS: {e}")

    # Hacker News
    try:
        data["hn"] = fetch_hn(3)
    except Exception as e:
        data["errors"].append(f"HN: {e}")

    # ArXiv
    try:
        data["arxiv"] = fetch_arxiv(3)
    except Exception as e:
        data["errors"].append(f"ArXiv: {e}")

    # Write output
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"tech_digest_data_{iso_date}.json"
    try:
        with open(out_path, "w") as f:
            json.dump(data, f, indent=2)
        print(f"✅ Wrote {out_path}")
        print(f"   news={len(data['news'])} rss={data['rss_top'] is not None} hn={len(data['hn'])} arxiv={len(data['arxiv'])} errors={len(data['errors'])}")
    except Exception as e:
        print(f"FATAL: could not write {out_path}: {e}", file=sys.stderr)
        sys.exit(2)

    sys.exit(0 if not data["errors"] else 1)


if __name__ == "__main__":
    main()
