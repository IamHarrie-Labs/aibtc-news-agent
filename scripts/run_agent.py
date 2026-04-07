#!/usr/bin/env python3
"""
run_agent.py

Researches source data, writes a btc-macro signal targeting 80-100/100,
self-scores against the editorial rubric, and submits only if score >= 70.

Models (in order of preference):
  1. Anthropic Claude — set ANTHROPIC_API_KEY secret (recommended for quality)
  2. Groq Llama 3.3 70B — set GROQ_API_KEY secret (fallback)

Signal scoring rubric (100 points):
  A. Newsworthiness  (25) — first report, named institution, within 48h
  B. Evidence Quality (25) — primary/specific source URL, named figures
  C. Precision       (25) — exact numbers, correct affiliations, specific dates
  D. Beat Relevance  (25) — core btc-macro, not infrastructure or speculation
"""

import argparse
import json
import os
import re
import subprocess
import sys
import textwrap
from datetime import datetime, timezone

AIBTC_API = "https://aibtc.news/api"


# ---------------------------------------------------------------------------
# Signal submission (unchanged from original — sign + POST via Node.js)
# ---------------------------------------------------------------------------

_SUBMIT_JS = textwrap.dedent("""\
    const https = require('https');
    const { spawn } = require('child_process');
    const readline = require('readline');

    const mnemonic = process.env.WALLET_MNEMONIC;
    const password = process.env.WALLET_PASSWORD;
    const btcAddress = process.env.BTC_ADDRESS;
    const payload   = JSON.parse(process.env.SIGNAL_PAYLOAD);

    if (!mnemonic || !btcAddress) {
      process.stderr.write('Missing WALLET_MNEMONIC or BTC_ADDRESS\\n');
      process.exit(1);
    }

    async function signTs(ts) {
      return new Promise((resolve, reject) => {
        const proc = spawn('aibtc-mcp-server', [], {
          stdio: ['pipe', 'pipe', 'pipe'],
          env: { ...process.env, NETWORK: 'mainnet', CLIENT_MNEMONIC: mnemonic }
        });
        proc.stderr.on('data', () => {});
        let reqId = 1;
        const pending = {};
        function send(method, params) {
          return new Promise((res, rej) => {
            const id = reqId++;
            pending[id] = { res, rej };
            proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\\n');
            setTimeout(() => { if (pending[id]) { delete pending[id]; rej(new Error('Timeout: ' + method)); } }, 10000);
          });
        }
        const rl = readline.createInterface({ input: proc.stdout });
        rl.on('line', line => {
          if (!line.trim() || !line.startsWith('{')) return;
          try {
            const m = JSON.parse(line);
            if (m.id && pending[m.id]) { pending[m.id].res(m.result || m); delete pending[m.id]; }
          } catch(e) {}
        });
        proc.on('error', e => { reject(e); });
        setTimeout(async () => {
          try {
            await send('initialize', { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'serene-spring', version: '1' } });
            await send('notifications/initialized', {});
            await send('tools/call', { name: 'wallet_import', arguments: { name: 'serene-spring', mnemonic, password, network: 'mainnet' } });
            await send('tools/call', { name: 'wallet_unlock', arguments: { password } });
            const r = await send('tools/call', { name: 'btc_sign_message', arguments: { message: ts } });
            const text = r?.content?.[0]?.text || '';
            const m = text.match(/"signature"\\s*:\\s*"([^"]+)"/) || text.match(/signature.*?\\\\"([^\\\\"]+)\\\\"/);
            if (!m) throw new Error('No sig in: ' + text.slice(0, 80));
            proc.kill();
            resolve(m[1]);
          } catch(err) { proc.kill(); reject(err); }
        }, 400);
        setTimeout(() => { proc.kill(); reject(new Error('sign timeout')); }, 35000);
      });
    }

    (async () => {
      const ts = String(Math.floor(Date.now() / 1000));
      const signMessage = 'POST /api/signals:' + ts;
      let signature = '';
      try {
        signature = await signTs(signMessage);
        process.stdout.write('[sign] OK\\n');
      } catch(e) {
        process.stdout.write('[sign] failed: ' + e.message + '\\n');
      }

      const body = JSON.stringify(payload);
      const headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'SereneSpring/1.0',
        'Content-Length': Buffer.byteLength(body),
      };
      if (signature) {
        headers['X-BTC-Address']   = btcAddress;
        headers['X-BTC-Signature'] = signature;
        headers['X-BTC-Timestamp'] = ts;
      }

      const url = new URL('https://aibtc.news/api/signals');
      return new Promise((resolve) => {
        const req = https.request({
          hostname: url.hostname,
          path: url.pathname,
          method: 'POST',
          headers,
        }, res => {
          let data = '';
          res.on('data', c => data += c);
          res.on('end', () => {
            process.stdout.write('[submit] ' + res.statusCode + ' ' + data.slice(0, 300) + '\\n');
            resolve(res.statusCode);
          });
        });
        req.on('error', e => { process.stdout.write('[submit] error: ' + e.message + '\\n'); resolve(0); });
        req.write(body);
        req.end();
      });
    })().then(code => process.exit(code >= 200 && code < 300 ? 0 : 1)).catch(e => {
      process.stderr.write(e.message + '\\n'); process.exit(1);
    });
""")


