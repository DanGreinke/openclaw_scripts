#!/usr/bin/env python3
"""
trending.py — trend-detection stage for the tech digest.

Pipeline (per the trend-detection design):
  Stage 1 (harvest):  HF daily papers (top 10 by upvotes)
                      + HN top stories (top 6 by comments, last 48h)
                      + keyword-matched editorial RSS items
  Stage 2 (signals):  per candidate — HN Algolia, GitHub repo search,
                      Bluesky*
                      (* Bluesky fails soft: IP-blocked from the Railway
                       container as of 2026-10. Circuit breaker disables it
                       after the first 403 so we don't burn time; it
                       re-enables automatically if unblocked. A blocked
                       source is reported in sources_status, not as an error.)
  Stage 3 (score):    trend_score = Σ log1p(engagement) per source
  Stage 5 (persist):  trending_data_<DATE>.json + trailing-7-day history
                      (days_seen → consecutive days on the list, rising
                       flag vs trailing average)

<DATE> is the Pacific-time date, matching tech_digest.py, so the summarizer
can tell whether both files are from today.

Output: $TECH_DIGEST_DIR/trending_data_<YYYY-MM-DD>.json
        (TECH_DIGEST_DIR defaults to /data/.hermes/scripts)
Exit codes: 0 = success, 1 = partial (some sources failed), 2 = fatal
"""

import email.utils
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from tech_digest import local_now

UA = "Hermes/1.0 tech-digest"
TIMEOUT = 12
OUT_DIR = Path(os.environ.get("TECH_DIGEST_DIR", "/data/.hermes/scripts"))
HISTORY_DAYS = 7

HF_PAPER_COUNT = 10
HN_STORY_COUNT = 6
RSS_PER_FEED = 3
RSS_MAX_AGE_H = 72

# Editorial feeds — the "smart people are writing about X" coverage signal.
# Items are keyword-filtered to stay in the digest's scope, then scored via
# the same cross-source lookup as papers (RSS itself carries no metrics).
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
# Substring matching lets "ai" hit "said"/"available". Short tokens (<=3 chars)
# get full word boundaries; longer ones get a prefix boundary so "robot" still
# matches "robotics".
_RSS_KW_RES = [
    re.compile(r"\b" + re.escape(kw) + (r"\b" if len(kw) <= 3 else ""))
    for kw in RSS_KEYWORDS
]


def _rss_keyword_match(text):
    return any(rx.search(text) for rx in _RSS_KW_RES)

STOPWORDS = {
    "a", "an", "the", "of", "for", "and", "or", "to", "in", "on", "with", "via",
    "at", "by", "from", "is", "are", "be", "as", "we", "our", "this", "that",
    "these", "those", "it", "its", "can", "do", "does", "how", "why", "what",
    "toward", "towards", "using", "use", "based", "new", "into", "without",
    "more", "most", "less", "than", "not", "no", "all", "any",
    # HN title prefixes ("Show HN: ...") carry no topic signal
    "show", "ask", "hn", "launch",
}

_errors = []
_last_github = [0.0]
# Circuit breakers: once a source hard-fails (403), skip it for the rest
# of the run so we don't pay the rate-limit sleep 16 times for nothing.
_source_down = {"hn": False, "github": False, "bsky": False}
# Sources whose 403 is expected (IP-blocked host) and shouldn't count as an
# error. A GitHub/HN 403 is a rate limit or auth problem worth surfacing.
SOFT_SOURCES = {"bsky"}


