#!/usr/bin/env python3
"""
run_agent.py

Sends primary-API data to Groq (Llama 3.3 70B) with a Platinum Halo-style
editorial prompt. Enforces Claim → Evidence → Implication structure.
Submits accepted signals to aibtc.news and logs the result.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

try:
    from groq import Groq
except ImportError:
    print("Missing dep. Run: pip install groq")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("Missing dep. Run: pip install requests")
    sys.exit(1)


AIBTC_API = "https://aibtc.com/api"
HEADERS = {"Content-Type": "application/json", "User-Agent": "SereneSpring/1.0"}

SLOT_LABEL = {
    1:  "00:00 UTC",  2:  "01:00 UTC",  3:  "02:00 UTC",  4:  "03:00 UTC",
    5:  "04:00 UTC",  6:  "05:00 UTC",  7:  "06:00 UTC",  8:  "07:00 UTC",
    9:  "08:00 UTC",  10: "09:00 UTC",  11: "10:00 UTC",  12: "11:00 UTC",
    13: "12:00 UTC",  14: "13:00 UTC",  15: "14:00 UTC",  16: "15:00 UTC",
    17: "16:00 UTC",  18: "17:00 UTC",  19: "18:00 UTC",  20: "19:00 UTC",
    21: "20:00 UTC",  22: "21:00 UTC",  23: "22:00 UTC",  24: "23:00 UTC",
}


def build_prompt(slot: int, beat: str, btc_address: str, articles: list) -> str:
    source_block = ""
    for i, a in enumerate(articles[:12], 1):
        source_block += f"""
[{i}] {a['title']}
    Source: {a['source']} | Type: {a['type']}
    Published: {a.get('published', 'now')[:19]} UTC
    URL: {a['url']}
    Data: {a['summary'][:400]}
"""

    return f"""You are Serene Spring, a registered AIBTC news agent filing signals at aibtc.news.

BEAT: {beat}
SLOT: {SLOT_LABEL.get(slot, str(slot))}
AGENT BTC ADDRESS: {btc_address}

---
PRIMARY SOURCE DATA ({len(articles)} items from live APIs — GitHub, Mempool, Hiro, AIBTC):
{source_block}
---

EDITORIAL STANDARD: Every accepted signal at aibtc.news follows the Claim → Evidence → Implication framework. Think like The Economist. Quantify everything. Time-bound every assertion. No hype.

FORBIDDEN PHRASES: "could", "might", "may signal", "exciting", "revolutionary", "game-changing", "interesting development", "worth watching"

MANDATORY: Every sentence must contain at least one number (sat/vB, block height, %, commit hash, release tag, timestamp, STX amount, tx count). If you cannot include a number, the sentence is too vague — rewrite or cut it.

---

STEP 1: SELECT THE BEST DATA POINT
Review the source data above. Pick the single item that:
- Contains the most concrete, verifiable numbers
- Represents a genuine on-chain event, release, or infrastructure change — NOT a media opinion
- Is directly relevant to beat: {beat}

State which item you selected and the key numbers it contains.

STEP 2: WRITE THE SIGNAL
Use EXACTLY this format — no extra lines, no markdown, no asterisks:

THE SIGNAL
[One sentence. Lead with the most important number. Factual. Under 60 words. Example structure: "[Subject] [did X] at [metric], [context detail]."]

SO WHAT
[One sentence. Why does this specific data point matter to {beat} right now? Include a second number or comparison.]

FOR AIBTC AGENTS:
[One sentence. Concrete operational action — what should an autonomous agent DO differently because of this? Be specific.]

SOURCE LOG
[The direct primary URL from the source data above — api.github.com, mempool.space, api.hiro.so, or aibtc.news. Never a media article.]

BOTTOM LINE
[One sentence under 100 characters. Could stand alone as a tweet. Include the key number.]

STEP 3: QUALITY GATE — answer YES or NO:
Q1. Does THE SIGNAL contain at least one specific number?
Q2. Is SOURCE LOG a primary API or official repo URL (not a news site)?
Q3. Is the full signal under 1000 characters?
Q4. Does FOR AIBTC AGENTS describe a concrete agent action (not "monitor" or "watch")?
Q5. Are all forbidden phrases absent?

If any answer is NO, rewrite the offending section and re-check.
If data is too generic to meet the standard → output exactly: NO_SIGNAL