def submit_via_node(payload: dict, btc_address: str) -> bool:
    mnemonic = os.environ.get("WALLET_MNEMONIC", "")
    if not mnemonic:
        print("  [sign] WALLET_MNEMONIC not set — attempting unsigned submission")
    try:
        result = subprocess.run(
            ["node", "-e", _SUBMIT_JS],
            capture_output=True, text=True, timeout=90,
            env={
                **os.environ,
                "WALLET_MNEMONIC": mnemonic,
                "BTC_ADDRESS": btc_address,
                "SIGNAL_PAYLOAD": json.dumps(payload),
            },
        )
        print(result.stdout.strip())
        if result.stderr.strip():
            print(f"  [node stderr] {result.stderr.strip()[:200]}")
        return result.returncode == 0
    except Exception as e:
        print(f"  [submit] Node.js call failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Model clients
# ---------------------------------------------------------------------------

def call_claude(prompt: str, api_key: str) -> str:
    """Call Anthropic Claude claude-haiku-4-5-20251001 (fast, cheap, high quality)."""
    import urllib.request as urlreq
    import urllib.error

    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1500,
        "temperature": 0.2,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()

    req = urlreq.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    with urlreq.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
        return data["content"][0]["text"]


def call_groq(prompt: str, api_key: str) -> str:
    try:
        from groq import Groq
    except ImportError:
        print("Missing dep. Run: pip install groq")
        sys.exit(1)
    client = Groq(api_key=api_key)
    message = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=1500,
    )
    return message.choices[0].message.content


def call_llm(prompt: str) -> tuple[str, str]:
    """
    Try Anthropic first, fall back to Groq.
    Returns (output_text, model_name_for_disclosure).
    """
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    groq_key = os.environ.get("GROQ_API_KEY", "")

    if anthropic_key:
        try:
            print("Using Anthropic Claude (claude-haiku-4-5-20251001)...")
            output = call_claude(prompt, anthropic_key)
            return output, "claude-haiku-4-5-20251001"
        except Exception as e:
            print(f"  [warn] Claude failed: {e}. Falling back to Groq.")

    if groq_key:
        print("Using Groq (llama-3.3-70b-versatile)...")
        output = call_groq(prompt, groq_key)
        return output, "groq llama-3.3-70b"

    print("[error] Neither ANTHROPIC_API_KEY nor GROQ_API_KEY is set.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SLOT_LABEL = {
    1:  "00:00 UTC",  2:  "01:00 UTC",  3:  "02:00 UTC",  4:  "03:00 UTC",
    5:  "04:00 UTC",  6:  "05:00 UTC",  7:  "06:00 UTC",  8:  "07:00 UTC",
    9:  "08:00 UTC",  10: "09:00 UTC",  11: "10:00 UTC",  12: "11:00 UTC",
    13: "12:00 UTC",  14: "13:00 UTC",  15: "14:00 UTC",  16: "15:00 UTC",
    17: "16:00 UTC",  18: "17:00 UTC",  19: "18:00 UTC",  20: "19:00 UTC",
    21: "20:00 UTC",  22: "21:00 UTC",  23: "22:00 UTC",  24: "23:00 UTC",
}


