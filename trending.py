#!/usr/bin/env python3
"""
trending.py — trend-detection stage for the tech digest.

Pipeline (per the trend-detection design):
  Stage 1 (harvest):  HF daily papers (top 10 by upvotes)
                      + HN top stories (top 6 by comments, last 48h)
  Stage 2 (signals):  per candidate — HN Algolia, GitHub repo search,
                      Reddit*, Bluesky*
                      (* Reddit/Bluesky fail soft: both IP-blocked from the
                       Railway container as of 2026-10. Circuit breaker
                       disables them after the first 403 so we don't burn
                       time; they re-enable automatically if unblocked.)
  Stage 3 (score):    trend_score = Σ log1p(engagement) per source
  Stage 5 (persist):  trending_data_<DATE>.json + trailing-7-day history
                      (days_seen → "N days straight", rising flag vs
                       trailing average)

Output: /data/.hermes/scripts/trending_data_<YYYY-MM-DD>.json
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
from datetime import datetime, timezone, timedelta
from pathlib import Path

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
TIMEOUT = 12
REDDIT_GAP = 1.2
GITHUB_GAP = 2.1  # authenticated search limit is 30 req/min — stay under it
OUT_DIR = Path("/data/.hermes/scripts")
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
}

_errors = []
_last_reddit = [0.0]
_last_github = [0.0]
# Circuit breakers: once a source hard-fails (403), skip it for the rest
# of the run so we don't pay the rate-limit sleep 16 times for nothing.
_source_down = {"reddit": False, "bsky": False}


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


def fetch_json(url, extra_headers=None):
    headers = {
        "User-Agent": UA,
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def sig_words(title, n=6):
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-']*", title or "")
    out = [w for w in words if len(w) > 1 and w.lower() not in STOPWORDS]
    return out[:n]


def _reddit_wait():
    delta = time.time() - _last_reddit[0]
    if delta < REDDIT_GAP:
        time.sleep(REDDIT_GAP - delta)
    _last_reddit[0] = time.time()


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
        items.append({
            "key": f"hn:{oid}",
            "kind": "story",
            "title": h.get("title", ""),
            "id": oid,
            "url": h.get("url") or f"https://news.ycombinator.com/item?id={oid}",
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
                "hn_link": "",
                "hf_upvotes": 0,
                "feed": source,
            })
            kept += 1
    return items


def _canon_url(url):
    """Normalize a URL for cross-source dedupe."""
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
        # merge: survivor inherits the loser's identifying fields
        if drop.get("hf_upvotes") and not keep.get("hf_upvotes"):
            keep["hf_upvotes"] = drop["hf_upvotes"]
        if drop.get("id") and not keep.get("id"):
            keep["id"] = drop["id"]
        if drop["kind"] == "paper" and drop.get("id"):
            keep["id"] = keep.get("id") or drop["id"]
        keep.setdefault("merged_keys", []).append(drop["key"])
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


def github_stats(query):
    """GitHub repo search — 'is anyone implementing this?' signal."""
    _github_wait()
    q = urllib.parse.quote(query)
    url = f"https://api.github.com/search/repositories?q={q}&sort=stars&order=desc&per_page=5"
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    data = fetch_json(url, extra_headers=headers)
    repos = data.get("items") or []
    stars = 0
    for r in repos:
        try:
            stars += int(r.get("stargazers_count") or 0)
        except (TypeError, ValueError):
            pass
    return {"repos": len(repos), "stars": stars, "query": query}


def reddit_stats(query):
    _reddit_wait()
    url = (
        "https://www.reddit.com/search.json?q="
        + urllib.parse.quote(query)
        + "&sort=new&limit=25&t=week"
    )
    data = fetch_json(url)
    children = (data.get("data") or {}).get("children") or []
    score = comments = 0
    for c in children:
        d = c.get("data") or {}
        try:
            score += int(d.get("score") or 0)
        except (TypeError, ValueError):
            pass
        try:
            comments += int(d.get("num_comments") or 0)
        except (TypeError, ValueError):
            pass
    return {"posts": len(children), "score": score, "comments": comments,
            "query": query}


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


def _try_source(name, fn, *args):
    """Call a signal source; circuit-break on repeated hard failure."""
    if _source_down.get(name):
        return None
    try:
        return fn(*args)
    except Exception as exc:
        _errors.append(f"{name}:{args[0] if args else ''}: "
                       f"{type(exc).__name__}: {exc}")
        # 403 / blocked → disable for the rest of the run
        if "403" in str(exc) or "Blocked" in str(exc):
            _source_down[name] = True
            _errors.append(f"{name}: hard-blocked, disabled for this run")
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

    # GitHub: arxiv id first for papers, title words fallback
    gh = None
    if c["kind"] == "paper" and c.get("id"):
        gh = _try_source("github", github_stats, c["id"])
        if gh and gh["repos"] == 0 and words:
            gh = _try_source("github", github_stats, " ".join(words))
    elif words:
        gh = _try_source("github", github_stats, " ".join(words))
    if gh is None:
        gh = {"repos": 0, "stars": 0, "query": ""}

    # Reddit: arxiv id, title fallback (usually blocked from this host)
    reddit = None
    if c["kind"] == "paper" and c.get("id"):
        reddit = _try_source("reddit", reddit_stats, c["id"])
        if reddit and reddit["posts"] == 0 and words:
            reddit = _try_source("reddit", reddit_stats, " ".join(words)) or reddit
    elif words:
        reddit = _try_source("reddit", reddit_stats, " ".join(words))
    if reddit is None:
        reddit = {"posts": 0, "score": 0, "comments": 0,
                  "query": "", "unavailable": True}

    # Bluesky (usually blocked from this host)
    bsky = None
    if c["kind"] == "paper" and c.get("id"):
        bsky = _try_source("bsky", bsky_stats, c["id"])
        if bsky and bsky["posts"] == 0 and words:
            bsky = _try_source("bsky", bsky_stats, " ".join(words)) or bsky
    elif words:
        bsky = _try_source("bsky", bsky_stats, " ".join(words))
    if bsky is None:
        bsky = {"posts": 0, "likes": 0, "query": "", "unavailable": True}

    score = (
        math.log1p(hn["points"] + 2 * hn["comments"])
        + math.log1p(gh["stars"] + 10 * gh["repos"])
        + math.log1p(reddit["score"] + 2 * reddit["comments"])
        + math.log1p(5 * bsky["posts"] + bsky["likes"])
        + math.log1p(2 * c.get("hf_upvotes", 0))
    )

    c.pop("_hn_preset", None)
    c["signals"] = {"hn": hn, "github": gh, "reddit": reddit, "bsky": bsky}
    c["score"] = round(score, 2)
    return c


# ---------------------------------------------------------------------------
# Stage 5 — persistence + history
# ---------------------------------------------------------------------------

def load_history(today):
    """Return {key: [scores from the trailing HISTORY_DAYS days]}."""
    history = {}
    seen_dates = set()
    for path in sorted(OUT_DIR.glob("trending_data_*.json"), reverse=True):
        date_part = path.stem.replace("trending_data_", "")
        if date_part >= today:
            continue
        if len(seen_dates) >= HISTORY_DAYS:
            break
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            continue
        seen_dates.add(date_part)
        for c in data.get("candidates", []):
            history.setdefault(c.get("key", ""), []).append(
                (date_part, c.get("score", 0.0)))
    return history


def annotate_history(candidates, history):
    for c in candidates:
        past = history.get(c["key"], [])
        c["days_seen"] = len(past) + 1  # today counts
        c["prev_scores"] = [round(s, 2) for _, s in past[:HISTORY_DAYS]]
        if past:
            avg = sum(s for _, s in past) / len(past)
            c["rising"] = c["score"] > max(avg * 1.5, avg + 1.0)
        else:
            c["rising"] = False  # no baseline yet — new ≠ rising
    return candidates


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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
    results = annotate_history(results, history)

    print()
    print(f"{'#':>2} {'score':>5}  title")
    for rank, r in enumerate(results, 1):
        hn, gh = r["signals"]["hn"], r["signals"]["github"]
        rd, bs = r["signals"]["reddit"], r["signals"]["bsky"]
        flags = []
        if r["days_seen"] > 1:
            flags.append(f"{r['days_seen']}d")
        if r["rising"]:
            flags.append("RISING")
        flag_s = f" [{','.join(flags)}]" if flags else ""
        print(f"{rank:>2} {r['score']:>5.1f}  {r['title'][:65]}{flag_s}")
        print(f"      HN {hn['threads']}th/{hn['points']}pt/{hn['comments']}c"
              f" | GH {gh['repos']}repo/{gh['stars']}★"
              f" | R {'n/a' if rd.get('unavailable') else str(rd['posts']) + 'p'}"
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
