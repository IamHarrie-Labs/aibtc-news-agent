#!/usr/bin/env python3
"""
scrape_sources.py

Pulls fresh data from RSS news feeds and primary APIs.
btc_macro bucket uses real macro news RSS feeds (CoinDesk, Bitcoin Magazine,
The Block, Decrypt) — NOT raw on-chain data — so signals can score 80+.

Output: JSON file consumed by run_agent.py.
"""

import argparse
import json
import time
import sys
from datetime import datetime, timezone, timedelta
import urllib.request
import urllib.error

try:
    import feedparser
except ImportError:
    print("Missing dep. Run: pip install feedparser")
    sys.exit(1)


# ---------------------------------------------------------------------------
# RSS and API sources
# ---------------------------------------------------------------------------

SOURCES = {
    # ---- Bitcoin Macro: REAL NEWS FEEDS (institutional, ETF, regulatory) ----
    "btc_macro": [
        {
            "name": "CoinDesk Markets",
            "url": "https://www.coindesk.com/arc/outboundfeeds/rss/category/markets/",
            "type": "rss",
            "beat": "Bitcoin Macro",
            "parser": "rss_feed",
        },
        {
            "name": "CoinDesk Business",
            "url": "https://www.coindesk.com/arc/outboundfeeds/rss/category/business/",
            "type": "rss",
            "beat": "Bitcoin Macro",
            "parser": "rss_feed",
        },
        {
            "name": "Bitcoin Magazine",
            "url": "https://bitcoinmagazine.com/feed",
            "type": "rss",
            "beat": "Bitcoin Macro",
            "parser": "rss_feed",
        },
        {
            "name": "The Block",
            "url": "https://www.theblock.co/rss.xml",
            "type": "rss",
            "beat": "Bitcoin Macro",
            "parser": "rss_feed",
        },
        {
            "name": "Decrypt Bitcoin",
            "url": "https://decrypt.co/feed/bitcoin",
            "type": "rss",
            "beat": "Bitcoin Macro",
            "parser": "rss_feed",
        },
        {
            "name": "Investing.com Crypto",
            "url": "https://www.investing.com/rss/news_301.rss",
            "type": "rss",
            "beat": "Bitcoin Macro",
            "parser": "rss_feed",
        },
    ],

    # ---- Bitcoin Infrastructure: on-chain metrics + GitHub ----
    "btc_infrastructure": [
        {
            "name": "Mempool Fee Rates",
            "url": "https://mempool.space/api/v1/fees/recommended",
            "type": "on-chain",
            "beat": "Bitcoin Infrastructure",
            "parser": "mempool_fees",
        },
        {
            "name": "Mempool Recent Blocks",
            "url": "https://mempool.space/api/v1/blocks",
            "type": "on-chain",
            "beat": "Bitcoin Infrastructure",
            "parser": "mempool_blocks",
        },
        {
            "name": "Hiro Latest Stacks Block",
            "url": "https://api.hiro.so/extended/v1/block?limit=1",
            "type": "on-chain",
            "beat": "Bitcoin Infrastructure",
            "parser": "hiro_block",
        },
        {
            "name": "Bitcoin Core Releases",
            "url": "https://api.github.com/repos/bitcoin/bitcoin/releases?per_page=5",
            "type": "github",
            "beat": "Bitcoin Infrastructure",
            "parser": "github_releases",
        },
    ],

    # ---- Agent Trading: GitHub + AIBTC ecosystem ----
    "agent_trading": [
        {
            "name": "AIBTC MCP Server Releases",
            "url": "https://api.github.com/repos/aibtcdev/aibtc-mcp-server/releases",
            "type": "github",
            "beat": "Agent Trading",
            "parser": "github_releases",
        },
        {
            "name": "AIBTC x402 Relay Releases",
            "url": "https://api.github.com/repos/aibtcdev/x402-sponsor-relay/releases",
            "type": "github",
            "beat": "Agent Trading",
            "parser": "github_releases",
        },
        {
            "name": "Bitflow Skills PRs",
            "url": "https://api.github.com/repos/BitflowFinance/bff-skills/pulls?state=open&per_page=10",
            "type": "github",
            "beat": "Agent Trading",
            "parser": "github_prs",
        },
        {
            "name": "AIBTC MCP Server PRs",
            "url": "https://api.github.com/repos/aibtcdev/aibtc-mcp-server/pulls?state=open&per_page=10",
            "type": "github",
            "beat": "Agent Trading",
            "parser": "github_prs",
        },
    ],

    # ---- AIBTC ecosystem health ----
    "aibtc_ecosystem": [
        {
            "name": "AIBTC News Report",
            "url": "https://aibtc.news/api/report",
            "type": "ecosystem",
            "beat": "Bitcoin Infrastructure",
            "parser": "aibtc_report",
        },
        {
            "name": "AIBTC Activity Feed",
            "url": "https://aibtc.com/api/activity",
            "type": "ecosystem",
            "beat": "Bitcoin Infrastructure",
            "parser": "aibtc_activity",
        },
    ],
}

