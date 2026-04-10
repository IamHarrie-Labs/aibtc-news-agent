#!/usr/bin/env python3
"""
scrape_sources.py

Pulls fresh data from RSS news feeds and primary APIs.
4 slots per day — each slot targets a different beat.

Slot → Beat rotation:
  1 (06:00 UTC) → bitcoin-macro      (institutional, ETF, regulatory)
  2 (12:00 UTC) → quantum            (quantum threat, post-quantum research)
  3 (18:00 UTC) → security/governance (alternates by day-of-month parity)
  4 (00:00 UTC) → infrastructure/agent-economy (alternates by day-of-month parity)
"""

import argparse
import json
import time
import re
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
# Source definitions
# ---------------------------------------------------------------------------

SOURCES = {

    # ---- Bitcoin Macro: institutional, ETF, regulatory ----
    "btc_macro": [
        {"name": "CoinDesk Markets",      "url": "https://www.coindesk.com/arc/outboundfeeds/rss/category/markets/",   "type": "rss", "beat": "bitcoin-macro", "parser": "rss_feed"},
        {"name": "CoinDesk Business",     "url": "https://www.coindesk.com/arc/outboundfeeds/rss/category/business/",  "type": "rss", "beat": "bitcoin-macro", "parser": "rss_feed"},
        {"name": "Bitcoin Magazine",      "url": "https://bitcoinmagazine.com/feed",                                   "type": "rss", "beat": "bitcoin-macro", "parser": "rss_feed"},
        {"name": "The Block",             "url": "https://www.theblock.co/rss.xml",                                    "type": "rss", "beat": "bitcoin-macro", "parser": "rss_feed"},
        {"name": "Decrypt Bitcoin",       "url": "https://decrypt.co/feed/bitcoin",                                    "type": "rss", "beat": "bitcoin-macro", "parser": "rss_feed"},
        {"name": "Investing.com Crypto",  "url": "https://www.investing.com/rss/news_301.rss",                         "type": "rss", "beat": "bitcoin-macro", "parser": "rss_feed"},
    ],

    # ---- Quantum: post-quantum research, Bitcoin cryptographic risk ----
    "quantum": [
        {"name": "CoinDesk Tech",         "url": "https://www.coindesk.com/arc/outboundfeeds/rss/category/tech/",      "type": "rss", "beat": "quantum",       "parser": "rss_feed"},
        {"name": "Cointelegraph",         "url": "https://cointelegraph.com/rss",                                      "type": "rss", "beat": "quantum",       "parser": "rss_feed"},
        {"name": "arXiv cs.CR",           "url": "https://rss.arxiv.org/rss/cs.CR",                                   "type": "rss", "beat": "quantum",       "parser": "rss_feed"},
        {"name": "arXiv quant-ph",        "url": "https://rss.arxiv.org/rss/quant-ph",                                "type": "rss", "beat": "quantum",       "parser": "rss_feed"},
        {"name": "TheStreet Crypto",      "url": "https://www.thestreet.com/crypto/rss.xml",                          "type": "rss", "beat": "quantum",       "parser": "rss_feed"},
        {"name": "Quantum Insider",       "url": "https://thequantuminsider.com/feed/",                               "type": "rss", "beat": "quantum",       "parser": "rss_feed"},
    ],

    # ---- Security: custody risk, key theft, wallet vulnerabilities ----
    "security": [
        {"name": "CoinDesk Tech",         "url": "https://www.coindesk.com/arc/outboundfeeds/rss/category/tech/",      "type": "rss", "beat": "security",      "parser": "rss_feed"},
        {"name": "The Block",             "url": "https://www.theblock.co/rss.xml",                                    "type": "rss", "beat": "security",      "parser": "rss_feed"},
        {"name": "Bitcoin Magazine",      "url": "https://bitcoinmagazine.com/feed",                                   "type": "rss", "beat": "security",      "parser": "rss_feed"},
        {"name": "Cointelegraph",         "url": "https://cointelegraph.com/rss",                                      "type": "rss", "beat": "security",      "parser": "rss_feed"},
        {"name": "Krebs on Security",     "url": "https://krebsonsecurity.com/feed/",                                  "type": "rss", "beat": "security",      "parser": "rss_feed"},
        {"name": "Decrypt",               "url": "https://decrypt.co/feed",                                           "type": "rss", "beat": "security",      "parser": "rss_feed"},
    ],

    # ---- Governance: Bitcoin BIPs, Stacks SIPs, PoX cycle data ----
    "governance": [
        {"name": "Cointelegraph",         "url": "https://cointelegraph.com/rss",                                      "type": "rss",    "beat": "governance",    "parser": "rss_feed"},
        {"name": "Bitcoin Magazine",      "url": "https://bitcoinmagazine.com/feed",                                   "type": "rss",    "beat": "governance",    "parser": "rss_feed"},
        {"name": "CoinDesk Policy",       "url": "https://www.coindesk.com/arc/outboundfeeds/rss/category/policy/",   "type": "rss",    "beat": "governance",    "parser": "rss_feed"},
        {"name": "Hiro PoX Info",         "url": "https://api.hiro.so/v2/pox",                                        "type": "on-chain","beat": "governance",   "parser": "hiro_pox"},
        {"name": "Stacks SIPs GitHub",    "url": "https://api.github.com/repos/stacksgov/sips/issues?state=open&per_page=5", "type": "github", "beat": "governance", "parser": "github_prs"},
        {"name": "Bitcoin BIPs GitHub",   "url": "https://api.github.com/repos/bitcoin/bips/pulls?state=open&per_page=5",   "type": "github", "beat": "governance", "parser": "github_prs"},
    ],

    # ---- Infrastructure: mempool, Stacks blocks, aibtc ecosystem ----
    "btc_infrastructure": [
        {"name": "Mempool Fee Rates",     "url": "https://mempool.space/api/v1/fees/recommended",                     "type": "on-chain", "beat": "infrastructure", "parser": "mempool_fees"},
        {"name": "Mempool Recent Blocks", "url": "https://mempool.space/api/v1/blocks",                               "type": "on-chain", "beat": "infrastructure", "parser": "mempool_blocks"},
        {"name": "Hiro Latest Block",     "url": "https://api.hiro.so/extended/v1/block?limit=1",                    "type": "on-chain", "beat": "infrastructure", "parser": "hiro_block"},
        {"name": "AIBTC MCP Releases",    "url": "https://api.github.com/repos/aibtcdev/aibtc-mcp-server/releases",  "type": "github",   "beat": "infrastructure", "parser": "github_releases"},
        {"name": "AIBTC x402 Relay",      "url": "https://api.github.com/repos/aibtcdev/x402-sponsor-relay/releases","type": "github",   "beat": "infrastructure", "parser": "github_releases"},
        {"name": "Bitcoin Core Releases", "url": "https://api.github.com/repos/bitcoin/bitcoin/releases?per_page=5", "type": "github",   "beat": "infrastructure", "parser": "github_releases"},
    ],

    # ---- Agent Economy: AIBTC network metrics, autonomous agent activity ----
    "agent_economy": [
        {"name": "AIBTC News Report",     "url": "https://aibtc.news/api/report",                                     "type": "ecosystem", "beat": "agent-economy", "parser": "aibtc_report"},
        {"name": "Cointelegraph",         "url": "https://cointelegraph.com/rss",                                     "type": "rss",       "beat": "agent-economy", "parser": "rss_feed"},
        {"name": "Decrypt",               "url": "https://decrypt.co/feed",                                          "type": "rss",       "beat": "agent-economy", "parser": "rss_feed"},
        {"name": "Bitcoin Magazine",      "url": "https://bitcoinmagazine.com/feed",                                  "type": "rss",       "beat": "agent-economy", "parser": "rss_feed"},
        {"name": "Bitflow PRs",           "url": "https://api.github.com/repos/BitflowFinance/bff-skills/pulls?state=open&per_page=10", "type": "github", "beat": "agent-economy", "parser": "github_prs"},
    ],
}

