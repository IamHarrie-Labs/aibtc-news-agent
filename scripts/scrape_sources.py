#!/usr/bin/env python3
"""
scrape_sources.py — Fetch raw signal sources for Serene Spring based on slot + beat.

Slots:
  1 → arXiv AI/Bitcoin/agent research
  2 → RSS feeds (beat primary)
  3 → Stacks/BTC market data (Tenero + Hiro)
  4 → RSS feeds (beat primary, second pass)
  5 → AIBTC network activity (scout)
  6 → Deal flow signals (bounties, ordinals, x402)
"""

import argparse
import json
import time
import sys
from datetime import datetime, timezone

try:
    import requests
    import feedparser
except ImportError:
    print("Missing deps. Run: pip install requests feedparser", file=sys.stderr)
    sys.exit(1)

HEADERS = {"User-Agent": "SereneSpring/1.0 (aibtc.news correspondent)"}

# ── RSS feeds by beat ───────────────────────────────────────────────────────

FEEDS = {
    "bitcoin-defi-stacks": [
        "https://stacks.org/feed",
        "https://blog.bitflow.finance/rss",
        "https://www.hiro.so/blog/rss.xml",
        "https://learnmeabitcoin.com/feed",
    ],
    "ordinals-runes": [
        "https://ordinalhub.com/feed",
        "https://runealpha.xyz/rss",
        "https://www.ord.io/feed",
    ],
    "ai-agent-economy": [
        "https://aibtc.com/rss",
        "https://venturebeat.com/category/ai/feed/",
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
    ],
    # fallback — all three combined
    "all": [
        "https://stacks.org/feed",
        "https://www.hiro.so/blog/rss.xml",
        "https://aibtc.com/rss",
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://venturebeat.com/category/ai/feed/",
    ],
}


def fetch_rss(beat: str, max_items: int = 10) -> list[dict]:
    key = beat.lower().replace(" ", "-").replace("&", "").replace("  ", "-")
    urls = FEEDS.get(key, FEEDS["all"])
    items = []
    for url in urls:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:max_items]:
                items.append({
                    "source": feed.feed.get("title", url),
                    "title": entry.get("title", ""),
                    "summary": entry.get("summary", "")[:500],
                    "link": entry.get("link", ""),
                    "published": entry.get("published", ""),
                })
        except Exception as e:
            print(f"  [warn] RSS fetch failed for {url}: {e}", file=sys.stderr)
    return items[:20]


def fetch_arxiv(max_results: int = 8) -> list[dict]:
    """Fetch recent arXiv papers on LLMs, autonomous agents, Bitcoin."""
    query = "ti:(bitcoin+OR+stacks+OR+autonomous+agents+OR+LLM+OR+AI+agents)"
    url = (
        f"http://export.arxiv.org/api/query"
        f"?search_query={query}&start=0&max_results={max_results}"
        f"&sortBy=submittedDate&sortOrder=descending"
    )
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        feed = feedparser.parse(resp.text)
        return [
            {
                "source": "arXiv",
                "title": e.get("title", "").replace("\n", " "),
                "summary": e.get("summary", "")[:600].replace("\n", " "),
                "link": e.get("link", ""),
                "published": e.get("published", ""),
                "authors": [a.get("name", "") for a in e.get("authors", [])[:3]],
            }
            for e in feed.entries
        ]
    except Exception as e:
        print(f"  [warn] arXiv fetch failed: {e}", file=sys.stderr)
        return []


def fetch_stacks_market() -> dict:
    """Fetch current Stacks network stats from Hiro API."""
    data = {}
    endpoints = {
        "network_status": "https://api.hiro.so/extended/v1/status",
        "stx_supply": "https://api.hiro.so/extended/v1/stx_supply",
        "recent_blocks": "https://api.hiro.so/extended/v1/block?limit=5",
        "mempool_stats": "https://api.hiro.so/extended/v1/tx/mempool/stats",
    }
    for key, url in endpoints.items():
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code == 200:
                data[key] = resp.json()
        except Exception as e:
            print(f"  [warn] Hiro API {key} failed: {e}", file=sys.stderr)
    return data


