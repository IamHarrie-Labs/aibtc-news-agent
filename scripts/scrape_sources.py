#!/usr/bin/env python3
"""
scrape_sources.py

Pulls fresh content from leading web3 and AI media outlets.
Maps each slot to the source types most likely to yield
approved signals on that beat.

Sources are fetched via RSS (no API keys needed for most).
Output is a JSON file consumed by run_agent.py.
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
    print("feedparser not installed. Run: pip install feedparser")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Source registry
# All RSS — no auth required. Add/remove freely.
# ---------------------------------------------------------------------------

SOURCES = {
    "research": [
        # arXiv CS.CR (crypto/security), CS.AI, and q-fin
        {"name": "arXiv cs.CR", "url": "https://rss.arxiv.org/rss/cs.CR", "type": "arxiv"},
        {"name": "arXiv cs.AI",  "url": "https://rss.arxiv.org/rss/cs.AI",  "type": "arxiv"},
        {"name": "arXiv q-fin",  "url": "https://rss.arxiv.org/rss/q-fin",  "type": "arxiv"},
        {"name": "SSRN crypto",  "url": "https://papers.ssrn.com/rss/hrd_sub.cfm?per=9&ncd=crypto", "type": "paper"},
    ],
    "web3_protocol": [
        {"name": "The Block",        "url": "https://www.theblock.co/rss.xml",                   "type": "media"},
        {"name": "Decrypt",          "url": "https://decrypt.co/feed",                           "type": "media"},
        {"name": "CoinDesk",         "url": "https://www.coindesk.com/arc/outboundfeeds/rss/",   "type": "media"},
        {"name": "Blockworks",       "url": "https://blockworks.co/feed",                        "type": "media"},
        {"name": "Bitcoin Magazine",  "url": "https://bitcoinmagazine.com/.rss/full/",           "type": "media"},
        {"name": "Messari Research", "url": "https://messari.io/rss",                            "type": "research"},
        {"name": "DeFi Llama blog",  "url": "https://defillama.com/blog/rss.xml",                "type": "on-chain"},
    ],
    "ai_general": [
        {"name": "Hacker News AI",   "url": "https://hnrss.org/newest?q=AI+agent+bitcoin",      "type": "community"},
        {"name": "VentureBeat AI",   "url": "https://venturebeat.com/category/ai/feed/",         "type": "media"},
        {"name": "MIT Tech Review",  "url": "https://www.technologyreview.com/feed/",            "type": "media"},
        {"name": "Import AI",        "url": "https://importai.substack.com/feed",                "type": "newsletter"},
        {"name": "Last Week in AI",  "url": "https://lastweekin.ai/feed",                        "type": "newsletter"},
    ],
    "market_onchain": [
        {"name": "Glassnode blog",   "url": "https://insights.glassnode.com/rss/",              "type": "on-chain"},
        {"name": "Dune blog",        "url": "https://dune.com/blog/rss.xml",                    "type": "on-chain"},
        {"name": "Token Terminal",   "url": "https://tokenterminal.com/blog/rss.xml",           "type": "on-chain"},
        {"name": "Kaito",            "url": "https://blog.kaito.ai/rss",                        "type": "on-chain"},
    ],
    "deals_ecosystem": [
        {"name": "CrunchBase crypto", "url": "https://news.crunchbase.com/tag/cryptocurrency/feed/", "type": "deals"},
        {"name": "The Block deals",   "url": "https://www.theblock.co/rss.xml",                      "type": "deals"},
        {"name": "Blockworks deals",  "url": "https://blockworks.co/feed",                           "type": "deals"},
    ],
}

# Slot → which source buckets to pull from (12 slots, every 2 hours)
SLOT_SOURCE_MAP = {
    1:  ["research"],                        # 00:00 UTC — midnight sweep
    2:  ["web3_protocol", "ai_general"],     # 02:00 UTC — early research
    3:  ["market_onchain"],                  # 04:00 UTC — pre-market
    4:  ["research"],                        # 06:00 UTC — arxiv + papers
    5:  ["web3_protocol", "ai_general"],     # 08:00 UTC — beat sweep
    6:  ["market_onchain"],                  # 10:00 UTC — on-chain data
    7:  ["web3_protocol"],                   # 12:00 UTC — midday beat
    8:  ["market_onchain", "research"],      # 14:00 UTC — market update
    9:  ["web3_protocol", "ai_general"],     # 16:00 UTC — US session scout
    10: ["web3_protocol"],                   # 18:00 UTC — beat sweep
    11: ["deals_ecosystem", "web3_protocol"],# 20:00 UTC — deal flow
    12: ["research", "ai_general"],          # 22:00 UTC — daily wrap
}

# Beat → keywords to filter articles for relevance
BEAT_KEYWORDS = {
    "DeFi and Protocol Updates": [
        "defi", "protocol", "liquidity", "tvl", "amm", "yield", "vault",
        "lending", "dex", "swap", "uniswap", "aave", "compound", "staking",
    ],
    "Smart Contract Security": [
        "exploit", "vulnerability", "audit", "hack", "reentrancy", "bug",
        "security", "smart contract", "solidity", "clarity", "erc",
    ],
    "AI Agent Economy": [
        "ai agent", "autonomous agent", "agentic", "llm", "claude", "gpt",
        "mcp", "x402", "agent economy", "ai wallet", "machine payment",
    ],
    "Bitcoin Infrastructure": [
        "bitcoin", "btc", "lightning", "taproot", "ordinals", "runes",
        "sbtc", "stacks", "layer 2", "l2", "rgb", "ark",
    ],
    "Bitcoin Macro": [
        "bitcoin", "macro", "etf", "institutional", "federal reserve",
        "inflation", "treasury", "sovereign", "adoption", "halving",
    ],
}


def fetch_feed(source: dict, max_age_hours: int = 48) -> list[dict]:
    """Fetch and filter a single RSS feed. Returns list of article dicts."""
    try:
        feed = feedparser.parse(source["url"])
    except Exception as e:
        print(f"  [warn] failed to parse {source['name']}: {e}")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    articles = []

    for entry in feed.entries[:20]:  # cap at 20 per source
        # Parse published date
        pub = None
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            pub = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
            pub = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)

        if pub and pub < cutoff:
            continue  # too old

        articles.append({
            "title":   getattr(entry, "title",   "").strip(),
            "url":     getattr(entry, "link",    "").strip(),
            "summary": getattr(entry, "summary", "")[:500].strip(),
            "source":  source["name"],
            "type":    source["type"],
            "published": pub.isoformat() if pub else None,
        })

    return articles


def score_relevance(article: dict, beat: str) -> int:
    """Return keyword match count for a given beat."""
    keywords = BEAT_KEYWORDS.get(beat, [])
    text = (article["title"] + " " + article["summary"]).lower()
    return sum(1 for kw in keywords if kw in text)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot",   type=int, required=True, help="1–6")
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
            print(f"  Fetching: {source['name']} ...")
            articles = fetch_feed(source)
            for a in articles:
                if a["url"] not in seen_urls:
                    seen_urls.add(a["url"])
                    a["relevance_score"] = score_relevance(a, args.beat)
                    all_articles.append(a)
            time.sleep(0.5)  # be polite

    # Sort by relevance desc, then recency
    all_articles.sort(key=lambda x: (-x["relevance_score"], x["published"] or ""))

    # Keep top 20 most relevant for the agent
    top = all_articles[:20]

    output = {
        "slot": args.slot,
        "beat": args.beat,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "total_fetched": len(all_articles),
        "articles": top,
    }

    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nDone. {len(all_articles)} articles fetched, top {len(top)} written to {args.output}.")
    print("Top 5 by relevance:")
    for a in top[:5]:
        print(f"  [{a['relevance_score']}] {a['title'][:80]} — {a['source']}")


if __name__ == "__main__":
    main()