# 4 slots per day — beat rotation.
# Slots 3 & 4 alternate by day-of-month parity (computed at runtime in main()).
SLOT_SOURCE_MAP = {
    1: ["btc_macro"],           # 06:00 UTC — bitcoin-macro
    2: ["quantum"],             # 12:00 UTC — quantum
    3: None,                    # 18:00 UTC — security (even day) OR governance (odd day)
    4: None,                    # 00:00 UTC — infrastructure (even day) OR agent-economy (odd day)
}


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def fetch_json(url: str, timeout: int = 12):
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "SereneSpring/1.0 (AIBTC News Agent)",
                "Accept":     "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"  [warn] fetch failed for {url}: {e}")
        return None


def parse_rss_feed(data, source: dict) -> list:
    try:
        feed = feedparser.parse(source["url"])
    except Exception as e:
        print(f"  [warn] feedparser failed for {source['url']}: {e}")
        return []

    articles = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=72)

    for entry in feed.entries[:15]:
        title   = entry.get("title", "").strip()
        link    = entry.get("link", "")
        summary = entry.get("summary", "") or entry.get("description", "")
        summary = re.sub(r"<[^>]+>", " ", summary).strip()
        summary = " ".join(summary.split())[:500]

        pub_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        if pub_parsed:
            from datetime import datetime as dt
            pub = dt(*pub_parsed[:6], tzinfo=timezone.utc)
            pub_str = pub.isoformat()
        else:
            pub = datetime.now(timezone.utc)
            pub_str = pub.isoformat()

        if pub < cutoff:
            continue
        if not title or not link:
            continue

        articles.append({
            "title":   title,
            "url":     link,
            "summary": summary[:500],
            "source":  source["name"],
            "type":    source["type"],
            "published": pub_str,
        })

    return articles