# 24 slots per day — ALL slots pull btc_macro RSS feeds only.
# Agent beat is bitcoin-macro; infrastructure content is never filed under this beat
# and the infra prompt is not triggered, so mixing in btc_infrastructure sources
# only causes the LLM to incorrectly pick GitHub release notes as macro signals.
SLOT_SOURCE_MAP = {
    1:  ["btc_macro"],
    2:  ["btc_macro"],
    3:  ["btc_macro"],
    4:  ["btc_macro"],
    5:  ["btc_macro"],
    6:  ["btc_macro"],
    7:  ["btc_macro"],
    8:  ["btc_macro"],
    9:  ["btc_macro"],
    10: ["btc_macro"],
    11: ["btc_macro"],
    12: ["btc_macro"],
    13: ["btc_macro"],
    14: ["btc_macro"],
    15: ["btc_macro"],
    16: ["btc_macro"],
    17: ["btc_macro"],
    18: ["btc_macro"],
    19: ["btc_macro"],
    20: ["btc_macro"],
    21: ["btc_macro"],
    22: ["btc_macro"],
    23: ["btc_macro"],
    24: ["btc_macro"],
}


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def fetch_json(url: str, timeout: int = 10):
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "SereneSpring/1.0 (AIBTC News Agent)",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"  [warn] fetch failed for {url}: {e}")
        return None


def parse_rss_feed(data, source: dict) -> list:
    """Parse a feedparser result — fetch the RSS URL via feedparser directly."""
    try:
        feed = feedparser.parse(source["url"])
    except Exception as e:
        print(f"  [warn] feedparser failed for {source['url']}: {e}")
        return []

    articles = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=72)

    for entry in feed.entries[:15]:
        title = entry.get("title", "").strip()
        link = entry.get("link", "")
        summary = entry.get("summary", "") or entry.get("description", "")
        # Strip HTML tags from summary
        import re
        summary = re.sub(r"<[^>]+>", " ", summary).strip()
        summary = " ".join(summary.split())[:500]

        pub_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        if pub_parsed:
            pub = datetime(*pub_parsed[:6], tzinfo=timezone.utc)
            pub_str = pub.isoformat()
        else:
            pub = datetime.now(timezone.utc)
            pub_str = pub.isoformat()

        if pub < cutoff:
            continue

        if not title or not link:
            continue

        articles.append({
            "title": title,
            "url": link,
            "summary": summary[:500],
            "source": source["name"],
            "type": source["type"],
            "published": pub_str,
        })

    return articles


