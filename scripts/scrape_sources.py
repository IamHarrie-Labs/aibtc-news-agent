#!/usr/bin/env python3
"""
scrape_sources.py

Pulls fresh data from primary on-chain and ecosystem APIs.
No RSS feeds — all sources are direct JSON APIs.
Maps each slot to the best source bucket for that beat.

Output: JSON file consumed by run_agent.py.
"""

import argparse
import json
import time
import sys
from datetime import datetime, timezone, timedelta
import urllib.request
import urllib.error


# ---------------------------------------------------------------------------
# Primary API sources — no auth required (GitHub unauthenticated: 60 req/hr)
# ---------------------------------------------------------------------------

SOURCES = {
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
    ],
    "btc_macro": [
        {
            "name": "Hiro STX Supply",
            "url": "https://api.hiro.so/extended/v1/stx_supply",
            "type": "on-chain",
            "beat": "Bitcoin Macro",
            "parser": "hiro_stx_supply",
        },
        {
            "name": "Mempool Fee Rates",
            "url": "https://mempool.space/api/v1/fees/recommended",
            "type": "on-chain",
            "beat": "Bitcoin Macro",
            "parser": "mempool_fees",
        },
        {
            "name": "Mempool Recent Blocks",
            "url": "https://mempool.space/api/v1/blocks",
            "type": "on-chain",
            "beat": "Bitcoin Macro",
            "parser": "mempool_blocks",
        },
    ],
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

# 24 slots (1 per hour) → source buckets
# Rotate through beats: Infrastructure, Macro, Agent Trading
SLOT_SOURCE_MAP = {
    1:  ["btc_infrastructure"],              # 00:00 — infra midnight sweep
    2:  ["agent_trading"],                   # 01:00 — GitHub activity overnight
    3:  ["btc_macro"],                       # 02:00 — macro pre-open
    4:  ["btc_infrastructure"],              # 03:00 — infra
    5:  ["agent_trading", "aibtc_ecosystem"],# 04:00 — ecosystem + trading
    6:  ["btc_macro"],                       # 05:00 — macro
    7:  ["btc_infrastructure"],              # 06:00 — infra morning
    8:  ["agent_trading"],                   # 07:00 — GitHub morning
    9:  ["btc_infrastructure", "btc_macro"], # 08:00 — combined
    10: ["agent_trading"],                   # 09:00 — trading
    11: ["btc_macro"],                       # 10:00 — macro
    12: ["btc_infrastructure"],              # 11:00 — infra
    13: ["agent_trading", "aibtc_ecosystem"],# 12:00 — midday trading
    14: ["btc_macro"],                       # 13:00 — macro
    15: ["btc_infrastructure"],              # 14:00 — infra afternoon
    16: ["agent_trading"],                   # 15:00 — trading
    17: ["btc_macro"],                       # 16:00 — US open macro
    18: ["btc_infrastructure"],              # 17:00 — infra
    19: ["agent_trading", "aibtc_ecosystem"],# 18:00 — trading + ecosystem
    20: ["btc_macro"],                       # 19:00 — macro
    21: ["btc_infrastructure"],              # 20:00 — infra evening
    22: ["agent_trading"],                   # 21:00 — trading
    23: ["btc_macro"],                       # 22:00 — macro wrap
    24: ["btc_infrastructure", "agent_trading"], # 23:00 — full sweep
}


def fetch_json(url: str, timeout: int = 10) -> any:
    """Fetch a URL and return parsed JSON, or None on failure."""
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


def parse_mempool_fees(data: dict, source: dict) -> list:
    if not data or not isinstance(data, dict):
        return []
    fastest = data.get("fastestFee", "?")
    half_hour = data.get("halfHourFee", "?")
    hour = data.get("hourFee", "?")
    economy = data.get("economyFee", "?")
    return [{
        "title": f"BTC mempool fees: fastest {fastest} sat/vB, 30-min {half_hour} sat/vB, 1-hr {hour} sat/vB, economy {economy} sat/vB",
        "url": "https://mempool.space/api/v1/fees/recommended",
        "summary": (
            f"Real-time Bitcoin fee market snapshot from mempool.space. "
            f"Fastest: {fastest} sat/vB. Half-hour: {half_hour} sat/vB. "
            f"1-hour: {hour} sat/vB. Economy: {economy} sat/vB. "
            f"Fee compression indicates current block demand level."
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
                f"Mined at {pub[:16]} UTC. "
                f"Block composition reflects current mempool clearance rate."
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
            f"Contains {tx_n} transactions. "
            f"Block finality at {pub[:16]} UTC. "
            f"Hiro API source: api.hiro.so/extended/v1/block."
        ),
        "source": source["name"],
        "type": source["type"],
        "published": pub,
        "raw": block,
    }]


def parse_hiro_stx_supply(data: dict, source: dict) -> list:
    if not data or not isinstance(data, dict):
        return []
    total = data.get("total_stx", "?")
    unlocked = data.get("unlocked_stx", "?")
    locked = data.get("locked_stx", "?")
    block_height = data.get("block_height", "?")
    return [{
        "title": f"STX supply at block {block_height}: {unlocked} unlocked of {total} total ({locked} locked)",
        "url": "https://api.hiro.so/extended/v1/stx_supply",
        "summary": (
            f"Stacks STX supply snapshot at block {block_height}. "
            f"Total: {total} STX. Unlocked (circulating): {unlocked} STX. "
            f"Locked (staking/vesting): {locked} STX. "
            f"Source: Hiro API /extended/v1/stx_supply."
        ),
        "source": source["name"],
        "type": source["type"],
        "published": datetime.now(timezone.utc).isoformat(),
        "raw": data,
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
            "summary": (
                f"{source['name']} published release {tag} on {pub_str[:10]}. "
                f"{body[:200]}"
            ),
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
                f"Title: {title}. "
                f"URL: {html_url}"
            ),
            "source": source["name"],
            "type": source["type"],
            "published": pub.isoformat(),
            "raw": {"number": number, "title": title, "url": html_url, "user": user},
        })
    return articles


def parse_aibtc_report(data: any, source: dict) -> list:
    if not data:
        return []
    if isinstance(data, list):
        items = data[:3]
    elif isinstance(data, dict):
        items = [data]
    else:
        return []
    articles = []
    for item in items:
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


def parse_aibtc_activity(data: any, source: dict) -> list:
    if not data:
        return []
    if isinstance(data, list):
        items = data[:5]
    elif isinstance(data, dict):
        items = [data]
    else:
        return []
    articles = []
    for item in items:
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
    "mempool_fees":    parse_mempool_fees,
    "mempool_blocks":  parse_mempool_blocks,
    "hiro_block":      parse_hiro_block,
    "hiro_stx_supply": parse_hiro_stx_supply,
    "github_releases": parse_github_releases,
    "github_prs":      parse_github_prs,
    "aibtc_report":    parse_aibtc_report,
    "aibtc_activity":  parse_aibtc_activity,
}


def fetch_source(source: dict) -> list:
    print(f"  Fetching: {source['name']} ...")
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

    output = {
        "slot": args.slot,
        "beat": args.beat,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "total_fetched": len(all_articles),
        "articles": all_articles,
    }

    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nDone. {len(all_articles)} data points fetched → {args.output}")
    for a in all_articles[:5]:
        print(f"  - {a['title'][:90]}")


if __name__ == "__main__":
    main()