def parse_mempool_fees(data: dict, source: dict) -> list:
    if not data or not isinstance(data, dict):
        return []
    fastest   = data.get("fastestFee", "?")
    half_hour = data.get("halfHourFee", "?")
    economy   = data.get("economyFee", "?")
    return [{
        "title":   f"BTC mempool: fastest {fastest} sat/vB, 30-min {half_hour} sat/vB, economy {economy} sat/vB",
        "url":     "https://mempool.space/api/v1/fees/recommended",
        "summary": f"Bitcoin fee market snapshot. Fastest: {fastest} sat/vB. Half-hour: {half_hour} sat/vB. Economy: {economy} sat/vB.",
        "source":  source["name"],
        "type":    source["type"],
        "published": datetime.now(timezone.utc).isoformat(),
        "raw":     data,
    }]


def parse_mempool_blocks(data: list, source: dict) -> list:
    if not data or not isinstance(data, list):
        return []
    articles = []
    for block in data[:3]:
        height   = block.get("height", "?")
        tx_count = block.get("tx_count", "?")
        size_kb  = round(block.get("size", 0) / 1024, 1)
        ts       = block.get("timestamp")
        pub = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else datetime.now(timezone.utc).isoformat()
        articles.append({
            "title":   f"Bitcoin block {height}: {tx_count} transactions, {size_kb} KB",
            "url":     f"https://mempool.space/block/{block.get('id', '')}",
            "summary": f"Block {height} confirmed with {tx_count} transactions ({size_kb} KB). Mined at {pub[:16]} UTC.",
            "source":  source["name"],
            "type":    source["type"],
            "published": pub,
        })
    return articles


def parse_hiro_block(data: dict, source: dict) -> list:
    if not data or not isinstance(data, dict):
        return []
    results = data.get("results", [])
    if not results:
        return []
    block       = results[0]
    height      = block.get("height", "?")
    burn_height = block.get("burn_block_height", "?")
    tx_list     = block.get("txs", [])
    tx_n        = len(tx_list) if isinstance(tx_list, list) else block.get("tx_count", "?")
    ts          = block.get("burn_block_time")
    pub = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else datetime.now(timezone.utc).isoformat()
    return [{
        "title":   f"Stacks block {height} anchored to Bitcoin block {burn_height}: {tx_n} transactions",
        "url":     f"https://explorer.hiro.so/block/{block.get('hash', '')}",
        "summary": f"Latest Stacks block #{height} anchored to Bitcoin block {burn_height}. {tx_n} transactions. {pub[:16]} UTC.",
        "source":  source["name"],
        "type":    source["type"],
        "published": pub,
    }]


def parse_hiro_pox(data: dict, source: dict) -> list:
    if not data or not isinstance(data, dict):
        return []
    cycle        = data.get("current_cycle", {})
    next_cycle   = data.get("next_cycle", {})
    min_stx      = data.get("min_amount_ustx", 0)
    min_stx_val  = min_stx // 1_000_000 if min_stx else "?"
    cycle_id     = cycle.get("id", "?")
    prepare_in   = next_cycle.get("blocks_until_prepare_phase", "?")
    return [{
        "title":   f"PoX Cycle {cycle_id}: minimum stacking threshold {min_stx_val:,} STX, prepare phase in ~{prepare_in} blocks",
        "url":     "https://api.hiro.so/v2/pox",
        "summary": (
            f"PoX Cycle {cycle_id} is active. Minimum stacking threshold: {min_stx_val:,} STX. "
            f"Prepare phase begins in approximately {prepare_in} blocks."
        ),
        "source":  source["name"],
        "type":    source["type"],
        "published": datetime.now(timezone.utc).isoformat(),
        "raw":     data,
    }]


