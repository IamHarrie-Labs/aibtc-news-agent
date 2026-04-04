#!/usr/bin/env python3
"""
run_agent.py — Serene Spring signal agent.

Runs Claude with the appropriate skill prompt for the given slot,
processes scraped sources, generates a signal, submits a heartbeat,
and appends to the news log.
"""

import argparse
import json
import os
import sys
import subprocess
import hashlib
import hmac
from datetime import datetime, timezone
from pathlib import Path

try:
    import anthropic
    import requests
except ImportError:
    print("Missing deps. Run: pip install anthropic requests", file=sys.stderr)
    sys.exit(1)

AIBTC_API = "https://aibtc.com/api"
HEADERS = {"Content-Type": "application/json", "User-Agent": "SereneSpring/1.0"}

# ── Skill prompts by slot ────────────────────────────────────────────────────

SLOT_PROMPTS = {
    1: """You are Serene Spring, a Genesis-level AIBTC correspondent. Your beat covers
Bitcoin DeFi, Stacks ecosystem, Ordinals/Runes, and the AI agent economy.

You have just received a batch of arXiv research papers. Your job:
1. SCAN each paper for signals relevant to your beat (Bitcoin, Stacks, autonomous agents, DeFi)
2. SCORE each paper 1-10 for beat relevance and novelty
3. SELECT the top 2-3 papers
4. WRITE a short research digest (200-300 words) in aibtc.news correspondent voice:
   - Lead with the most actionable insight
   - Include paper titles, authors, and arXiv links
   - Note implications for the Bitcoin/Stacks/agent ecosystem
   - Keep it signal-dense, no fluff
5. OUTPUT valid JSON only:
{
  "slot": 1,
  "type": "arxiv_digest",
  "headline": "...",
  "body": "...",
  "sources": [{"title": "...", "link": "...", "score": 0}],
  "tags": ["research", "arxiv", ...]
}""",

    2: """You are Serene Spring, a Genesis-level AIBTC correspondent covering
Bitcoin DeFi, Stacks, Ordinals/Runes, and the AI agent economy.

You have received RSS feed items from your beat sources. Your job:
1. IDENTIFY the top signal from today's feeds — one clear, verifiable event
2. FACT-CHECK: cross-reference claims across multiple sources in the feed
3. WRITE a signal post (150-250 words) in aibtc.news style:
   - Hard news lede: what happened, who, when, what it means
   - Include on-chain context where possible
   - Cite your sources
4. OUTPUT valid JSON only:
{
  "slot": 2,
  "type": "beat_signal",
  "headline": "...",
  "body": "...",
  "sources": [{"title": "...", "link": "..."}],
  "tags": [...]
}""",

    3: """You are Serene Spring, a Genesis-level AIBTC correspondent.

You have current Stacks network data + RSS feeds. Your job:
1. ANALYZE the market data: STX supply, block activity, mempool, recent transactions
2. IDENTIFY any notable on-chain trends or anomalies
3. CROSS-REFERENCE with RSS news items for context
4. WRITE a market brief (150-200 words):
   - Lead with the most notable on-chain fact
   - Include specific numbers (block height, tx count, fees, supply)
   - Connect to broader narrative if RSS items support it
5. OUTPUT valid JSON only:
{
  "slot": 3,
  "type": "market_brief",
  "headline": "...",
  "body": "...",
  "data_points": {"stx_price": null, "block_height": null, "mempool_size": null},
  "tags": ["market", "stacks", ...]
}""",

    4: """You are Serene Spring, a Genesis-level AIBTC correspondent.

Second beat pass of the day — RSS feeds from your primary beat.
Your job:
1. LOOK for developments since the morning pass (new announcements, responses, updates)
2. WRITE a follow-up signal or new story if warranted (100-200 words)
3. If nothing new is significant, write a SHORT note explaining why (50 words max)
4. OUTPUT valid JSON only:
{
  "slot": 4,
  "type": "beat_followup",
  "headline": "...",
  "body": "...",
  "sources": [{"title": "...", "link": "..."}],
  "tags": [...],
  "has_signal": true
}""",

    5: """You are Serene Spring, a Genesis-level AIBTC correspondent.
Your special role in slot 5 is scout — find new agents, new signals, emerging patterns.

You have AIBTC network activity data + RSS from the agent economy. Your job:
1. IDENTIFY newly registered agents, unusual activity, or emerging collaborations
2. SPOT any agents approaching level-up thresholds or completing bounties
3. WRITE a scout report (150-200 words):
   - Lead with the most interesting new agent or activity
   - Note agent names, levels, beats, owner handles
   - Flag any unusual x402 payment flows or inbox spikes
4. OUTPUT valid JSON only:
{
  "slot": 5,
  "type": "scout_report",
  "headline": "...",
  "body": "...",
  "agents_spotted": [{"name": "...", "level": null, "beat": "..."}],
  "tags": ["scout", "agents", ...]
}""",

    6: """You are Serene Spring, a Genesis-level AIBTC correspondent.
Slot 6 is deal flow — the final signal of the day.

You have deal flow data: ordinals trades, x402 payments, completed bounties, contract deployments.
Your job:
1. FIND the day's most significant deal, trade, or on-chain event
2. VERIFY: check amounts, addresses, and timestamps for plausibility
3. WRITE a deal flow signal (150-200 words):
   - Lead with the deal (what moved, how much, between whom)
   - Include tx/inscription IDs where available
   - Note implications: is this a trend? A one-off? A signal of demand?
4. OUTPUT valid JSON only:
{
  "slot": 6,
  "type": "deal_flow",
  "headline": "...",
  "body": "...",
  "deal": {"type": "...", "amount": null, "asset": "...", "tx_id": "..."},
  "tags": ["deals", "ordinals", ...]
}""",
}


