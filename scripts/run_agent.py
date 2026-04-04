#!/usr/bin/env python3
"""
run_agent.py

Sends the scraped sources to Claude (via Anthropic API) with a
tightly structured prompt. Claude researches, fact-checks, writes
the signal in the required format, then submits it via the
aibtc-news-correspondent skill.

The submission step is a Claude Code / MCP call — this script
handles it by shelling out to `claude` CLI with the MCP server active.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone


SLOT_SKILL_MAP = {
    1: "arxiv-research",
    2: "beat-primary",       # resolved from AGENT_PRIMARY_SKILL env
    3: "tenero + query",
    4: "beat-primary",
    5: "aibtc-news-scout",
    6: "aibtc-news-deal-flow",
}

SLOT_LABEL = {
    1: "06:00 UTC — research",
    2: "09:00 UTC — beat sweep 1",
    3: "12:00 UTC — on-chain",
    4: "15:00 UTC — beat sweep 2",
    5: "18:00 UTC — scout",
    6: "21:00 UTC — deal flow",
}


def build_prompt(slot: int, beat: str, primary_skill: str, btc_address: str, articles: list[dict]) -> str:
    skill = SLOT_SKILL_MAP.get(slot, "aibtc-news-correspondent")
    if skill == "beat-primary":
        skill = primary_skill

    # Format top articles as a numbered reference list for Claude
    source_block = ""
    for i, a in enumerate(articles[:15], 1):
        source_block += f"""
[{i}] {a['title']}
    Source: {a['source']} | Type: {a['type']}
    Published: {a.get('published', 'unknown')}
    URL: {a['url']}
    Summary: {a['summary'][:300]}
"""

    return f"""You are a registered AIBTC news agent filing signals at aibtc.news.

AGENT ADDRESS: {btc_address}
BEAT: {beat}
SLOT: {SLOT_LABEL.get(slot, str(slot))}
PRIMARY SKILL FOR THIS SLOT: {skill}
PUBLISHER REVIEWING YOUR SIGNAL: Rising Leviathan (Claude Code agent)

---
SCRAPED SOURCES ({len(articles)} articles, ranked by relevance to your beat):
{source_block}
---

YOUR TASK — follow this sequence exactly. Do not skip any step.

STEP 1: IDENTIFY THE BEST CANDIDATE
Review the scraped sources above. Select the ONE article that:
- Is directly relevant to your beat: {beat}
- Has a primary, verifiable URL (not an aggregator)
- Represents a genuine development, not opinion or rehash
- Was published within the last 48 hours

State which article you selected and why in one sentence.

STEP 2: VERIFY THE FACTS
Before writing anything, verify the key factual claims in the selected article.
Use the aibtc-news-fact-checker skill to check every claim you plan to include.
List each claim and its verification status (verified / unverified / contradicted).

If any core claim is unverified or contradicted:
- Discard that article
- Select the next best candidate from the list
- Repeat verification

Do NOT proceed to Step 3 until all claims in your chosen article are verified.

STEP 3: WRITE THE SIGNAL
Write the signal using ONLY this format — no deviations:

HEADLINE: [one sentence, under 15 words, factual, no hype, no opinion]
SUMMARY: [exactly 2–3 sentences: (1) what happened, (2) why it matters for {beat}, (3) what comes next or what to watch]
SOURCE: [direct URL to the primary source — no aggregators, no newsletters]
BEAT: {beat}

Rules for the headline:
- State a specific fact, not a vague claim
- No words like "revolutionary", "game-changing", "landmark", "major"
- No questions

Rules for the summary:
- Sentence 1: the core event or finding, with numbers where they exist
- Sentence 2: why this matters specifically to {beat}
- Sentence 3: implication, next step, or what to watch

STEP 4: QUALITY GATE
Before submitting, answer each of these with YES or NO.
If any answer is NO, revise the signal or discard and start over.

1. Headline under 15 words?
2. Headline states a specific verifiable fact?
3. Source is a primary source (not aggregator)?
4. All claims in summary passed fact-check?
5. Summary has exactly 3 sentences covering what/why/next?
6. Story is new — not a repeat of a previously filed signal?
7. Would a crypto-native analyst consider this worth reading?

Only proceed if all 7 answers are YES.

STEP 5: SUBMIT
Use aibtc-news-correspondent to submit the signal.
Confirm submission and note any response from the publisher.

STEP 6: OUTPUT YOUR LOG ENTRY
After submission, output exactly one line in this format for the log:
LOG_ENTRY: [UTC timestamp] | slot {slot} | {beat} | [headline] | [source URL] | submitted

Do not output anything else after LOG_ENTRY.
"""


def submit_via_claude_cli(prompt: str, log_path: str) -> bool:
    """
    Shells out to the `claude` CLI with the MCP server active.
    Captures the LOG_ENTRY line and appends it to news-log.md.
    """
    print("Sending prompt to Claude Code agent...")

    try:
        result = subprocess.run(
            ["claude", "--print", prompt],
            capture_output=True,
            text=True,
            timeout=600,  # 10 min max
            env={**os.environ, "NETWORK": "mainnet"},
        )
    except subprocess.TimeoutExpired:
        print("[error] Claude CLI timed out after 10 minutes.")
        return False
    except FileNotFoundError:
        print("[error] claude CLI not found. Is Claude Code installed?")
        return False

    output = result.stdout
    print("\n--- Agent output ---")
    print(output[:3000])  # print first 3000 chars for the Action log
    if result.stderr:
        print("[stderr]", result.stderr[:500])

    # Extract and append the log entry
    for line in output.splitlines():
        if line.startswith("LOG_ENTRY:"):
            entry = line.replace("LOG_ENTRY:", "").strip()
            with open(log_path, "a") as f:
                f.write(entry + "\n")
            print(f"\nLogged: {entry}")
            return True

    print("[warn] No LOG_ENTRY found in agent output. Signal may not have been submitted.")
    # Log a failure entry anyway
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(log_path, "a") as f:
        f.write(f"{ts} | slot ? | error — no LOG_ENTRY in output\n")
    return False


def check_todays_count(log_path: str) -> int:
    """Count how many signals have been filed today."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT")
    try:
        with open(log_path) as f:
            return sum(1 for line in f if today in line and "error" not in line)
    except FileNotFoundError:
        return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot",          type=int,   required=True)
    parser.add_argument("--sources",       type=str,   required=True)
    parser.add_argument("--beat",          type=str,   required=True)
    parser.add_argument("--primary-skill", type=str,   required=True)
    parser.add_argument("--btc-address",   type=str,   required=True)
    parser.add_argument("--log",           type=str,   default="news-log.md")
    args = parser.parse_args()

    # Safety: never exceed 6 signals per UTC day
    count = check_todays_count(args.log)
    if count >= 6:
        print(f"[abort] Already filed {count} signals today. Limit is 6. Exiting.")
        sys.exit(0)

    # Load scraped sources
    with open(args.sources) as f:
        data = json.load(f)

    articles = data.get("articles", [])
    if not articles:
        print("[abort] No articles in source file. Nothing to file.")
        sys.exit(0)

    print(f"Slot {args.slot} | Beat: {args.beat} | {len(articles)} candidate articles")
    print(f"Signals filed today so far: {count}/6")

    prompt = build_prompt(
        slot=args.slot,
        beat=args.beat,
        primary_skill=args.primary_skill,
        btc_address=args.btc_address,
        articles=articles,
    )

    success = submit_via_claude_cli(prompt, args.log)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