def local_today():
    """Today's date in Pacific time, as YYYY-MM-DD (same as tech_digest.py)."""
    return local_now(datetime.now(timezone.utc)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Secrets / HTTP
# ---------------------------------------------------------------------------

def _load_secret(env_path, key):
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


ENV_PATH = os.environ.get("HERMES_ENV_FILE", "/data/.hermes/.env")
GITHUB_TOKEN = _load_secret(ENV_PATH, "GITHUB_API_KEY")
# Search API limit: 30 req/min authenticated, 10 req/min without a token.
GITHUB_GAP = 2.1 if GITHUB_TOKEN else 6.1


def fetch_json(url, extra_headers=None):
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def sig_words(title, n=6):
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-']*", title or "")
    out = [w for w in words if len(w) > 1 and w.lower() not in STOPWORDS]
    return out[:n]


_ARXIV_ID_RE = re.compile(r"arxiv\.org/(?:abs|pdf|html)/(\d{4}\.\d{4,5})", re.I)


def arxiv_id_from_url(url):
    """'https://arxiv.org/pdf/2410.12345v2' → '2410.12345' (else '')."""
    m = _ARXIV_ID_RE.search(url or "")
    return m.group(1) if m else ""


def _github_wait():
    delta = time.time() - _last_github[0]
    if delta < GITHUB_GAP:
        time.sleep(GITHUB_GAP - delta)
    _last_github[0] = time.time()


# ---------------------------------------------------------------------------
# Stage 1 — harvest
# ---------------------------------------------------------------------------

def harvest_hf_papers(count=HF_PAPER_COUNT):
    """HuggingFace daily papers, pre-filtered by community upvotes."""
    data = fetch_json("https://huggingface.co/api/daily_papers")
    if isinstance(data, dict):
        data = data.get("papers") or data.get("dailyPapers") or []
    items = []
    for item in data:
        if not isinstance(item, dict):
            continue
        paper = item.get("paper") or {}
        if not isinstance(paper, dict):
            paper = {}
        title = paper.get("title") or item.get("title") or ""
        arxiv_id = paper.get("id") or item.get("id") or ""
        up = item.get("upvotes")
        if up is None:
            up = paper.get("upvotes")
        try:
            up = int(up or 0)
        except (TypeError, ValueError):
            up = 0
        if not title or not arxiv_id:
            continue
        items.append({
            "key": f"arxiv:{arxiv_id}",
            "kind": "paper",
            "title": title,
            "id": arxiv_id,
            "url": f"https://arxiv.org/abs/{arxiv_id}",
            "arxiv_id": arxiv_id,
            "hn_link": "",
            "hf_upvotes": up,
        })
    items.sort(key=lambda x: x["hf_upvotes"], reverse=True)
    return items[:count]


def harvest_hn_stories(count=HN_STORY_COUNT):
    """Top HN stories from the last 48h, ranked by comments."""
    cutoff = int((datetime.now(timezone.utc) - timedelta(hours=48)).timestamp())
    url = (
        "https://hn.algolia.com/api/v1/search"
        f"?query=&tags=story&hitsPerPage=100&numericFilters=created_at_i>{cutoff}"
    )
    hits = fetch_json(url).get("hits", [])
    hits = [h for h in hits if h.get("title")]
    hits.sort(key=lambda h: h.get("num_comments", 0), reverse=True)
    items = []
    for h in hits[:count]:
        oid = h.get("objectID", "")
        url = h.get("url") or f"https://news.ycombinator.com/item?id={oid}"
        items.append({
            "key": f"hn:{oid}",
            "kind": "story",
            "title": h.get("title", ""),
            "id": oid,
            "url": url,
            "arxiv_id": arxiv_id_from_url(url),
            "hn_link": f"https://news.ycombinator.com/item?id={oid}",
            "hf_upvotes": 0,
            # Front-page harvest already gives us the HN signal for free.
            "_hn_preset": {
                "threads": 1,
                "points": int(h.get("points") or 0),
                "comments": int(h.get("num_comments") or 0),
                "query": "",
            },
        })
    return items


def _parse_pubdate(text):
    """RFC-2822 (RSS) or ISO-8601 (Atom) → aware datetime, or None."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(text)
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def harvest_rss_articles():
    """Recent keyword-matched items from the editorial feeds."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=RSS_MAX_AGE_H)
    items = []
    for source, url in RSS_FEEDS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                root = ET.fromstring(r.read())
        except Exception as e:
            _errors.append(f"rss:{source}: {type(e).__name__}: {e}")
            continue
        channel = root.find("channel")
        entries = channel.findall("item") if channel is not None else []
        kept = 0
        for entry in entries:
            if kept >= RSS_PER_FEED:
                break
            def _text(tag):
                el = entry.find(tag)
                return (el.text or "").strip() if el is not None else ""
            title = _text("title")
            link = _text("link")
            pub = _parse_pubdate(_text("pubDate"))
            if not title or not link:
                continue
            if pub and pub < cutoff:
                continue
            text = (title + " " + _text("description")).lower()
            if not _rss_keyword_match(text):
                continue
            items.append({
                "key": f"rss:{link}",
                "kind": "article",
                "title": title,
                "id": "",
                "url": link,
                "arxiv_id": arxiv_id_from_url(link),
                "hn_link": "",
                "hf_upvotes": 0,
                "feed": source,
            })
            kept += 1
    return items


def _canon_url(url):
    """Normalize a URL for cross-source dedupe. arXiv abs/pdf/html links
    (with or without a version suffix) all collapse to the paper id."""
    aid = arxiv_id_from_url(url)
    if aid:
        return f"arxiv:{aid}"
    u = (url or "").strip().lower()
    u = re.sub(r"^https?://(www\.)?", "", u)
    u = u.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    return u


def dedupe_candidates(candidates):
    """Same article can arrive as HN story + HF paper + RSS item. Collapse by
    canonical URL; highest-priority record survives, inheriting useful fields
    (arxiv id, HF upvotes) from the merged duplicates."""
    PRIORITY = {"story": 0, "paper": 1, "article": 2}
    by_url = {}
    for c in candidates:
        u = _canon_url(c.get("url"))
        if not u:
            by_url[id(c)] = c
            continue
        if u not in by_url:
            by_url[u] = c
            continue
        keep, drop = by_url[u], c
        if PRIORITY.get(drop["kind"], 9) < PRIORITY.get(keep["kind"], 9):
            keep, drop = drop, keep
        # merge: survivor inherits the loser's signals and identity
        keep["hf_upvotes"] = max(keep.get("hf_upvotes", 0), drop.get("hf_upvotes", 0))
        keep["arxiv_id"] = keep.get("arxiv_id") or drop.get("arxiv_id", "")
        keep["merged_keys"] = (keep.get("merged_keys", []) + [drop["key"]]
                               + drop.get("merged_keys", []))
        by_url[u] = keep
    return list(by_url.values())


# ---------------------------------------------------------------------------
# Stage 2 — signals
# ---------------------------------------------------------------------------

def hn_stats(words):
    q = urllib.parse.quote(" ".join(words))
    url = f"https://hn.algolia.com/api/v1/search?query={q}&tags=story&hitsPerPage=10"
    hits = fetch_json(url).get("hits") or []
    points = comments = 0
    for h in hits:
        try:
            points += int(h.get("points") or 0)
        except (TypeError, ValueError):
            pass
        try:
            comments += int(h.get("num_comments") or 0)
        except (TypeError, ValueError):
            pass
    return {"threads": len(hits), "points": points, "comments": comments,
            "query": " ".join(words)}


GITHUB_FALLBACK_AGE_D = 90
GITHUB_FALLBACK_STAR_CAP = 1000


def github_stats(query, fallback=False):
    """GitHub repo search — 'is anyone implementing this?' signal.

    Exact mode (arxiv id): search READMEs, where implementations cite the
    paper. Fallback mode (title words): only count repos created recently
    and cap each repo's stars, so a generic title can't match an unrelated
    100k-star project and dominate the score."""
    _github_wait()
    if fallback:
        since = (datetime.now(timezone.utc)
                 - timedelta(days=GITHUB_FALLBACK_AGE_D)).strftime("%Y-%m-%d")
        q = f"{query} created:>{since}"
    else:
        q = f'"{query}" in:readme'
    url = (f"https://api.github.com/search/repositories?q={urllib.parse.quote(q)}"
           "&sort=stars&order=desc&per_page=5")
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    data = fetch_json(url, extra_headers=headers)
    repos = data.get("items") or []
    stars = 0
    for r in repos:
        try:
            n = int(r.get("stargazers_count") or 0)
        except (TypeError, ValueError):
            continue
        stars += min(n, GITHUB_FALLBACK_STAR_CAP) if fallback else n
    return {"repos": len(repos), "stars": stars, "query": q}


def bsky_stats(query):
    url = (
        "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts?q="
        + urllib.parse.quote(query)
        + "&limit=25"
    )
    data = fetch_json(url)
    posts = data.get("posts") or []
    likes = 0
    for p in posts:
        try:
            likes += int(p.get("likeCount") or 0)
        except (TypeError, ValueError):
            pass
    return {"posts": len(posts), "likes": likes, "query": query}


def fetch_trending_repos(count=5):
    """New repos gaining stars fast — 'trending on GitHub' proxy, independent
    of any paper/story. created:>7d + min-stars filters out stale projects."""
    _github_wait()
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    q = urllib.parse.quote(f"created:>{since} stars:>100")
    url = (f"https://api.github.com/search/repositories?q={q}"
           f"&sort=stars&order=desc&per_page={count}")
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    data = fetch_json(url, extra_headers=headers)
    repos = []
    for r in (data.get("items") or [])[:count]:
        repos.append({
            "name": r.get("full_name") or r.get("name") or "",
            "url": r.get("html_url") or "",
            "description": (r.get("description") or "")[:200],
            "stars": int(r.get("stargazers_count") or 0),
            "language": r.get("language") or "",
        })
    return repos


def _try_source(name, fn, *args, **kwargs):
    """Call a signal source; circuit-break on hard failure (403).

    A blocked SOFT_SOURCES source is reported via sources_status rather
    than _errors, so a source that's been blocked for weeks doesn't make
    every run 'partial'."""
    if _source_down.get(name):
        return None
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        blocked = getattr(exc, "code", None) == 403 or "Blocked" in str(exc)
        if blocked:
            _source_down[name] = True
            print(f"{name}: blocked (403), disabled for this run")
        if not (blocked and name in SOFT_SOURCES):
            _errors.append(f"{name}:{args[0] if args else ''}: "
                           f"{type(exc).__name__}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Stage 3 — score
# ---------------------------------------------------------------------------

def score_candidate(c):
    title = c["title"]
    words = sig_words(title, 6)

    # HN
    if c.get("_hn_preset"):
        hn = c["_hn_preset"]
    elif words:
        hn = _try_source("hn", hn_stats, words) or \
            {"threads": 0, "points": 0, "comments": 0, "query": " ".join(words)}
    else:
        hn = {"threads": 0, "points": 0, "comments": 0, "query": ""}

    # GitHub / Bluesky: arxiv id first (exact), title words as fallback.
    # Keyed on arxiv_id, not kind, so an HN story that links a paper
    # still gets the exact lookup.
    aid = c.get("arxiv_id")
    query = " ".join(words)

    gh = None
    if aid:
        gh = _try_source("github", github_stats, aid)
    if (gh is None or gh["repos"] == 0) and words:
        gh = _try_source("github", github_stats, query, fallback=True) or gh
    if gh is None:
        gh = {"repos": 0, "stars": 0, "query": ""}

    bsky = None
    if aid:
        bsky = _try_source("bsky", bsky_stats, aid)
    if (bsky is None or bsky["posts"] == 0) and words:
        bsky = _try_source("bsky", bsky_stats, query) or bsky
    if bsky is None:
        bsky = {"posts": 0, "likes": 0, "query": "", "unavailable": True}

    score = (
        math.log1p(hn["points"] + 2 * hn["comments"])
        + math.log1p(gh["stars"] + 10 * gh["repos"])
        + math.log1p(5 * bsky["posts"] + bsky["likes"])
        + math.log1p(2 * c.get("hf_upvotes", 0))
    )

    c.pop("_hn_preset", None)
    c["signals"] = {"hn": hn, "github": gh, "bsky": bsky}
    c["score"] = round(score, 2)
    return c


# ---------------------------------------------------------------------------
# Stage 5 — persistence + history
# ---------------------------------------------------------------------------

def load_history(today):
    """Return {key: {date: score}} for the HISTORY_DAYS calendar days
    before today. Merged duplicates are indexed under every key they had,
    so an item keeps its history when a different source wins the dedupe."""
    oldest = (date.fromisoformat(today) - timedelta(days=HISTORY_DAYS)).isoformat()
    history = {}
    for path in OUT_DIR.glob("trending_data_*.json"):
        date_part = path.stem.replace("trending_data_", "")
        if not (oldest <= date_part < today):
            continue
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            continue
        for c in data.get("candidates", []):
            for key in [c.get("key", "")] + c.get("merged_keys", []):
                history.setdefault(key, {})[date_part] = c.get("score", 0.0)
    return history


def annotate_history(candidates, history, today):
    for c in candidates:
        past = {}
        for key in [c["key"]] + c.get("merged_keys", []):
            past.update(history.get(key, {}))
        # Consecutive days on the list, counting back from yesterday.
        streak, d = 1, date.fromisoformat(today)  # today counts
        while (d - timedelta(days=1)).isoformat() in past:
            d -= timedelta(days=1)
            streak += 1
        c["days_seen"] = streak
        c["prev_scores"] = [round(past[k], 2) for k in sorted(past, reverse=True)]
        if past:
            avg = sum(past.values()) / len(past)
            c["rising"] = c["score"] > max(avg * 1.5, avg + 1.0)
        else:
            c["rising"] = False  # no baseline yet — new ≠ rising
    return candidates


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    today = local_today()
    print(f"=== trending.py {today} ===")

    # Stage 1
    candidates = []
    try:
        papers = harvest_hf_papers()
        candidates.extend(papers)
        print(f"harvest: {len(papers)} HF papers")
    except Exception as e:
        _errors.append(f"harvest-hf: {type(e).__name__}: {e}")
    try:
        stories = harvest_hn_stories()
        candidates.extend(stories)
        print(f"harvest: {len(stories)} HN stories")
    except Exception as e:
        _errors.append(f"harvest-hn: {type(e).__name__}: {e}")
    try:
        articles = harvest_rss_articles()
        candidates.extend(articles)
        print(f"harvest: {len(articles)} RSS articles")
    except Exception as e:
        _errors.append(f"harvest-rss: {type(e).__name__}: {e}")

    before = len(candidates)
    candidates = dedupe_candidates(candidates)
    if len(candidates) < before:
        print(f"dedupe: {before} -> {len(candidates)} candidates")

    if not candidates:
        print("FATAL: no candidates harvested", file=sys.stderr)
        sys.exit(2)

    # Stages 2+3
    total = len(candidates)
    results = []
    for i, c in enumerate(candidates):
        print(f"[{i+1}/{total}] {c['title'][:60]}")
        results.append(score_candidate(c))
    results.sort(key=lambda r: r["score"], reverse=True)

    # Stage 5
    history = load_history(today)
    results = annotate_history(results, history, today)

    print()
    print(f"{'#':>2} {'score':>5}  title")
    for rank, r in enumerate(results, 1):
        hn, gh = r["signals"]["hn"], r["signals"]["github"]
        bs = r["signals"]["bsky"]
        flags = []
        if r["days_seen"] > 1:
            flags.append(f"{r['days_seen']}d")
        if r["rising"]:
            flags.append("RISING")
        flag_s = f" [{','.join(flags)}]" if flags else ""
        print(f"{rank:>2} {r['score']:>5.1f}  {r['title'][:65]}{flag_s}")
        print(f"      HN {hn['threads']}th/{hn['points']}pt/{hn['comments']}c"
              f" | GH {gh['repos']}repo/{gh['stars']}★"
              f" | BS {'n/a' if bs.get('unavailable') else str(bs['posts']) + 'p'}"
              f" | HF {r.get('hf_upvotes', 0)}")

    # Standalone GitHub trending list (not part of candidate scoring)
    github_trending = []
    try:
        github_trending = fetch_trending_repos()
        print(f"github trending: {len(github_trending)} repos")
    except Exception as e:
        _errors.append(f"github-trending: {type(e).__name__}: {e}")

    out = {
        "date": today,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources_status": {k: ("blocked" if v else "ok")
                           for k, v in _source_down.items()},
        "candidates": results,
        "github_trending": github_trending,
        "errors": _errors,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"trending_data_{today}.json"
    try:
        with open(out_path, "w") as f:
            json.dump(out, f, indent=2)
        print(f"\nwrote {out_path}")
    except Exception as e:
        print(f"FATAL: could not write {out_path}: {e}", file=sys.stderr)
        sys.exit(2)

    sys.exit(0 if not _errors else 1)


if __name__ == "__main__":
    main()