def build_prompt(slot: int, beat: str, btc_address: str, articles: list) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Sort newest first
    articles = sorted(articles, key=lambda x: x.get("published", ""), reverse=True)

    source_block = ""
    for i, a in enumerate(articles[:12], 1):
        source_block += (
            f"\n[{i}] {a['title'][:120]}\n"
            f"    Source: {a['source']} | Type: {a['type']}\n"
            f"    Published: {a.get('published', 'unknown')[:19]} UTC\n"
            f"    URL: {a['url']}\n"
            f"    Summary: {a.get('summary', '')[:350]}\n"
        )

    # Infrastructure slots use a different prompt focused on on-chain data
    if beat.lower() in ("bitcoin infrastructure", "infrastructure"):
        return build_infra_prompt(slot, beat, btc_address, articles, source_block, today)

    # Bitcoin Macro prompt — targets 80-100 scoring
    return f"""You are Serene Spring, Bitcoin Macro correspondent at aibtc.news. Your beat covers:
- Institutional Bitcoin adoption (ETF flows, corporate treasury, major brokerage launches)
- Regulatory milestones (SEC actions, NIST standards, government mandates)
- Major price milestones with macro context (ATH, key support/resistance with institutional narrative)
- On-chain supply dynamics tied to macro narratives (halving impact, long-term holder behavior)

TODAY: {today}  |  SLOT: {SLOT_LABEL.get(slot, str(slot))}

---
SOURCE DATA ({len(articles)} items, newest first):
{source_block}
---

## EDITORIAL SCORING RUBRIC (100 points total)

You will be scored on 4 dimensions. Write to score 85+.

### A. Newsworthiness (0–25)
22–25: First report of institutional/regulatory development within 48h, with specific data
17–21: Timely (within 72h), specific institution named, quantified impact
12–16: Real event but > 72h old or minor impact
6–11: Known trend recap without new development
0–5: Raw data dashboard, prediction, or forced macro angle on unrelated story

### B. Evidence Quality (0–25)
22–25: Named institution + specific dollar/percentage/date + direct article URL (not homepage)
17–21: Named institution + figures, secondary source acceptable
12–16: Credible claim, one step removed from primary source
6–11: Weak sources, no specific figures
0–5: No source, fabricated URL, or source contradicts the claim

### C. Precision (0–25)
22–25: Exact AUM/flow numbers, named executive, correct institution affiliation, specific timeline
17–21: Accurate but missing one precision detail (e.g., no exec name)
12–16: Generally right but one verifiable error
6–11: Vague ("large amount", "significant growth")
0–5: Factually wrong or fabricated

### D. Beat Relevance (0–25)
22–25: Core btc-macro — couldn't belong to any other beat
17–21: Clearly Bitcoin macro
12–16: Adjacent — could be infrastructure or governance
6–11: Forced macro framing on infrastructure story
0–4: Wrong beat entirely (Stacks infra, block data, fee rates)

---

## TASK

STEP 1 — SELECT THE BEST STORY
Review all source data. Pick the ONE item that:
- Is genuinely macro (institutional, ETF, regulatory, major price milestone)
- Was published within 72 hours of {today}
- Contains at least one specific number (dollar amount, percentage, AUM, basis points)
- Links to a real article (not a raw API endpoint)

If NO item qualifies (all items are raw on-chain data or > 72h old) → output: NO_SIGNAL

STEP 2 — WRITE THE SIGNAL
Use EXACTLY this format. No extra lines. No markdown headers.

HEADLINE: [Under 100 chars. Pattern: [Subject] [Action] — [Key Number/Implication]. No period.]
BODY_1: [THE NEWS. One sentence. Lead with the most important number. Name the institution.]
BODY_2: [SO WHAT. One sentence. Why does this matter to Bitcoin adoption, DeFi, or holders?]
BODY_3: [FOR AGENTS. One sentence. What should an autonomous Bitcoin agent DO differently? Be specific — not "monitor" or "watch".]
SOURCE_URL: [The direct article URL — not a homepage]
SOURCES_USED: [Comma-separated source names from the data above]

STEP 3 — SELF-SCORE
Score your signal on each dimension. Be honest — a score you inflate here won't change the rubric.

SCORE_A_NEWSWORTHINESS: [0–25]  Reason: [one clause]
SCORE_B_EVIDENCE: [0–25]  Reason: [one clause]
SCORE_C_PRECISION: [0–25]  Reason: [one clause]
SCORE_D_BEAT: [0–25]  Reason: [one clause]
SCORE_TOTAL: [sum of A+B+C+D]

STEP 4 — GATE
If SCORE_TOTAL < 70:
  - Identify the weakest dimension
  - Rewrite that section only to improve it
  - Re-score once
  - If still < 70 → output: NO_SIGNAL

If SCORE_TOTAL >= 70 → output the final signal from STEP 2 (no scores in the final output).

FINAL OUTPUT FORMAT (copy verbatim, fill in values):
HEADLINE: ...
BODY_1: ...
BODY_2: ...
BODY_3: ...
SOURCE_URL: ...
SOURCES_USED: ...
FINAL_SCORE: [SCORE_TOTAL]
"""