STEP 4: OUTPUT
If quality gate passes, output the final signal in the exact format from Step 2.
If quality gate fails after one rewrite → output exactly: NO_SIGNAL
"""


def call_groq(prompt: str, api_key: str) -> str:
    client = Groq(api_key=api_key)
    message = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=1024,
    )
    return message.choices[0].message.content


def parse_signal(output: str) -> dict:
    signal = {
        "the_signal": "",
        "so_what": "",
        "for_agents": "",
        "source_log": "",
        "bottom_line": "",
    }

    lines = output.splitlines()
    current_section = None
    buffer = []

    def flush(section, buf):
        if section and buf:
            text = " ".join(l.strip() for l in buf if l.strip())
            signal[section] = text

    section_map = {
        "THE SIGNAL": "the_signal",
        "SO WHAT": "so_what",
        "FOR AIBTC AGENTS:": "for_agents",
        "FOR AIBTC AGENTS": "for_agents",
        "SOURCE LOG": "source_log",
        "BOTTOM LINE": "bottom_line",
    }

    for line in lines:
        stripped = line.strip()
        matched = False
        for marker, key in section_map.items():
            if stripped == marker or stripped.startswith(marker):
                flush(current_section, buffer)
                current_section = key
                # inline content after the marker (e.g. "SOURCE LOG\nhttps://...")
                remainder = stripped[len(marker):].strip().lstrip(":").strip()
                buffer = [remainder] if remainder else []
                matched = True
                break
        if not matched and current_section:
            # Skip quality gate lines
            if stripped.startswith("Q") and ("YES" in stripped or "NO" in stripped):
                continue
            if stripped.startswith("STEP "):
                continue
            buffer.append(stripped)

    flush(current_section, buffer)

    # Build headline and summary from parsed fields for API submission
    signal["headline"] = signal["the_signal"][:120] if signal["the_signal"] else ""
    signal["summary"] = " | ".join(filter(None, [
        signal["the_signal"],
        signal["so_what"],
        signal["for_agents"],
    ]))
    signal["source"] = signal["source_log"]
    return signal


def submit_signal(headline: str, summary: str, source_url: str, beat: str,
                  btc_address: str) -> bool:
    if not headline or not source_url:
        return False
    payload = {
        "btcAddress": btc_address,
        "headline": headline,
        "summary": summary,
        "sourceUrl": source_url,
        "beat": beat,
        "tags": derive_tags(beat),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        resp = requests.post(f"{AIBTC_API}/signals", json=payload, headers=HEADERS, timeout=15)
        if resp.status_code in (200, 201):
            print(f"  [submit] Signal accepted by aibtc.news")
            return True
        else:
            print(f"  [submit] API returned {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        print(f"  [submit] Request failed: {e}")
        return False


def derive_tags(beat: str) -> list:
    tag_map = {
        "Bitcoin Infrastructure": ["bitcoin", "stacks", "infrastructure", "on-chain"],
        "Bitcoin Macro": ["bitcoin", "macro", "btc", "institutional"],
        "Agent Trading": ["ai-agent", "mcp", "x402", "autonomous", "agent-economy"],
    }
    return tag_map.get(beat, ["bitcoin", "aibtc"])


def append_log(log_path: str, entry: str):
    with open(log_path, "a") as f:
        f.write(entry + "\n")
    print(f"  [log] {entry}")


def check_todays_count(log_path: str) -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT")
    try:
        with open(log_path) as f:
            return sum(1 for line in f if today in line and "skipped" not in line and "error" not in line)
    except FileNotFoundError:
        return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot",          type=int, required=True)
    parser.add_argument("--sources",       type=str, required=True)
    parser.add_argument("--beat",          type=str, required=True)
    parser.add_argument("--primary-skill", type=str, required=True)
    parser.add_argument("--btc-address",   type=str, required=True)
    parser.add_argument("--log",           type=str, default="news-log.md")
    args = parser.parse_args()

    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        print("[error] GROQ_API_KEY not set.")
        sys.exit(1)

    # Cap at 24 signals per UTC day
    count = check_todays_count(args.log)
    if count >= 24:
        print(f"[abort] Already filed {count} signals today. Limit is 24. Exiting.")
        sys.exit(0)

    with open(args.sources) as f:
        data = json.load(f)

    articles = data.get("articles", [])
    if not articles:
        print("[abort] No articles in source file.")
        sys.exit(0)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"Slot {args.slot} | Beat: {args.beat} | {len(articles)} data points | signals today: {count}/24")

    prompt = build_prompt(
        slot=args.slot,
        beat=args.beat,
        btc_address=args.btc_address,
        articles=articles,
    )

    print("Sending to Groq (Llama 3.3 70B)...")
    try:
        output = call_groq(prompt, api_key)
    except Exception as e:
        print(f"[error] Groq API call failed: {e}")
        append_log(args.log, f"{ts} | slot {args.slot} | {args.beat} | groq error | — | error")
        sys.exit(1)

    print("\n--- Groq output ---")
    print(output[:2000])

    if "NO_SIGNAL" in output:
        print("[info] Agent returned NO_SIGNAL — no publishable data this slot.")
        append_log(args.log, f"{ts} | slot {args.slot} | {args.beat} | no signal | — | skipped")
        sys.exit(0)

    signal = parse_signal(output)

    if signal["headline"] and signal["source"]:
        submitted = submit_signal(
            headline=signal["headline"],
            summary=signal["summary"],
            source_url=signal["source"],
            beat=args.beat,
            btc_address=args.btc_address,
        )
        status = "submitted" if submitted else "submit-failed"
    else:
        print("[warn] Could not parse signal fields from Groq output.")
        status = "parse-failed"

    append_log(
        args.log,
        f"{ts} | slot {args.slot} | {args.beat} | {signal['headline'][:80] or 'no headline'} | {signal['source'] or '—'} | {status}"
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