def parse_mempool_fees(data: dict, source: dict) -> list:
    if not data or not isinstance(data, dict):
        return []
    fastest = data.get("fastestFee", "?")
    half_hour = data.get("halfHourFee", "?")
    hour = data.get("hourFee", "?")
    economy = data.get("economyFee", "?")
    return [{
        "title": f"BTC mempool: fastest {fastest} sat/vB, 30-min {half_hour} sat/vB, economy {economy} sat/vB",
        "url": "https://mempool.space/api/v1/fees/recommended",
        "summary": (
            f"Bitcoin fee market snapshot. Fastest: {fastest} sat/vB. "
            f"Half-hour: {half_hour} sat/vB. 1-hour: {hour} sat/vB. "
            f"Economy: {economy} sat/vB."
        ),
        "source": source["name"],
        "type": source["type"],
        "published": datetime.now(timezone.utc).isoformat(),
        "raw": data,
    }]


def parse_mempool_blocks(data: list, source: dict) -> list:
    if not data or not isinstance(data, list):
        return []
    articles = []
    for block in data[:3]:
        height = block.get("height", "?")
        tx_count = block.get("tx_count", "?")
        size = block.get("size", 0)
        size_kb = round(size / 1024, 1) if size else "?"
        ts = block.get("timestamp")
        pub = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else datetime.now(timezone.utc).isoformat()
        articles.append({
            "title": f"Bitcoin block {height}: {tx_count} transactions, {size_kb} KB",
            "url": f"https://mempool.space/block/{block.get('id', '')}",
            "summary": (
                f"Block {height} confirmed with {tx_count} transactions ({size_kb} KB). "
                f"Mined at {pub[:16]} UTC."
            ),
            "source": source["name"],
            "type": source["type"],
            "published": pub,
            "raw": block,
        })
    return articles


def parse_hiro_block(data: dict, source: dict) -> list:
    if not data or not isinstance(data, dict):
        return []
    results = data.get("results", [])
    if not results:
        return []
    block = results[0]
    height = block.get("height", "?")
    tx_count = block.get("txs", [])
    tx_n = len(tx_count) if isinstance(tx_count, list) else block.get("tx_count", "?")
    burn_height = block.get("burn_block_height", "?")
    ts = block.get("burn_block_time")
    pub = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else datetime.now(timezone.utc).isoformat()
    return [{
        "title": f"Stacks block {height} anchored to Bitcoin block {burn_height}: {tx_n} transactions",
        "url": f"https://explorer.hiro.so/block/{block.get('hash', '')}",
        "summary": (
            f"Latest Stacks block #{height} anchored to Bitcoin block {burn_height}. "
            f"Contains {tx_n} transactions. Block finality at {pub[:16]} UTC."
        ),
        "source": source["name"],
        "type": source["type"],
        "published": pub,
        "raw": block,
    }]


def parse_github_releases(data: list, source: dict) -> list:
    if not data or not isinstance(data, list):
        return []
    articles = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    for release in data[:5]:
        pub_str = release.get("published_at") or release.get("created_at", "")
        try:
            pub = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
        except Exception:
            pub = datetime.now(timezone.utc)
        if pub < cutoff:
            continue
        tag = release.get("tag_name", "?")
        name = release.get("name") or tag
        body = (release.get("body") or "")[:300].replace("\r\n", " ").replace("\n", " ")
        repo_url = release.get("html_url", "")
        articles.append({
            "title": f"{source['name']} released {tag}: {name}",
            "url": repo_url,
            "summary": f"{source['name']} published release {tag} on {pub_str[:10]}. {body[:200]}",
            "source": source["name"],
            "type": source["type"],
            "published": pub.isoformat(),
            "raw": {"tag": tag, "name": name, "url": repo_url},
        })
    return articles


def parse_github_prs(data: list, source: dict) -> list:
    if not data or not isinstance(data, list):
        return []
    articles = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    for pr in data[:5]:
        pub_str = pr.get("created_at", "")
        try:
            pub = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
        except Exception:
            pub = datetime.now(timezone.utc)
        if pub < cutoff:
            continue
        title = pr.get("title", "")
        number = pr.get("number", "?")
        html_url = pr.get("html_url", "")
        user = pr.get("user", {}).get("login", "unknown")
        articles.append({
            "title": f"{source['name']} PR #{number}: {title}",
            "url": html_url,
            "summary": (
                f"Open pull request #{number} on {source['name']} opened by {user} on {pub_str[:10]}. "
                f"Title: {title}. URL: {html_url}"
            ),
            "source": source["name"],
            "type": source["type"],
            "published": pub.isoformat(),
            "raw": {"number": number, "title": title, "url": html_url, "user": user},
        })
    return articles