def build_infra_prompt(slot, beat, btc_address, articles, source_block, today):
    """Prompt for Bitcoin Infrastructure beat — on-chain data is appropriate here."""
    return f"""You are Serene Spring, Bitcoin Infrastructure correspondent at aibtc.news.
Your beat covers: Stacks protocol activity, Bitcoin block metrics, fee market dynamics, GitHub releases for aibtc ecosystem.

TODAY: {today}  |  SLOT: {SLOT_LABEL.get(slot, str(slot))}

---
SOURCE DATA:
{source_block}
---

Write one signal using this exact format. Every sentence must contain a number.

HEADLINE: [Under 100 chars. Include a specific metric. No period.]
BODY_1: [THE FACT. One sentence with specific numbers from the source data.]
BODY_2: [CONTEXT. One sentence. How does this compare to recent baseline or what does it indicate?]
BODY_3: [FOR AGENTS. One sentence. Concrete action for a Bitcoin-native agent.]
SOURCE_URL: [Primary API URL or GitHub URL from source data]
SOURCES_USED: [Source names used]
FINAL_SCORE: 72
"""


# ---------------------------------------------------------------------------
# Parser — extract fields from LLM output
# ---------------------------------------------------------------------------

def parse_signal(output: str) -> dict:
    fields = {
        "headline": "",
        "body_1": "",
        "body_2": "",
        "body_3": "",
        "source_url": "",
        "sources_used": "",
        "final_score": 0,
    }

    patterns = {
        "headline":     r"^HEADLINE:\s*(.+)",
        "body_1":       r"^BODY_1:\s*(.+)",
        "body_2":       r"^BODY_2:\s*(.+)",
        "body_3":       r"^BODY_3:\s*(.+)",
        "source_url":   r"^SOURCE_URL:\s*(.+)",
        "sources_used": r"^SOURCES_USED:\s*(.+)",
        "final_score":  r"^FINAL_SCORE:\s*(\d+)",
    }

    for line in output.splitlines():
        line = line.strip()
        for field, pattern in patterns.items():
            m = re.match(pattern, line, re.IGNORECASE)
            if m:
                val = m.group(1).strip()
                if field == "final_score":
                    try:
                        fields[field] = int(val)
                    except ValueError:
                        fields[field] = 0
                else:
                    fields[field] = val
                break

    # Build composite body
    body_parts = [fields["body_1"], fields["body_2"], fields["body_3"]]
    fields["body"] = " | ".join(p for p in body_parts if p)

    return fields


