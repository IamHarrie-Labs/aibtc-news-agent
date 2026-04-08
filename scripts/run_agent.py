#!/usr/bin/env python3
"""
run_agent.py

Researches source data, writes a btc-macro signal targeting 85+/100,
self-scores against the editorial rubric, and submits only if score >= 80.

Model priority:
  1. Anthropic Claude Sonnet 4.6 -- best quality (uses ANTHROPIC_API_KEY)
  2. Groq Llama 3.3 70B -- fallback (uses GROQ_API_KEY)

Signal scoring rubric (100 points):
  A. Newsworthiness  (25) -- first report, named institution, within 48h
  B. Evidence Quality (25) -- 2+ primary sources from different publications
  C. Precision       (25) -- exact numbers, correct affiliations, named executives
  D. Beat Relevance  (25) -- core btc-macro, not infrastructure or speculation
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
MAX_SIGNALS_PER_DAY = 4
QUALITY_GATE = 80


# ---------------------------------------------------------------------------
# Signal submission via Node.js (BTC signature + POST)
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
        print("  [sign] WALLET_MNEMONIC not set -- attempting unsigned submission")
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
    """Call Anthropic Claude Sonnet 4.6 via direct HTTP."""
    import urllib.request as urlreq

    body = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 2000,
        "temperature": 0.1,
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
    try:
        with urlreq.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            if "content" not in data:
                raise ValueError(f"Unexpected API response: {str(data)[:200]}")
            return data["content"][0]["text"]
    except urlreq.HTTPError as e:
        body_err = e.read().decode()[:300]
        raise RuntimeError(f"Anthropic API HTTP {e.code}: {body_err}")


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
        temperature=0.1,
        max_tokens=2000,
    )
    return message.choices[0].message.content


def call_llm(prompt: str) -> tuple:
    """Try Anthropic Claude Sonnet 4.6 first, fall back to Groq."""
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    groq_key = os.environ.get("GROQ_API_KEY", "").strip()

    if anthropic_key:
        try:
            print("Using Anthropic Claude Sonnet 4.6...")
            output = call_claude(prompt, anthropic_key)
            return output, "claude-sonnet-4-6"
        except Exception as e:
            print(f"  [warn] Claude failed: {e}. Falling back to Groq.")

    if groq_key:
        print("Using Groq (llama-3.3-70b-versatile)...")
        output = call_groq(prompt, groq_key)
        return output, "groq llama-3.3-70b"

    print("[error] Neither ANTHROPIC_API_KEY nor GROQ_API_KEY is set.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

SLOT_LABEL = {1: "00:00 UTC", 2: "01:00 UTC", 3: "02:00 UTC", 4: "03:00 UTC"}


def build_prompt(slot: int, beat: str, btc_address: str, articles: list) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    articles = sorted(articles, key=lambda x: x.get("published", ""), reverse=True)

    source_block = ""
    for i, a in enumerate(articles[:15], 1):
        source_block += (
            f"\n[{i}] {a['title'][:120]}\n"
            f"    Source: {a['source']} | Published: {a.get('published', 'unknown')[:19]} UTC\n"
            f"    URL: {a['url']}\n"
            f"    Summary: {a.get('summary', '')[:400]}\n"
        )

    return f"""You are Serene Spring, Bitcoin Macro correspondent at aibtc.news.
Your beat: institutional Bitcoin adoption, ETF flows, regulatory milestones, major BTC price events backed by macro drivers.

TODAY: {today}  |  SLOT: {SLOT_LABEL.get(slot, str(slot))}

---
SOURCE DATA ({len(articles)} items, newest first):
{source_block}
---

## SCORING RUBRIC (target 85+ / 100)

A. Newsworthiness (0-25)
22-25: First report of a named institution confirmed action within 48h, with a specific figure
17-21: Timely (within 72h), specific institution named, quantified impact
Below 17: Older story, no institution named, repetition of known trend

B. Evidence Quality (0-25)
22-25: TWO OR MORE independent sources from DIFFERENT publications, each a direct article URL
17-21: Two sources but one is weaker (blog, secondary report)
Below 17: Single source only -- platform WILL REJECT. Must have two.

C. Precision (0-25)
22-25: Exact dollar/percentage/date figures, named executive or agency, institution full name
17-21: Accurate but missing one precision detail
Below 17: Vague numbers ("significant", "large"), no named entity

D. Beat Relevance (0-25)
22-25: Core btc-macro -- institutional, ETF, regulatory, or major BTC price milestone with macro driver
17-21: Clearly Bitcoin macro context
Below 12: Infrastructure changelog, block data, fee rates, Stacks ecosystem -- WRONG BEAT, will be rejected