def fetch_aibtc_activity() -> list[dict]:
    """Fetch recent AIBTC network activity — agents, signals, bounties."""
    items = []
    endpoints = [
        ("https://aibtc.com/api/agents/recent", "recent_agents"),
        ("https://aibtc.com/api/signals/recent", "recent_signals"),
        ("https://aibtc.com/api/bounties/open", "open_bounties"),
    ]
    for url, label in endpoints:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code == 200:
                payload = resp.json()
                if isinstance(payload, list):
                    items.extend([{**item, "_source": label} for item in payload[:5]])
                elif isinstance(payload, dict):
                    items.append({**payload, "_source": label})
        except Exception as e:
            print(f"  [warn] AIBTC API {label} failed: {e}", file=sys.stderr)
    return items


def fetch_deal_flow() -> list[dict]:
    """Fetch ordinals trades, x402 payments, bounty completions."""
    items = []
    endpoints = [
        ("https://aibtc.com/api/deals/recent", "deals"),
        ("https://aibtc.com/api/x402/recent", "x402_payments"),
        ("https://aibtc.com/api/bounties/completed", "completed_bounties"),
        ("https://ordinalhub.com/api/recent-trades", "ordinal_trades"),
    ]
    for url, label in endpoints:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code == 200:
                payload = resp.json()
                if isinstance(payload, list):
                    items.extend([{**item, "_source": label} for item in payload[:5]])
        except Exception as e:
            print(f"  [warn] Deal flow {label} failed: {e}", file=sys.stderr)
    return items


# ── Slot dispatcher ──────────────────────────────────────────────────────────

def scrape(slot: int, beat: str) -> dict:
    ts = datetime.now(timezone.utc).isoformat()
    print(f"[scrape] slot={slot} beat='{beat}' at {ts}")

    if slot == 1:
        print("  → arXiv research fetch")
        sources = fetch_arxiv(max_results=10)
        return {"slot": slot, "type": "arxiv", "beat": beat, "timestamp": ts, "items": sources}

    elif slot in (2, 4):
        print(f"  → RSS beat feeds (slot {slot})")
        sources = fetch_rss(beat, max_items=8)
        return {"slot": slot, "type": "rss_beat", "beat": beat, "timestamp": ts, "items": sources}

    elif slot == 3:
        print("  → Stacks market data (Tenero + Hiro)")
        market = fetch_stacks_market()
        rss = fetch_rss(beat, max_items=5)
        return {"slot": slot, "type": "market", "beat": beat, "timestamp": ts,
                "market": market, "items": rss}

    elif slot == 5:
        print("  → AIBTC network scout")
        sources = fetch_aibtc_activity()
        rss = fetch_rss("ai-agent-economy", max_items=5)
        return {"slot": slot, "type": "scout", "beat": beat, "timestamp": ts,
                "network": sources, "items": rss}

    elif slot == 6:
        print("  → Deal flow signals")
        sources = fetch_deal_flow()
        return {"slot": slot, "type": "deal_flow", "beat": beat, "timestamp": ts, "items": sources}

    else:
        return {"slot": slot, "type": "unknown", "beat": beat, "timestamp": ts, "items": []}


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Scrape sources for AIBTC News Agent")
    parser.add_argument("--slot", type=int, required=True, help="Signal slot 1-6")
    parser.add_argument("--beat", type=str, default="all", help="Agent beat/topic")
    parser.add_argument("--output", type=str, default="raw_sources.json", help="Output JSON path")
    args = parser.parse_args()

    data = scrape(args.slot, args.beat)

    with open(args.output, "w") as f:
        json.dump(data, f, indent=2, default=str)

    item_count = len(data.get("items", [])) + len(data.get("network", []))
    print(f"[scrape] Done — {item_count} items written to {args.output}")


if __name__ == "__main__":
    main()
