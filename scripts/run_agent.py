#!/usr/bin/env python3
"""
run_agent.py

Sends scraped sources to Gemini (gemini-2.0-flash) with a structured prompt.
Gemini researches, fact-checks, and writes the signal in the required format.
The script then submits it to aibtc.news via REST API and logs the result.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

try:
    import google.generativeai as genai
except ImportError:
    print("Missing dep. Run: pip install google-generativeai")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("Missing dep. Run: pip install requests")
    sys.exit(1)


AIBTC_API = "https://aibtc.com/api"
HEADERS = {"Content-Type": "application/json", "User-Agent": "SereneSpring/1.0"}

SLOT_SKILL_MAP = {
    1:  "arxiv-research",
    2:  "beat-primary",
    3:  "tenero + query",
    4:  "beat-primary",
    5:  "aibtc-news-scout",
    6:  "aibtc-news-deal-flow",
    7:  "beat-primary",
    8:  "tenero + query",
    9:  "aibtc-news-scout",
    10: "beat-primary",
    11: "aibtc-news-deal-flow",
    12: "arxiv-research",
}

SLOT_LABEL = {
    1:  "00:00 UTC — midnight sweep",
    2:  "02:00 UTC — early research",
    3:  "04:00 UTC — pre-market",
    4:  "06:00 UTC — arxiv + papers",
    5:  "08:00 UTC — beat sweep",
    6:  "10:00 UTC — on-chain data",
    7:  "12:00 UTC — midday beat",
    8:  "14:00 UTC — market update",
    9:  "16:00 UTC — US session scout",
    10: "18:00 UTC — beat sweep",
    11: "20:00 UTC — deal flow",
    12: "22:00 UTC — daily wrap",
}


def build_prompt(slot: int, beat: str, primary_skill: str, btc_address: str, articles: list) -> str:
    skill = SLOT_SKILL_MAP.get(slot, "aibtc-news-correspondent")
    if skill == "beat-primary":
        skill = primary_skill

    source_block = ""
    for i, a in enumerate(articles[:15], 1):
        source_block += f"""
[{i}] {a['title']}
    Source: {a['source']} | Type: {a['type']}
    Published: {a.get('published', 'unknown')}
    URL: {a['url']}
    Summary: {a['summary'][:300]}
"""

    return f"""You are Serene Spring — a registered AIBTC news agent (Level 2 Genesis) filing signals at aibtc.news.

AGENT ADDRESS: {btc_address}
BEAT: {beat}
SLOT: {SLOT_LABEL.get(slot, str(slot))}
PRIMARY SKILL FOR THIS SLOT: {skill}

---
SCRAPED SOURCES ({len(articles)} articles, ranked by relevance):
{source_block}
---

YOUR TASK — follow this sequence exactly. Do not skip any step.

STEP 1: IDENTIFY THE BEST CANDIDATE
Select the ONE article that:
- Is directly relevant to your beat: {beat}
- Has a primary, verifiable URL (not an aggregator)
- Represents a genuine development, not opinion or rehash
- Was published within the last 48 hours

State which article you selected and why in one sentence.

STEP 2: VERIFY THE FACTS
List each key claim from the article and mark it:
- VERIFIED — claim is plausible and consistent with known facts
- UNVERIFIED — cannot confirm from the summary alone
- CONTRADICTED — claim conflicts with known facts

If any CORE claim is CONTRADICTED, discard and pick the next best article.

STEP 3: WRITE THE SIGNAL
Use ONLY this format:

HEADLINE: [one sentence, under 15 words, factual, no hype]
SUMMARY: [exactly 3 sentences: (1) what happened with numbers, (2) why it matters for {beat}, (3) what to watch next]
SOURCE: [direct primary URL — no aggregators]
BEAT: {beat}
TAGS: [3-5 comma-separated tags]

STEP 4: QUALITY GATE
Answer YES or NO for each:
1. Headline under 15 words?
2. Headline states a specific verifiable fact?
3. Source is a primary source?
4. All core claims verified?
5. Summary has exactly 3 sentences?
6. Would a crypto-native analyst consider this worth reading?

Only proceed if all answers are YES.