---

## TASK

### STEP 1 -- FIND THE BEST STORY WITH TWO SOURCES

Scan ALL source items. Find ONE story covered by AT LEAST TWO items from DIFFERENT publications.

Qualifying stories:
- Confirmed institutional action (ETF launch, corporate treasury buy, brokerage launch, bank offering)
- Regulatory event (SEC ruling, CFTC guidance, government mandate, legislative milestone)
- ETF flow data with specific daily/weekly dollar figures from a data provider
- Major BTC price event WITH a named institutional or macro driver (not just "BTC price rose")

Disqualifying stories (output NO_SIGNAL if only these are available):
- Only one source available for the best story
- Raw on-chain data: block heights, fee rates, mempool size, sat/vB numbers
- GitHub releases or changelogs -- not btc-macro
- AIBTC platform stats or reports -- not btc-macro
- Price movement with no named institutional or macro cause

### STEP 2 -- WRITE THE SIGNAL

Use EXACTLY this format. One line per field. No markdown. No bullet points. No extra text.

HEADLINE: [Max 100 chars. Format: "[Institution/Subject] [Specific Action] -- [Key Figure or Implication]". No trailing period. Lead with the news, not the source name.]
BODY_1: [THE FACT. One sentence. Lead with the most important number. Name the institution and its full name. Reference both sources. Example: "Morgan Stanley spot Bitcoin ETF (MBST) began trading April 8, 2026, with 16,000 advisors authorized to sell to retail clients, per Bitcoin Magazine and CoinDesk."]
BODY_2: [THE SIGNIFICANCE. One sentence. Explain the structural implication -- why does this cohort or event matter to Bitcoin adoption beyond what already exists? Compare to existing demand cohorts (ETFs, corporate treasury, retail) if relevant.]
BODY_3: [AGENT ACTION. One sentence. SPECIFIC operational instruction for an autonomous Bitcoin agent. Must include at least one threshold, timeframe, or parameter. BANNED phrases: "adjust portfolio", "monitor closely", "watch for", "consider adjusting", "update models", "adjust strategies", "adjust allocation". REQUIRED: reference a specific number, date, or condition. Good examples: "Agents with BTC exposure below 3pct of AUM should trigger a rebalance before MBST first-day close if inflows exceed $200M." or "If FDIC final rule passes Senate by June 30, agents running stablecoin-to-BTC rotation should shift the trigger threshold from 90-day to 30-day settlement windows."]
SOURCE_URL_1: [Direct article URL from publication 1 -- specific article page, not a homepage or API endpoint]
SOURCE_URL_2: [Direct article URL from publication 2 -- must be a DIFFERENT publication than SOURCE_URL_1]
SOURCES_USED: [Publication names comma-separated]

### STEP 3 -- SELF-SCORE (honest assessment only)

SCORE_A: [0-25]  Reason: [one clause]
SCORE_B: [0-25]  Reason: [confirm two independent publication URLs present]
SCORE_C: [0-25]  Reason: [identify specific numbers and named entities in the signal]
SCORE_D: [0-25]  Reason: [confirm this is core btc-macro, not infrastructure or off-beat]
SCORE_TOTAL: [A+B+C+D]

### STEP 4 -- QUALITY GATE

If SCORE_TOTAL < 80:
  - Identify the single weakest dimension
  - Rewrite that section only
  - Re-score once
  - If still < 80 output: NO_SIGNAL

If SCORE_TOTAL >= 80, output the final signal:

HEADLINE: ...
BODY_1: ...
BODY_2: ...
BODY_3: ...
SOURCE_URL_1: ...
SOURCE_URL_2: ...
SOURCES_USED: ...
FINAL_SCORE: [number]
"""


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_signal(output: str) -> dict:
    fields = {
        "headline": "",
        "body_1": "",
        "body_2": "",
        "body_3": "",
        "source_url_1": "",
        "source_url_2": "",
        "sources_used": "",
        "final_score": 0,
    }

    patterns = {
        "headline":     r"^HEADLINE:\s*(.+)",
        "body_1":       r"^BODY_1:\s*(.+)",
        "body_2":       r"^BODY_2:\s*(.+)",
        "body_3":       r"^BODY_3:\s*(.+)",
        "source_url_1": r"^SOURCE_URL_1:\s*(.+)",
        "source_url_2": r"^SOURCE_URL_2:\s*(.+)",
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


_INFRA_URL_PATTERNS = [
    "github.com/bitcoin/bitcoin",
    "github.com/aibtcdev/",
    "mempool.space/api",
    "mempool.space/block/",
    "api.hiro.so/",
    "explorer.hiro.so/",
    "aibtc.news/api/",
    "aibtc.com/api/",
]

_INFRA_HEADLINE_RE = re.compile(
    r"releases? v\d|release.*v\d+\.\d+|v\d+\.\d+\.\d+.*release|"
    r"block #?\d{5,}|mempool fee|sat/vb|\d+ transactions.*kb|"
    r"mcp server release|bitcoin core release",
    re.IGNORECASE,
)

_FORBIDDEN_BODY3_RE = re.compile(
    r"adjust (your |their |its |portfolio|allocation|trading|market|risk|sentiment|strateg)|"
    r"monitor closely|watch for|consider (adjusting|monitoring|rebalancing|shifting)|"
    r"update (their |your |its |models|algorithms|strategies)|"
    r"agents should (adjust|monitor|watch|consider|update|optimize|track)",
    re.IGNORECASE,
)


def submit_signal(parsed: dict, beat: str, btc_address: str, model_name: str) -> bool:
    headline = parsed.get("headline", "").strip()
    body = parsed.get("body", "").strip()
    source_url_1 = parsed.get("source_url_1", "").strip()
    source_url_2 = parsed.get("source_url_2", "").strip()
    sources_used = parsed.get("sources_used", "").strip()

    if not headline or not source_url_1:
        print("  [warn] Missing headline or primary source URL -- not submitting.")
        return False

    beat_lower = beat.lower()
    if any(kw in beat_lower for kw in ("bitcoin macro", "btc-macro", "bitcoin-macro",
                                        "defi and stacks", "defi & stacks")):
        for pattern in _INFRA_URL_PATTERNS:
            if pattern.lower() in source_url_1.lower() or pattern.lower() in source_url_2.lower():
                print(f"  [gate] Infrastructure URL blocked for btc-macro: {source_url_1}")
                return False
        if _INFRA_HEADLINE_RE.search(headline):
            print(f"  [gate] Infrastructure headline blocked for btc-macro: {headline}")
            return False

    body_3 = parsed.get("body_3", "")
    if _FORBIDDEN_BODY3_RE.search(body_3):
        print(f"  [gate] Generic BODY_3 blocked -- rewrite required: {body_3[:100]}")
        return False

    sources = [{"url": source_url_1, "title": headline[:100]}]
    if source_url_2 and source_url_2 != source_url_1:
        sources.append({"url": source_url_2, "title": f"{headline[:80]} (2)"})

    payload = {
        "btc_address": btc_address,
        "beat_slug": derive_beat_slug(beat),
        "headline": headline[:120],
        "content": body[:1000],
        "sources": sources,
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
            return sum(1 for line in f if today in line
                       and "skipped" not in line and "error" not in line)
    except FileNotFoundError:
        return 0


def get_recent_urls(log_path: str, hours: int = 48) -> set:
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    urls = set()
    try:
        with open(log_path) as f:
            for line in f:
                if not line.rstrip().endswith("submitted"):
                    continue
                parts = [p.strip() for p in line.split("|")]
                if len(parts) < 5:
                    continue
                try:
                    from datetime import datetime as dt
                    ts = dt.fromisoformat(parts[0].replace("Z", "+00:00")).timestamp()
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


def is_duplicate(source_url: str, recent_urls: set) -> bool:
    if source_url in recent_urls:
        return True
    base = source_url.split("?")[0].rstrip("/")
    return any(u.split("?")[0].rstrip("/") == base for u in recent_urls)


# ---------------------------------------------------------------------------
# Pre-filed signal loader (bypasses LLM when prefiled_signals.json exists)
# ---------------------------------------------------------------------------

def load_prefiled_signal(slot: int) -> dict | None:
    """
    Check for prefiled_signals.json in the repo root.
    Returns the signal dict for the given slot if:
      - the file exists
      - the 'date' field matches today UTC
      - a signal for this slot is present
    Returns None otherwise (fall through to LLM).
    """
    prefiled_path = "prefiled_signals.json"
    if not os.path.exists(prefiled_path):
        return None
    try:
        with open(prefiled_path) as f:
            data = json.load(f)
        target_date = data.get("date", "")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if target_date != today:
            print(f"  [prefiled] date mismatch ({target_date} vs {today}) — using LLM.")
            return None
        for sig in data.get("signals", []):
            if sig.get("slot") == slot:
                print(f"  [prefiled] Found pre-written signal for slot {slot}.")
                return sig
    except Exception as e:
        print(f"  [prefiled] Could not load prefiled_signals.json: {e}")
    return None


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
    if count >= MAX_SIGNALS_PER_DAY:
        print(f"[abort] Already filed {count} signals today (limit {MAX_SIGNALS_PER_DAY}). Exiting.")
        sys.exit(0)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- PRE-FILED SIGNAL PATH (no LLM needed) ---
    prefiled = load_prefiled_signal(args.slot)
    if prefiled:
        headline = prefiled.get("headline", "")
        body     = prefiled.get("body", "")
        url1     = prefiled.get("source_url_1", "")
        url2     = prefiled.get("source_url_2", "")
        sources  = prefiled.get("sources_used", "")
        score    = prefiled.get("self_score", 0)
        tags     = prefiled.get("tags", derive_tags(args.beat))
        print(f"\n[prefiled] Headline : {headline}")
        print(f"[prefiled] Score    : {score}/100")
        print(f"[prefiled] Source 1 : {url1}")
        print(f"[prefiled] Source 2 : {url2}")

        recent_urls = get_recent_urls(args.log)
        if url1 and is_duplicate(url1, recent_urls):
            print("[prefiled] Duplicate detected — skipping.")
            append_log(args.log, f"{ts} | slot {args.slot} | {args.beat} | duplicate | {url1[:80]} | skipped")
            sys.exit(0)

        sources_list = [{"url": url1, "title": headline[:100]}]
        if url2 and url2 != url1:
            sources_list.append({"url": url2, "title": f"{headline[:80]} (2)"})

        payload = {
            "btc_address": args.btc_address,
            "beat_slug": derive_beat_slug(args.beat),
            "headline": headline[:120],
            "content": body[:1000],
            "sources": sources_list,
            "tags": tags,
            "disclosure": f"pre-researched by Serene Spring, sources: {sources[:150]}",
        }
        submitted = submit_via_node(payload, args.btc_address)
        status = "submitted" if submitted else "submit-failed"
        append_log(args.log,
            f"{ts} | slot {args.slot} | {args.beat} | score {score} | {headline[:80]} | {url1} | {status}")
        sys.exit(0)

    # --- LLM PATH (fallback when no pre-filed signal) ---
    with open(args.sources) as f:
        data = json.load(f)

    articles = data.get("articles", [])
    if not articles:
        print("[abort] No articles in source file.")
        sys.exit(0)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"Slot {args.slot} | Beat: {args.beat} | {len(articles)} items | today: {count}/{MAX_SIGNALS_PER_DAY}")

    prompt = build_prompt(slot=args.slot, beat=args.beat,
                          btc_address=args.btc_address, articles=articles)

    try:
        output, model_name = call_llm(prompt)
    except Exception as e:
        print(f"[error] LLM call failed: {e}")
        append_log(args.log, f"{ts} | slot {args.slot} | {args.beat} | llm error | -- | error")
        sys.exit(1)

    print("\n--- LLM output ---")
    print(output[:3000])
    print("---")

    if "NO_SIGNAL" in output:
        print("[info] Agent returned NO_SIGNAL -- no publishable data this slot.")
        append_log(args.log, f"{ts} | slot {args.slot} | {args.beat} | no signal | -- | skipped")
        sys.exit(0)

    parsed = parse_signal(output)
    score = parsed.get("final_score", 0)

    print(f"\nParsed headline : {parsed['headline']}")
    print(f"Self-score      : {score}/100")
    print(f"Source 1        : {parsed['source_url_1']}")
    print(f"Source 2        : {parsed['source_url_2']}")

    if 0 < score < QUALITY_GATE:
        print(f"[gate] Self-score {score} < {QUALITY_GATE}. Skipping.")
        append_log(args.log,
            f"{ts} | slot {args.slot} | {args.beat} | {parsed['headline'][:80]} | score {score} | skipped-low-score")
        sys.exit(0)

    recent_urls = get_recent_urls(args.log)
    if parsed["source_url_1"] and is_duplicate(parsed["source_url_1"], recent_urls):
        print("[gate] Duplicate story already filed. Skipping.")
        append_log(args.log,
            f"{ts} | slot {args.slot} | {args.beat} | duplicate | {parsed['source_url_1'][:80]} | skipped")
        sys.exit(0)

    if parsed["headline"] and parsed["source_url_1"]:
        submitted = submit_signal(parsed, args.beat, args.btc_address, model_name)
        status = "submitted" if submitted else "submit-failed"
    else:
        print("[warn] Could not parse signal fields from LLM output.")
        status = "parse-failed"

    append_log(args.log,
        f"{ts} | slot {args.slot} | {args.beat} | score {score} | "
        f"{parsed['headline'][:80] or 'no headline'} | "
        f"{parsed['source_url_1'] or '--'} | {status}")
    sys.exit(0)


if __name__ == "__main__":
    main()