def parse_github_releases(data: list, source: dict) -> list:
    if not data or not isinstance(data, list):
        return []
    articles = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    for release in data[:5]:
        pub_str = release.get("published_at") or release.get("created_at", "")
        try:
            pub = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
        except Exception:
            pub = datetime.now(timezone.utc)
        if pub < cutoff:
            continue
        tag  = release.get("tag_name", "?")
        name = release.get("name") or tag
        body = (release.get("body") or "")[:300].replace("\r\n", " ").replace("\n", " ")
        articles.append({
            "title":   f"{source['name']} released {tag}: {name}",
            "url":     release.get("html_url", ""),
            "summary": f"{source['name']} published release {tag} on {pub_str[:10]}. {body[:200]}",
            "source":  source["name"],
            "type":    source["type"],
            "published": pub.isoformat(),
        })
    return articles


def parse_github_prs(data: list, source: dict) -> list:
    if not data or not isinstance(data, list):
        return []
    articles = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    for pr in data[:5]:
        pub_str = pr.get("created_at", "") or pr.get("updated_at", "")
        try:
            pub = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
        except Exception:
            pub = datetime.now(timezone.utc)
        if pub < cutoff:
            continue
        title  = pr.get("title", "")
        number = pr.get("number", "?")
        user   = pr.get("user", {}).get("login", "unknown")
        articles.append({
            "title":   f"{source['name']} #{number}: {title}",
            "url":     pr.get("html_url", ""),
            "summary": f"PR #{number} on {source['name']} opened by {user} on {pub_str[:10]}. {title}",
            "source":  source["name"],
            "type":    source["type"],
            "published": pub.isoformat(),
        })
    return articles


def parse_aibtc_report(data, source: dict) -> list:
    if not data:
        return []
    items = data if isinstance(data, list) else [data]
    articles = []
    for item in items[:3]:
        title   = item.get("title") or item.get("headline") or "AIBTC Report"
        url     = item.get("url") or item.get("sourceUrl") or "https://aibtc.news/api/report"
        summary = item.get("summary") or item.get("content") or str(item)[:300]
        articles.append({
            "title":   title,
            "url":     url,
            "summary": summary[:400],
            "source":  source["name"],
            "type":    source["type"],
            "published": datetime.now(timezone.utc).isoformat(),
        })
    return articles


PARSERS = {
    "rss_feed":        parse_rss_feed,
    "mempool_fees":    parse_mempool_fees,
    "mempool_blocks":  parse_mempool_blocks,
    "hiro_block":      parse_hiro_block,
    "hiro_pox":        parse_hiro_pox,
    "github_releases": parse_github_releases,
    "github_prs":      parse_github_prs,
    "aibtc_report":    parse_aibtc_report,
}


def fetch_source(source: dict) -> list:
    print(f"  Fetching: {source['name']} ({source['type']}) ...")
    if source["type"] == "rss":
        return parse_rss_feed(None, source)

    data = fetch_json(source["url"])
    if data is None:
        return []
    parser_fn = PARSERS.get(source["parser"])
    if not parser_fn:
        print(f"  [warn] no parser for {source['parser']}")
        return []
    return parser_fn(data, source)


def resolve_slot_buckets(slot: int) -> list:
    """
    Return the source bucket names for a given slot.
    Slots 3 & 4 alternate by day-of-month parity.
    """
    day = datetime.now(timezone.utc).day
    even_day = (day % 2 == 0)

    if slot == 1:
        return ["btc_macro"]
    if slot == 2:
        return ["quantum"]
    if slot == 3:
        return ["security"] if even_day else ["governance"]
    if slot == 4:
        return ["btc_infrastructure"] if even_day else ["agent_economy"]
    return ["btc_macro"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot",   type=int, required=True, help="1–4")
    parser.add_argument("--beat",   type=str, required=True)
    parser.add_argument("--output", type=str, default="raw_sources.json")
    args = parser.parse_args()

    buckets = resolve_slot_buckets(args.slot)
    print(f"Slot {args.slot} | Beat: {args.beat}")
    print(f"Pulling from buckets: {', '.join(buckets)}")

    all_articles = []
    seen_urls    = set()

    for bucket in buckets:
        for source in SOURCES.get(bucket, []):
            articles = fetch_source(source)
            for a in articles:
                if a["url"] not in seen_urls:
                    seen_urls.add(a["url"])
                    all_articles.append(a)
            time.sleep(0.3)

    all_articles.sort(key=lambda a: a.get("published", ""), reverse=True)

    output = {
        "slot":          args.slot,
        "beat":          args.beat,
        "scraped_at":    datetime.now(timezone.utc).isoformat(),
        "total_fetched": len(all_articles),
        "articles":      all_articles,
    }

    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nDone. {len(all_articles)} items fetched → {args.output}")
    for a in all_articles[:8]:
        print(f"  [{a['type']}] {a['title'][:90]}")


if __name__ == "__main__":
    main()