# ── Heartbeat ────────────────────────────────────────────────────────────────

def send_heartbeat(btc_address: str) -> bool:
    """Submit a heartbeat to the AIBTC network."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    message = f"AIBTC Check-In | {ts}"

    try:
        # Use @aibtc/mcp-server CLI to sign the message
        result = subprocess.run(
            ["npx", "--yes", "@aibtc/mcp-server", "sign", message],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "WALLET_PASSWORD": os.environ.get("WALLET_PASSWORD", "")}
        )
        signature = result.stdout.strip()
        if not signature:
            print(f"  [heartbeat] Sign failed: {result.stderr}", file=sys.stderr)
            return False

        payload = {
            "btcAddress": btc_address,
            "message": message,
            "signature": signature,
        }
        resp = requests.post(f"{AIBTC_API}/heartbeat", json=payload, headers=HEADERS, timeout=10)
        if resp.status_code in (200, 201):
            print(f"  [heartbeat] ✓ Submitted at {ts}")
            return True
        else:
            print(f"  [heartbeat] Failed {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
            return False
    except Exception as e:
        print(f"  [heartbeat] Error: {e}", file=sys.stderr)
        return False


# ── Signal submission ────────────────────────────────────────────────────────

def submit_signal(signal: dict, btc_address: str) -> bool:
    """Submit the generated signal to aibtc.news."""
    payload = {
        "btcAddress": btc_address,
        "slot": signal.get("slot"),
        "type": signal.get("type"),
        "headline": signal.get("headline", ""),
        "body": signal.get("body", ""),
        "tags": signal.get("tags", []),
        "sources": signal.get("sources", []),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        resp = requests.post(f"{AIBTC_API}/signals", json=payload, headers=HEADERS, timeout=15)
        if resp.status_code in (200, 201):
            print(f"  [signal] ✓ Submitted: {signal.get('headline', '')[:60]}")
            return True
        else:
            print(f"  [signal] Submit failed {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
            return False
    except Exception as e:
        print(f"  [signal] Submit error: {e}", file=sys.stderr)
        return False


# ── Log ──────────────────────────────────────────────────────────────────────

def append_log(log_path: str, slot: int, signal: dict, heartbeat_ok: bool, submit_ok: bool):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    headline = signal.get("headline", "(no headline)")
    signal_type = signal.get("type", "unknown")
    tags = ", ".join(signal.get("tags", []))
    body_preview = signal.get("body", "")[:300].replace("\n", " ")

    entry = f"""
---

## Slot {slot} — {ts}

**Type:** {signal_type}
**Headline:** {headline}
**Tags:** {tags}
**Heartbeat:** {"✓" if heartbeat_ok else "✗"}
**Signal submitted:** {"✓" if submit_ok else "✗ (logged only)"}

{body_preview}{"..." if len(signal.get("body","")) > 300 else ""}
"""
    with open(log_path, "a") as f:
        f.write(entry)
    print(f"  [log] Appended to {log_path}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Run Serene Spring signal agent")
    parser.add_argument("--slot", type=int, required=True)
    parser.add_argument("--sources", type=str, required=True, help="Path to raw_sources.json")
    parser.add_argument("--beat", type=str, default="all")
    parser.add_argument("--primary-skill", type=str, default="")
    parser.add_argument("--btc-address", type=str, default="")
    parser.add_argument("--log", type=str, default="news-log.md")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("[agent] ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    # Load sources
    with open(args.sources) as f:
        sources = json.load(f)

    # Build prompt
    system_prompt = SLOT_PROMPTS.get(args.slot, SLOT_PROMPTS[2])
    if args.primary_skill:
        system_prompt += f"\n\nYour primary skill for today is: {args.primary_skill}"
    system_prompt += f"\n\nYour beat: {args.beat}"

    user_message = f"Here are today's sources for slot {args.slot}:\n\n{json.dumps(sources, indent=2, default=str)[:8000]}"

    print(f"[agent] Running slot {args.slot} ({sources.get('type','?')}) — beat: {args.beat}")

    # Call Claude
    client = anthropic.Anthropic(api_key=api_key)
    try:
        message = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}]
        )
        raw_output = message.content[0].text.strip()
        print(f"  [agent] Claude responded ({len(raw_output)} chars)")
    except Exception as e:
        print(f"  [agent] Claude API error: {e}", file=sys.stderr)
        sys.exit(1)

    # Parse JSON signal
    signal = {}
    try:
        # Strip markdown code fences if present
        clean = raw_output
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1]
        if clean.endswith("```"):
            clean = clean.rsplit("```", 1)[0]
        signal = json.loads(clean.strip())
        print(f"  [agent] Signal parsed: {signal.get('headline','')[:60]}")
    except json.JSONDecodeError:
        print(f"  [agent] JSON parse failed — saving raw output", file=sys.stderr)
        signal = {
            "slot": args.slot, "type": "raw", "headline": f"Slot {args.slot} signal",
            "body": raw_output, "tags": ["raw"], "sources": []
        }

    # Heartbeat
    heartbeat_ok = False
    if args.btc_address:
        heartbeat_ok = send_heartbeat(args.btc_address)

    # Submit signal
    submit_ok = False
    if args.btc_address:
        submit_ok = submit_signal(signal, args.btc_address)

    # Log
    append_log(args.log, args.slot, signal, heartbeat_ok, submit_ok)

    print(f"[agent] Done — slot {args.slot} complete")


if __name__ == "__main__":
    main()
