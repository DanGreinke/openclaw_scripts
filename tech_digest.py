#!/usr/bin/env python3
"""
tech_digest.py
Daily tech digest: agentic AI, autonomous vehicles, emerging tech.
Sources: Brave News API, RSS feeds, Hacker News (Algolia), ArXiv.
Sends 3 Telegram messages: News | Community | Research.
"""

import json, os, re, subprocess, sys, urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

BRAVE_KEY       = os.environ.get("BRAVE_SEARCH_API_KEY", "")
TELEGRAM_TARGET = "8438066154"

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

# --- Brave News: one query per topic ---
BRAVE_TOPICS = [
    ("🤖 Agentic AI",          "agentic AI LLM agent news"),
    ("🚗 Autonomous Vehicles",  "autonomous vehicle self-driving news"),
    ("⚡ Emerging Tech",        "emerging technology breakthrough 2026"),
]


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

def strip_html(text):
    return re.sub(r"<[^>]+>", "", text or "").strip()


def truncate(text, n=140):
    text = text.strip()
    return text[:n] + "…" if len(text) > n else text


def fetch_brave(query):
    """Return top result for query as {title, description, url}."""
    if not BRAVE_KEY:
        return None
    url = (
        "https://api.search.brave.com/res/v1/news/search"
        f"?q={urllib.parse.quote(query)}&count=1&freshness=pd"
    )
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "X-Subscription-Token": BRAVE_KEY,
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        results = json.loads(r.read()).get("results", [])
    if not results:
        return None
    item = results[0]
    return {
        "title": item.get("title", ""),
        "summary": truncate(item.get("description", "")),
        "url": item.get("url", ""),
    }


def fetch_rss_items(url, source_name):
    """Fetch and parse an RSS 2.0 feed, return list of items."""
    req = urllib.request.Request(url, headers={"User-Agent": "OpenClaw/1.0 tech-digest"})
    with urllib.request.urlopen(req, timeout=10) as r:
        root = ET.fromstring(r.read())
    items = []
    # Handle both RSS 2.0 and Atom
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

        # Atom <link> is an attribute, not text
        if not link:
            el = entry.find("{http://www.w3.org/2005/Atom}link")
            if el is not None:
                link = el.get("href", "")

        items.append({"title": title, "summary": truncate(desc), "url": link, "source": source_name})
    return items


def rss_matches_keywords(item):
    text = (item["title"] + " " + item["summary"]).lower()
    return any(kw in text for kw in RSS_KEYWORDS)


def fetch_top_rss_item():
    """Return the single most relevant recent item across all RSS feeds."""
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
    """Fetch top HN stories matching tech topics by score."""
    query = urllib.parse.quote("AI agent LLM autonomous self-driving")
    url = (
        "https://hn.algolia.com/api/v1/search"
        f"?query={query}&tags=story&hitsPerPage=20&numericFilters=points>20"
    )
    with urllib.request.urlopen(url, timeout=10) as r:
        hits = json.loads(r.read()).get("hits", [])
    seen = set()
    results = []
    for h in sorted(hits, key=lambda x: x.get("points", 0), reverse=True):
        title = h.get("title", "")
        if not title or title in seen:
            continue
        seen.add(title)
        hn_url  = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        hn_link = f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        results.append({
            "title":    title,
            "url":      hn_url,
            "hn_link":  hn_link,
            "points":   h.get("points", 0),
            "comments": h.get("num_comments", 0),
        })
        if len(results) >= count:
            break
    return results


def fetch_arxiv(count=3):
    """Fetch recent papers from cs.AI, cs.LG, cs.RO."""
    query = urllib.parse.quote("cat:cs.AI OR cat:cs.LG OR cat:cs.RO")
    url = (
        "http://export.arxiv.org/api/query"
        f"?search_query={query}"
        "&sortBy=submittedDate&sortOrder=descending"
        f"&max_results={count}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "OpenClaw/1.0 tech-digest"})
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
# Formatting & sending
# ---------------------------------------------------------------------------

def send_telegram(message):
    result = subprocess.run(
        ["openclaw", "message", "send",
         "--channel", "telegram",
         "--target", TELEGRAM_TARGET,
         "--message", message],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Send failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)


def main():
    utc = datetime.now(timezone.utc)
    dst = (utc.month > 3 or (utc.month == 3 and utc.day >= 8)) and utc.month < 11
    now = utc.astimezone(timezone(timedelta(hours=-7 if dst else -8)))
    date_str = now.strftime("%B %-d, %Y")

    # ------------------------------------------------------------------
    # Message 1: News (Brave per topic + one RSS item)
    # ------------------------------------------------------------------
    news_lines = [f"📰 Tech News — {date_str}\n"]

    if not BRAVE_KEY:
        news_lines.append("(Brave API key not set)")
    else:
        for label, query in BRAVE_TOPICS:
            try:
                item = fetch_brave(query)
                if item:
                    news_lines.append(f"{label}")
                    news_lines.append(f"• {item['title']}")
                    if item["summary"]:
                        news_lines.append(f"  {item['summary']}")
                    news_lines.append(f"  🔗 {item['url']}")
                    news_lines.append("")
            except Exception as e:
                news_lines.append(f"{label}: error — {e}\n")

    try:
        rss_item = fetch_top_rss_item()
        if rss_item:
            news_lines.append(f"📡 {rss_item['source']}")
            news_lines.append(f"• {rss_item['title']}")
            if rss_item["summary"]:
                news_lines.append(f"  {rss_item['summary']}")
            news_lines.append(f"  🔗 {rss_item['url']}")
    except Exception as e:
        news_lines.append(f"RSS: error — {e}")

    send_telegram("\n".join(news_lines))
    print("✅ News sent.")

    # ------------------------------------------------------------------
    # Message 2: Hacker News
    # ------------------------------------------------------------------
    hn_lines = ["💬 Hacker News\n"]
    try:
        stories = fetch_hn(3)
        for s in stories:
            hn_lines.append(f"• {s['title']}")
            hn_lines.append(f"  ↑ {s['points']} · {s['comments']} comments")
            hn_lines.append(f"  🔗 {s['url']}")
            hn_lines.append(f"  💬 {s['hn_link']}")
            hn_lines.append("")
    except Exception as e:
        hn_lines.append(f"Error: {e}")

    send_telegram("\n".join(hn_lines))
    print("✅ HN sent.")

    # ------------------------------------------------------------------
    # Message 3: ArXiv research
    # ------------------------------------------------------------------
    arxiv_lines = ["🔬 Research\n"]
    try:
        papers = fetch_arxiv(3)
        for p in papers:
            arxiv_lines.append(f"• {p['title']}")
            if p["summary"]:
                arxiv_lines.append(f"  {p['summary']}")
            arxiv_lines.append(f"  🔗 {p['url']}")
            arxiv_lines.append("")
    except Exception as e:
        arxiv_lines.append(f"Error: {e}")

    send_telegram("\n".join(arxiv_lines))
    print("✅ Research sent.")


if __name__ == "__main__":
    main()