STEP 5: OUTPUT YOUR LOG ENTRY
Output exactly one line in this format — nothing after it:
LOG_ENTRY: [UTC timestamp] | slot {slot} | {beat} | [your headline] | [source URL] | submitted
"""


def call_gemini(prompt: str, api_key: str) -> str:
    """Call Gemini 2.0 Flash and return the text response."""
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.0-flash")
    response = model.generate_content(
        prompt,
        generation_config=genai.types.GenerationConfig(
            temperature=0.4,
            max_output_tokens=2048,
        )
    )
    return response.text


def submit_signal(headline: str, summary: str, source_url: str, beat: str,
                  tags: list, btc_address: str) -> bool:
    """Submit the signal to aibtc.news REST API."""
    payload = {
        "btcAddress": btc_address,
        "headline": headline,
        "summary": summary,
        "sourceUrl": source_url,
        "beat": beat,
        "tags": tags,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        resp = requests.post(f"{AIBTC_API}/signals", json=payload, headers=HEADERS, timeout=15)
        if resp.status_code in (200, 201):
            print(f"  [submit] ✓ Signal accepted by aibtc.news")
            return True
        else:
            print(f"  [submit] API returned {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        print(f"  [submit] Request failed: {e}")
        return False


def parse_signal(output: str) -> dict:
    """Extract structured fields from Gemini's output."""
    signal = {"headline": "", "summary": "", "source": "", "beat": "", "tags": []}
    lines = output.splitlines()
    summary_lines = []
    in_summary = False

    for line in lines:
        line = line.strip()
        if line.startswith("HEADLINE:"):
            signal["headline"] = line.replace("HEADLINE:", "").strip()
            in_summary = False
        elif line.startswith("SUMMARY:"):
            summary_lines = [line.replace("SUMMARY:", "").strip()]
            in_summary = True
        elif line.startswith("SOURCE:"):
            signal["source"] = line.replace("SOURCE:", "").strip()
            in_summary = False
        elif line.startswith("BEAT:"):
            signal["beat"] = line.replace("BEAT:", "").strip()
            in_summary = False
        elif line.startswith("TAGS:"):
            raw = line.replace("TAGS:", "").strip()
            signal["tags"] = [t.strip() for t in raw.split(",")]
            in_summary = False
        elif in_summary and line and not any(line.startswith(k) for k in ["SOURCE:", "BEAT:", "TAGS:", "LOG_ENTRY:", "STEP"]):
            summary_lines.append(line)

    signal["summary"] = " ".join(summary_lines).strip()
    return signal


def append_log(log_path: str, log_entry: str):
    with open(log_path, "a") as f:
        f.write(log_entry + "\n")
    print(f"  [log] {log_entry}")


def check_todays_count(log_path: str) -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT")
    try:
        with open(log_path) as f:
            return sum(1 for line in f if today in line and "error" not in line)
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

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("[error] GEMINI_API_KEY not set.")
        sys.exit(1)

    # Safety: cap at 12 signals per UTC day
    count = check_todays_count(args.log)
    if count >= 12:
        print(f"[abort] Already filed {count} signals today. Limit is 12. Exiting.")
        sys.exit(0)

    # Load scraped sources
    with open(args.sources) as f:
        data = json.load(f)

    articles = data.get("articles", [])
    if not articles:
        print("[abort] No articles in source file.")
        sys.exit(0)

    print(f"Slot {args.slot} | Beat: {args.beat} | {len(articles)} candidate articles")
    print(f"Signals filed today so far: {count}/12")

    prompt = build_prompt(
        slot=args.slot,
        beat=args.beat,
        primary_skill=args.primary_skill,
        btc_address=args.btc_address,
        articles=articles,
    )

    print("Sending prompt to Gemini 2.0 Flash...")
    try:
        output = call_gemini(prompt, api_key)
    except Exception as e:
        print(f"[error] Gemini API call failed: {e}")
        sys.exit(1)

    print("\n--- Gemini output ---")
    print(output[:3000])

    # Extract LOG_ENTRY
    log_entry = None
    for line in output.splitlines():
        if line.strip().startswith("LOG_ENTRY:"):
            log_entry = line.strip()
            break

    if not log_entry:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log_entry = f"LOG_ENTRY: {ts} | slot {args.slot} | {args.beat} | no signal filed | — | skipped"
        print("[warn] No LOG_ENTRY found in output.")

    # Parse and submit signal
    signal = parse_signal(output)
    if signal["headline"] and signal["source"] and args.btc_address:
        submit_signal(
            headline=signal["headline"],
            summary=signal["summary"],
            source_url=signal["source"],
            beat=signal["beat"] or args.beat,
            tags=signal["tags"],
            btc_address=args.btc_address,
        )

    append_log(args.log, log_entry.replace("LOG_ENTRY:", "").strip())
    sys.exit(0)


if __name__ == "__main__":
    main()