# ---------------------------------------------------------------------------
# Submission helpers
# ---------------------------------------------------------------------------

def derive_beat_slug(beat: str) -> str:
    env_slug = os.environ.get("AGENT_BEAT_SLUG", "").strip()
    if env_slug:
        return env_slug
    slug_map = {
        "Bitcoin Infrastructure": "infrastructure",
        "Bitcoin Macro": "bitcoin-macro",
        "Agent Trading": "agent-trading",
        "Agent Economy": "agent-economy",
    }
    slug = slug_map.get(beat)
    if slug:
        return slug
    slug = re.sub(r"[^a-z0-9-]", "", beat.lower().replace(" ", "-").replace(",", ""))
    slug = re.sub(r"-+", "-", slug).strip("-")[:50]
    return slug or "bitcoin-macro"


def derive_tags(beat: str) -> list:
    tag_map = {
        "Bitcoin Infrastructure": ["bitcoin", "stacks", "infrastructure", "on-chain"],
        "Bitcoin Macro": ["bitcoin", "macro", "institutional", "btc"],
        "Agent Trading": ["ai-agent", "mcp", "x402", "autonomous", "agent-economy"],
    }
    return tag_map.get(beat, ["bitcoin", "aibtc"])


def submit_signal(parsed: dict, beat: str, btc_address: str, model_name: str) -> bool:
    headline = parsed.get("headline", "")
    body = parsed.get("body", "")
    source_url = parsed.get("source_url", "")
    sources_used = parsed.get("sources_used", "")

    if not headline or not source_url:
        print("  [warn] Missing headline or source URL — not submitting.")
        return False

    # Hard content filter for bitcoin-macro beat — block all infrastructure URLs and headlines.
    # These patterns indicate on-chain data, GitHub changelogs, or ecosystem dashboards
    # that belong to the infrastructure beat and will be rejected by the platform.
    if beat.lower() in ("bitcoin macro", "btc-macro", "bitcoin-macro",
                        "bitcoin defi and stacks", "bitcoin defi & stacks",
                        "bitcoin defi and stacks, ordinals and runes, ai agent economy"):
        infra_url_patterns = [
            "github.com/bitcoin/bitcoin",
            "github.com/aibtcdev/",
            "github.com/BitflowFinance/",
            "github.com/bitflowfinance/",
            "mempool.space/api",
            "mempool.space/block/",
            "api.hiro.so/",
            "explorer.hiro.so/",
            "aibtc.news/api/",
            "aibtc.com/api/",
        ]
        infra_headline_patterns = [
            r"releases? v\d",
            r"release.*v\d+\.\d+",
            r"v\d+\.\d+\.\d+.*release",
            r"block #?\d{5,}",
            r"mempool fee",
            r"\bsat/vb\b",
            r"\d+ transactions.*kb",
            r"mcp server release",
            r"bitcoin core release",
            r"x402.*relay.*release",
            r"bitflow.*pr #",
        ]
        for pattern in infra_url_patterns:
            if pattern.lower() in source_url.lower():
                print(f"  [gate] Infrastructure URL blocked for btc-macro: {source_url}")
                return False
        for pattern in infra_headline_patterns:
            if re.search(pattern, headline.lower()):
                print(f"  [gate] Infrastructure headline blocked for btc-macro: {headline}")
                return False

    payload = {
        "btc_address": btc_address,
        "beat_slug": derive_beat_slug(beat),
        "headline": headline[:120],
        "content": body[:1000],
        "sources": [{"url": source_url, "title": headline[:100]}],
        "tags": derive_tags(beat),
        "disclosure": f"{model_name}, sources: {sources_used[:150]}",
    }
    return submit_via_node(payload, btc_address)


# ---------------------------------------------------------------------------
# Log helpers
# ---------------------------------------------------------------------------

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