def parse_aibtc_report(data, source: dict) -> list:
    if not data:
        return []
    items = data if isinstance(data, list) else [data]
    articles = []
    for item in items[:3]:
        title = item.get("title") or item.get("headline") or "AIBTC Report"
        url = item.get("url") or item.get("sourceUrl") or "https://aibtc.news/api/report"
        summary = item.get("summary") or item.get("content") or str(item)[:300]
        articles.append({
            "title": title,
            "url": url,
            "summary": summary[:400],
            "source": source["name"],
            "type": source["type"],
            "published": datetime.now(timezone.utc).isoformat(),
        })
    return articles


def parse_aibtc_activity(data, source: dict) -> list:
    if not data:
        return []
    items = data if isinstance(data, list) else [data]
    articles = []
    for item in items[:5]:
        agent = item.get("agentName") or item.get("agent") or "unknown agent"
        action = item.get("action") or item.get("type") or "activity"
        ts = item.get("timestamp") or item.get("createdAt") or ""
        articles.append({
            "title": f"AIBTC activity: {agent} — {action}",
            "url": "https://aibtc.com/api/activity",
            "summary": f"Agent {agent} performed action '{action}' at {ts[:16]}. Raw: {json.dumps(item)[:200]}",
            "source": source["name"],
            "type": source["type"],
            "published": ts or datetime.now(timezone.utc).isoformat(),
        })
    return articles


PARSERS = {
    "rss_feed":        parse_rss_feed,
    "mempool_fees":    parse_mempool_fees,
    "mempool_blocks":  parse_mempool_blocks,
    "hiro_block":      parse_hiro_block,
    "hiro_stx_supply": lambda d, s: [],   # no longer used for macro
    "github_releases": parse_github_releases,
    "github_prs":      parse_github_prs,
    "aibtc_report":    parse_aibtc_report,
    "aibtc_activity":  parse_aibtc_activity,
}


def fetch_source(source: dict) -> list:
    print(f"  Fetching: {source['name']} ({source['type']}) ...")
    if source["type"] == "rss":
        # feedparser handles the fetch internally
        return parse_rss_feed(None, source)

    data = fetch_json(source["url"])
    if data is None:
        return []
    parser_fn = PARSERS.get(source["parser"])
    if not parser_fn:
        print(f"  [warn] no parser for {source['parser']}")
        return []
    return parser_fn(data, source)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot",   type=int, required=True, help="1–24")
    parser.add_argument("--beat",   type=str, required=True)
    parser.add_argument("--output", type=str, default="raw_sources.json")
    args = parser.parse_args()

    buckets = SLOT_SOURCE_MAP.get(args.slot, [])
    if not buckets:
        print(f"No sources mapped for slot {args.slot}. Exiting.")
        sys.exit(0)

    print(f"Slot {args.slot} | Beat: {args.beat}")
    print(f"Pulling from buckets: {', '.join(buckets)}")

    all_articles = []
    seen_urls = set()

    for bucket in buckets:
        for source in SOURCES.get(bucket, []):
            articles = fetch_source(source)
            for a in articles:
                if a["url"] not in seen_urls:
                    seen_urls.add(a["url"])
                    all_articles.append(a)
            time.sleep(0.3)

    # Sort by published date — newest first
    def pub_key(a):
        try:
            return a.get("published", "")
        except Exception:
            return ""

    all_articles.sort(key=pub_key, reverse=True)

    output = {
        "slot": args.slot,
        "beat": args.beat,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "total_fetched": len(all_articles),
        "articles": all_articles,
    }

    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nDone. {len(all_articles)} items fetched → {args.output}")
    for a in all_articles[:8]:
        print(f"  [{a['type']}] {a['title'][:90]}")


if __name__ == "__main__":
    main()