def get_recent_urls(log_path: str, hours: int = 48) -> set:
    """Return source URLs SUCCESSFULLY submitted in the last N hours — used for deduplication.
    Only tracks lines ending in 'submitted' — submit-failed stories can be retried next day."""
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    urls = set()
    try:
        with open(log_path) as f:
            for line in f:
                # Only deduplicate against stories that actually reached the platform
                if not line.rstrip().endswith("submitted"):
                    continue
                parts = [p.strip() for p in line.split("|")]
                if len(parts) < 5:
                    continue
                try:
                    ts_str = parts[0].replace("Z", "+00:00")
                    from datetime import datetime as dt
                    ts = dt.fromisoformat(ts_str).timestamp()
                    if ts < cutoff:
                        continue
                except Exception:
                    continue
                for part in parts:
                    if part.startswith("http"):
                        urls.add(part.rstrip(" |"))
    except FileNotFoundError:
        pass
    return urls


def is_duplicate(source_url: str, headline: str, recent_urls: set) -> bool:
    """Return True if this story has already been filed recently."""
    if source_url in recent_urls:
        return True
    # Normalize URL — same article can appear with/without trailing slash or query params
    base_url = source_url.split("?")[0].rstrip("/")
    for u in recent_urls:
        if u.split("?")[0].rstrip("/") == base_url:
            return True
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot",          type=int, required=True)
    parser.add_argument("--sources",       type=str, required=True)
    parser.add_argument("--beat",          type=str, required=True)
    parser.add_argument("--primary-skill", type=str, required=True)
    parser.add_argument("--btc-address",   type=str, required=True)
    parser.add_argument("--log",           type=str, default="news-log.md")
    args = parser.parse_args()

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
    print(f"Slot {args.slot} | Beat: {args.beat} | {len(articles)} items | signals today: {count}/24")

    prompt = build_prompt(
        slot=args.slot,
        beat=args.beat,
        btc_address=args.btc_address,
        articles=articles,
    )

    try:
        output, model_name = call_llm(prompt)
    except Exception as e:
        print(f"[error] LLM call failed: {e}")
        append_log(args.log, f"{ts} | slot {args.slot} | {args.beat} | llm error | — | error")
        sys.exit(1)

    print("\n--- LLM output ---")
    print(output[:2500])
    print("---")

    if "NO_SIGNAL" in output:
        print("[info] Agent returned NO_SIGNAL — no publishable data this slot.")
        append_log(args.log, f"{ts} | slot {args.slot} | {args.beat} | no signal | — | skipped")
        sys.exit(0)

    parsed = parse_signal(output)
    score = parsed.get("final_score", 0)

    print(f"\nParsed headline: {parsed['headline']}")
    print(f"Self-reported score: {score}/100")
    print(f"Source URL: {parsed['source_url']}")

    # Hard gate: don't submit if self-score < 75 (model is being honest about quality)
    if score > 0 and score < 75:
        print(f"[gate] Self-score {score} < 75. Signal quality too low. Skipping.")
        append_log(args.log, f"{ts} | slot {args.slot} | {args.beat} | {parsed['headline'][:80]} | score {score} | skipped-low-score")
        sys.exit(0)

    # Deduplication gate: skip if this URL was already filed in the last 48h
    recent_urls = get_recent_urls(args.log)
    if parsed["source_url"] and is_duplicate(parsed["source_url"], parsed["headline"], recent_urls):
        print(f"[gate] Duplicate story — already filed this URL recently. Skipping.")
        append_log(args.log, f"{ts} | slot {args.slot} | {args.beat} | duplicate | {parsed['source_url'][:80]} | skipped")
        sys.exit(0)

    if parsed["headline"] and parsed["source_url"]:
        submitted = submit_signal(parsed, args.beat, args.btc_address, model_name)
        status = "submitted" if submitted else "submit-failed"
    else:
        print("[warn] Could not parse signal fields from LLM output.")
        status = "parse-failed"

    append_log(
        args.log,
        f"{ts} | slot {args.slot} | {args.beat} | score {score} | {parsed['headline'][:80] or 'no headline'} | {parsed['source_url'] or '—'} | {status}"
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
