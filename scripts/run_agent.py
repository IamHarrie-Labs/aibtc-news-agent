#!/usr/bin/env python3
"""
run_agent.py

Researches source data, writes a signal targeting 80-100/100,
self-scores against the editorial rubric, and submits only if score >= 75.

Model: Anthropic Claude claude-haiku-4-5-20251001 (fast, accurate, no fallback)

Signal scoring rubric (100 points):
  A. Newsworthiness  (25) — first report, named institution, within 48h
  B. Evidence Quality (25) — primary/specific source URL, named figures
  C. Precision       (25) — exact numbers, correct affiliations, specific dates
  D. Beat Relevance  (25) — core beat, couldn't belong to any other
"""

import argparse
import json
import os
import re
import subprocess
import sys
import textwrap
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

AIBTC_API = "https://aibtc.news/api"
DAILY_CAP = 4          # platform cap per correspondent per day
MIN_SCORE = 75         # minimum self-score to submit
DEDUP_HOURS = 48       # window for platform-side deduplication


# ---------------------------------------------------------------------------
# Signal submission (sign + POST via Node.js)
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
        const proc = spawn('npx', ['-y', '@aibtc/mcp-server@latest'], {
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
# Model client — Claude Haiku only, no fallback
# ---------------------------------------------------------------------------

def call_claude(prompt: str) -> str:
    """Call Anthropic Claude Haiku. Exits hard if API key is missing."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("[error] ANTHROPIC_API_KEY is not set. Set it as a GitHub secret.")
        sys.exit(1)

    body = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 1500,
        "temperature": 0.2,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()

    req = urllib.request.Request(
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
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            return data["content"][0]["text"]
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()[:300]
        print(f"[error] Anthropic API error {e.code}: {body_text}")
        sys.exit(1)
    except Exception as e:
        print(f"[error] Claude API call failed: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Platform deduplication — fetch recent signals, block repeats
# ---------------------------------------------------------------------------

def fetch_platform_signals_today() -> list:
    """Fetch signals from aibtc.news filed in the last DEDUP_HOURS hours."""
    try:
        req = urllib.request.Request(
            f"{AIBTC_API}/signals",
            headers={"User-Agent": "SereneSpring/1.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            signals = data if isinstance(data, list) else data.get("signals", [])
    except Exception as e:
        print(f"  [dedup] Could not fetch platform signals: {e}")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=DEDUP_HOURS)
    recent = []
    for s in signals:
        ts_str = s.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts >= cutoff:
                recent.append(s)
        except Exception:
            pass
    print(f"  [dedup] {len(recent)} platform signals in last {DEDUP_HOURS}h")
    return recent


def _normalise(text: str) -> set:
    """Lowercase word set, stripping punctuation — for overlap comparison."""
    stop = {"a", "an", "the", "and", "or", "of", "in", "on", "at", "to",
            "for", "is", "are", "was", "with", "as", "by", "its", "—", "-"}
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if w not in stop and len(w) > 2}


def is_platform_duplicate(headline: str, url1: str, url2: str,
                           platform_signals: list) -> bool:
    """
    Return True if the signal is too similar to something already on the platform.
    Checks: exact URL match OR headline word-overlap >= 5 significant words.
    """
    candidate_words = _normalise(headline)
    candidate_urls = {url1.split("?")[0].rstrip("/"), url2.split("?")[0].rstrip("/")}

    for s in platform_signals:
        # URL match
        for src in s.get("sources", []):
            existing_url = src.get("url", "").split("?")[0].rstrip("/")
            if existing_url and existing_url in candidate_urls:
                print(f"  [dedup] URL match: {existing_url}")
                return True

        # Headline overlap
        existing_words = _normalise(s.get("headline", ""))
        overlap = candidate_words & existing_words
        if len(overlap) >= 5:
            print(f"  [dedup] Headline overlap ({len(overlap)} words): {s['headline'][:80]}")
            return True

    return False


# ---------------------------------------------------------------------------
# Beat-specific prompts
# ---------------------------------------------------------------------------

SLOT_LABEL = {
    1: "06:00 UTC", 2: "12:00 UTC", 3: "18:00 UTC", 4: "00:00 UTC",
}

_COMMON_FORMAT = """
FINAL OUTPUT FORMAT (copy verbatim, fill in values):
HEADLINE: [under 100 chars, include a key number, no period]
BODY_1: [THE NEWS — one sentence, lead with the most important number or named institution]
BODY_2: [SO WHAT — one sentence, why this matters to Bitcoin adoption or the Bitcoin-native AI economy]
BODY_3: [FOR AGENTS — one sentence, what should an autonomous Bitcoin agent DO differently? Be specific with a number or condition, not "monitor" or "watch"]
SOURCE_URL_1: [primary article URL — direct article path, not a homepage]
SOURCE_URL_2: [secondary article URL — DIFFERENT publication from SOURCE_URL_1]
SOURCES_USED: [comma-separated publication names]
FINAL_SCORE: [sum of A+B+C+D]
"""

_SCORE_RUBRIC = """
SELF-SCORE (be honest — inflating scores doesn't help you):
SCORE_A_NEWSWORTHINESS: [0–25]  Reason: [one clause]
SCORE_B_EVIDENCE: [0–25]  Reason: [one clause]
SCORE_C_PRECISION: [0–25]  Reason: [one clause]
SCORE_D_BEAT: [0–25]  Reason: [one clause]
SCORE_TOTAL: [sum]

If SCORE_TOTAL < 75: rewrite the weakest section, re-score once. If still < 75 → output: NO_SIGNAL
If SCORE_TOTAL >= 75 → output the FINAL OUTPUT FORMAT below (no scores in final output).
"""


def build_prompt(slot: int, beat: str, articles: list) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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

    beat_lower = beat.lower().strip()
    if beat_lower in ("quantum",):
        return _quantum_prompt(today, slot, source_block)
    if beat_lower in ("security",):
        return _security_prompt(today, slot, source_block)
    if beat_lower in ("governance",):
        return _governance_prompt(today, slot, source_block)
    if beat_lower in ("infrastructure", "bitcoin infrastructure"):
        return _infra_prompt(today, slot, source_block)
    if beat_lower in ("agent-economy", "agent economy"):
        return _agent_economy_prompt(today, slot, source_block)
    # default: bitcoin-macro
    return _macro_prompt(today, slot, source_block)


def _macro_prompt(today, slot, source_block):
    return f"""You are Serene Spring, Bitcoin Macro correspondent at aibtc.news.
Your beat: institutional Bitcoin adoption, ETF flows, regulatory milestones, major price milestones with macro context.

TODAY: {today}  |  SLOT: {SLOT_LABEL.get(slot, str(slot))}

SOURCE DATA (newest first):
{source_block}

SCORING RUBRIC (100 points — write to score 85+):
A. Newsworthiness (0–25): 22–25 = first report of institutional/regulatory development within 48h with specific data
B. Evidence Quality (0–25): 22–25 = named institution + specific dollar/percentage + direct article URL
C. Precision (0–25): 22–25 = exact AUM/flow numbers, named executive, correct affiliation, specific timeline
D. Beat Relevance (0–25): 22–25 = core btc-macro — institutional, ETF, regulatory, major price milestone

TASK:
1. Pick ONE story that is genuinely macro, published within 72h of {today}, contains at least one specific number, and links to a real article (not raw API).
   If NO item qualifies → output: NO_SIGNAL

2. Write the signal in the format below. Include DOWNSIDE/RISK — what would invalidate the thesis.

3. You MUST provide TWO source URLs from DIFFERENT publications.
{_SCORE_RUBRIC}
{_COMMON_FORMAT}"""


def _quantum_prompt(today, slot, source_block):
    return f"""You are Serene Spring, Quantum beat correspondent at aibtc.news.
Your beat: quantum computing threats to Bitcoin cryptography, post-quantum upgrade proposals (BIP-360, SPHINCS+), migration timelines, academic research on elliptic-curve vulnerability.

TODAY: {today}  |  SLOT: {SLOT_LABEL.get(slot, str(slot))}

SOURCE DATA (newest first):
{source_block}

SCORING RUBRIC (100 points — write to score 85+):
A. Newsworthiness (0–25): 22–25 = new research, milestone, or timeline update within 72h with specific data point
B. Evidence Quality (0–25): 22–25 = named researcher/institution + specific metric + direct article/paper URL
C. Precision (0–25): 22–25 = exact qubit counts, timeline years, BIP numbers, algorithm names
D. Beat Relevance (0–25): 22–25 = directly about quantum threat to Bitcoin — NOT generic quantum computing news

TASK:
1. Pick ONE story directly relevant to Bitcoin's quantum threat or post-quantum migration. Must be published within 72h of {today} and contain at least one specific number.
   If NO item qualifies → output: NO_SIGNAL

2. BODY_3 must state what a Bitcoin-holding agent should DO: e.g. "flag UTXOs in P2PK addresses as at-risk and begin consolidation to fresh P2WPKH addresses before [date/condition]".

3. Include what would INVALIDATE the thesis (e.g. "if quantum hardware timelines slip past 2032, the urgency reverses").

4. You MUST provide TWO source URLs from DIFFERENT publications or repositories.
{_SCORE_RUBRIC}
{_COMMON_FORMAT}"""


def _security_prompt(today, slot, source_block):
    return f"""You are Serene Spring, Security beat correspondent at aibtc.news.
Your beat: Bitcoin wallet security, custody concentration risk, private key theft, exchange/custodian vulnerabilities, ECDSA/Schnorr attack research, on-chain forensics.

TODAY: {today}  |  SLOT: {SLOT_LABEL.get(slot, str(slot))}

SOURCE DATA (newest first):
{source_block}

SCORING RUBRIC (100 points — write to score 85+):
A. Newsworthiness (0–25): 22–25 = specific incident or research finding within 72h with a concrete figure (BTC amount, CVE, block height)
B. Evidence Quality (0–25): 22–25 = named entity + specific loss/vulnerability metric + direct source URL
C. Precision (0–25): 22–25 = exact BTC amounts, named addresses or custodians, specific attack vector
D. Beat Relevance (0–25): 22–25 = directly actionable security risk to Bitcoin agents or holders

TASK:
1. Pick ONE story with a concrete, specific security implication published within 72h of {today}.
   If NO item qualifies → output: NO_SIGNAL

2. BODY_3 must give a SPECIFIC agent action with a threshold: e.g. "agents holding >1 BTC in single-sig hot wallets should move to multisig before [condition], given [specific risk]".

3. Include the invalidation condition: what would make this risk non-material.

4. You MUST provide TWO source URLs from DIFFERENT publications.
{_SCORE_RUBRIC}
{_COMMON_FORMAT}"""


def _governance_prompt(today, slot, source_block):
    return f"""You are Serene Spring, Governance beat correspondent at aibtc.news.
Your beat: Bitcoin BIP proposals, Stacks SIPs, PoX stacking governance, protocol parameter changes, community consensus decisions affecting the Bitcoin/Stacks ecosystem.

TODAY: {today}  |  SLOT: {SLOT_LABEL.get(slot, str(slot))}

SOURCE DATA (newest first):
{source_block}

SCORING RUBRIC (100 points — write to score 85+):
A. Newsworthiness (0–25): 22–25 = new BIP/SIP, vote outcome, or parameter change within 72h with specific numbers
B. Evidence Quality (0–25): 22–25 = named BIP/SIP + specific threshold/block height + direct GitHub or governance URL
C. Precision (0–25): 22–25 = exact block heights, STX thresholds, BIP numbers, cycle numbers
D. Beat Relevance (0–25): 22–25 = directly about protocol governance — NOT price or macro narrative

TASK:
1. Pick ONE governance event published within 72h of {today} with at least one specific on-chain number.
   If NO item qualifies → output: NO_SIGNAL

2. BODY_3 must state the agent action with a deadline: e.g. "agents stacking STX should commit before block [height] — threshold drops from X to Y in Cycle Z".

3. Include what would change the guidance (e.g. "if BIP does not reach activation threshold, agents can defer").

4. You MUST provide TWO source URLs from DIFFERENT sources (GitHub + news article acceptable).
{_SCORE_RUBRIC}
{_COMMON_FORMAT}"""


def _infra_prompt(today, slot, source_block):
    return f"""You are Serene Spring, Bitcoin Infrastructure correspondent at aibtc.news.
Your beat: Stacks protocol activity, Bitcoin fee market dynamics, block metrics, GitHub releases for the aibtc ecosystem (x402, MCP server, relay).

TODAY: {today}  |  SLOT: {SLOT_LABEL.get(slot, str(slot))}

SOURCE DATA:
{source_block}

SCORING RUBRIC:
A. Newsworthiness (0–25): 22–25 = on-chain metric change or ecosystem release within 72h with specific numbers
B. Evidence Quality (0–25): 22–25 = primary API URL or GitHub release link + specific figures
C. Precision (0–25): 22–25 = exact sat/vB rates, block heights, version numbers, tx counts
D. Beat Relevance (0–25): 22–25 = on-chain data or ecosystem tooling — NOT macro or price narrative

TASK:
1. Pick ONE infrastructure story with a concrete number published within 72h of {today}.
   If NO item qualifies → output: NO_SIGNAL

2. BODY_3: concrete agent action — e.g. "agents broadcasting transactions should use [X] sat/vB until mempool clears below [Y] txs".

3. Include what invalidates this (e.g. "if fee rate drops below X sat/vB within 2 blocks, guidance reverses").

4. You MUST provide TWO source URLs (primary API URL + secondary article or GitHub URL).
{_SCORE_RUBRIC}
{_COMMON_FORMAT}"""


def _agent_economy_prompt(today, slot, source_block):
    return f"""You are Serene Spring, Agent Economy correspondent at aibtc.news.
Your beat: autonomous Bitcoin agent activity, AIBTC network metrics, AI-native finance, sats-denominated agent transactions, agent performance on the aibtc.news leaderboard.

TODAY: {today}  |  SLOT: {SLOT_LABEL.get(slot, str(slot))}

SOURCE DATA (newest first):
{source_block}

SCORING RUBRIC (100 points — write to score 85+):
A. Newsworthiness (0–25): 22–25 = AIBTC network metric change or agent-economy development within 72h with specific numbers
B. Evidence Quality (0–25): 22–25 = named agent or protocol + specific sat/BTC figure + direct API/report URL
C. Precision (0–25): 22–25 = exact sats amounts, agent counts, leaderboard ranks, cycle numbers
D. Beat Relevance (0–25): 22–25 = about autonomous agents transacting — NOT generic AI or macro

TASK:
1. Pick ONE story about the Bitcoin-native agent economy published within 72h of {today}.
   If NO item qualifies → output: NO_SIGNAL

2. BODY_3: specific agent action — e.g. "agents with idle sBTC should enter [pool] now at [X]% APR before sentiment-driven liquidity returns and compresses yield below [Y]%".

3. Include the invalidation: what market condition would make this guidance wrong.

4. You MUST provide TWO source URLs from DIFFERENT sources.
{_SCORE_RUBRIC}
{_COMMON_FORMAT}"""


# ---------------------------------------------------------------------------
# Parser — extract fields from LLM output
# ---------------------------------------------------------------------------

def parse_signal(output: str) -> dict:
    fields = {
        "headline":     "",
        "body_1":       "",
        "body_2":       "",
        "body_3":       "",
        "source_url_1": "",
        "source_url_2": "",
        "sources_used": "",
        "final_score":  0,
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
        "bitcoin infrastructure": "infrastructure",
        "infrastructure":         "infrastructure",
        "bitcoin macro":          "bitcoin-macro",
        "bitcoin-macro":          "bitcoin-macro",
        "quantum":                "quantum",
        "security":               "security",
        "governance":             "governance",
        "agent-economy":          "agent-economy",
        "agent economy":          "agent-economy",
        "agent trading":          "agent-trading",
    }
    slug = slug_map.get(beat.lower().strip())
    if slug:
        return slug
    slug = re.sub(r"[^a-z0-9-]", "", beat.lower().replace(" ", "-"))
    return re.sub(r"-+", "-", slug).strip("-")[:50] or "bitcoin-macro"


def derive_tags(beat: str) -> list:
    tag_map = {
        "infrastructure":  ["bitcoin", "stacks", "infrastructure", "on-chain"],
        "bitcoin-macro":   ["bitcoin", "macro", "institutional", "btc"],
        "quantum":         ["quantum", "post-quantum", "security", "bitcoin"],
        "security":        ["security", "bitcoin", "custody", "risk"],
        "governance":      ["governance", "stacks", "bitcoin", "protocol"],
        "agent-economy":   ["agent-economy", "aibtc", "autonomous", "bitcoin"],
        "agent-trading":   ["agent-trading", "mcp", "x402", "autonomous"],
    }
    slug = derive_beat_slug(beat)
    return tag_map.get(slug, ["bitcoin", "aibtc"])


def _block_infra_url(url: str) -> bool:
    """Return True if URL is an infrastructure/raw-data URL that must not appear in a macro signal."""
    infra_patterns = [
        "github.com/bitcoin/bitcoin", "github.com/aibtcdev/", "github.com/bitflowfinance/",
        "mempool.space/api", "mempool.space/block/", "api.hiro.so/", "explorer.hiro.so/",
        "aibtc.news/api/", "aibtc.com/api/",
    ]
    return any(p in url.lower() for p in infra_patterns)


def _block_infra_headline(headline: str) -> bool:
    patterns = [
        r"releases? v\d", r"release.*v\d+\.\d+", r"v\d+\.\d+\.\d+.*release",
        r"block #?\d{5,}", r"mempool fee", r"\bsat/vb\b", r"\d+ transactions.*kb",
        r"mcp server release", r"bitcoin core release", r"x402.*relay.*release",
    ]
    return any(re.search(p, headline.lower()) for p in patterns)


def submit_signal(parsed: dict, beat: str, btc_address: str) -> bool:
    headline  = parsed.get("headline", "")
    body      = parsed.get("body", "")
    url1      = parsed.get("source_url_1", "")
    url2      = parsed.get("source_url_2", "")

    if not headline or not url1:
        print("  [warn] Missing headline or source URL — not submitting.")
        return False

    if not url2:
        print("  [warn] Missing SOURCE_URL_2 — signal fails the 2-source requirement. Skipping.")
        return False

    # Gate: block infrastructure content from macro beat
    slug = derive_beat_slug(beat)
    if slug in ("bitcoin-macro",):
        for url in (url1, url2):
            if _block_infra_url(url):
                print(f"  [gate] Infrastructure URL blocked for btc-macro: {url}")
                return False
        if _block_infra_headline(headline):
            print(f"  [gate] Infrastructure headline blocked for btc-macro: {headline}")
            return False

    sources = [{"url": url1, "title": headline[:100]}]
    if url2 and url2 != url1:
        sources.append({"url": url2, "title": f"{headline[:90]} (2)"})

    payload = {
        "btc_address":  btc_address,
        "beat_slug":    slug,
        "headline":     headline[:120],
        "content":      body[:1000],
        "sources":      sources,
        "tags":         derive_tags(beat),
        "disclosure":   "claude-sonnet-4-6, aibtc MCP tools",
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
            return sum(
                1 for line in f
                if today in line
                and "skipped" not in line
                and "error" not in line
                and "no signal" not in line.lower()
            )
    except FileNotFoundError:
        return 0


def get_recent_urls(log_path: str, hours: int = 48) -> set:
    """Return source URLs successfully submitted in the last N hours."""
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
                    ts = datetime.fromisoformat(parts[0].replace("Z", "+00:00")).timestamp()
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


def is_local_duplicate(url1: str, url2: str, recent_urls: set) -> bool:
    for url in (url1, url2):
        base = url.split("?")[0].rstrip("/")
        if base in recent_urls or url in recent_urls:
            return True
        for u in recent_urls:
            if u.split("?")[0].rstrip("/") == base:
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

    # Daily cap check (local log)
    count = check_todays_count(args.log)
    if count >= DAILY_CAP:
        print(f"[abort] Already filed {count} signals today. Daily cap is {DAILY_CAP}. Exiting.")
        sys.exit(0)

    with open(args.sources) as f:
        data = json.load(f)

    articles = data.get("articles", [])
    if not articles:
        print("[abort] No articles in source file.")
        sys.exit(0)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"Slot {args.slot} | Beat: {args.beat} | {len(articles)} items | signals today: {count}/{DAILY_CAP}")

    # Fetch platform signals for deduplication
    platform_signals = fetch_platform_signals_today()

    prompt = build_prompt(slot=args.slot, beat=args.beat, articles=articles)

    print("Calling Claude Haiku...")
    output = call_claude(prompt)

    print("\n--- LLM output ---")
    print(output[:2500])
    print("---")

    if "NO_SIGNAL" in output:
        print("[info] Agent returned NO_SIGNAL — no publishable data this slot.")
        append_log(args.log, f"{ts} | slot {args.slot} | {args.beat} | no signal | — | skipped")
        sys.exit(0)

    parsed = parse_signal(output)
    score  = parsed.get("final_score", 0)

    print(f"\nParsed headline:  {parsed['headline']}")
    print(f"Source URL 1:     {parsed['source_url_1']}")
    print(f"Source URL 2:     {parsed['source_url_2']}")
    print(f"Self-score:       {score}/100")

    # Quality gate
    if score > 0 and score < MIN_SCORE:
        print(f"[gate] Self-score {score} < {MIN_SCORE}. Signal quality too low. Skipping.")
        append_log(args.log, f"{ts} | slot {args.slot} | {args.beat} | {parsed['headline'][:80]} | score {score} | skipped-low-score")
        sys.exit(0)

    # Local deduplication gate
    recent_urls = get_recent_urls(args.log)
    if is_local_duplicate(parsed["source_url_1"], parsed["source_url_2"], recent_urls):
        print("[gate] Duplicate story (local log). Skipping.")
        append_log(args.log, f"{ts} | slot {args.slot} | {args.beat} | duplicate-local | {parsed['source_url_1'][:80]} | skipped")
        sys.exit(0)

    # Platform deduplication gate
    if is_platform_duplicate(parsed["headline"], parsed["source_url_1"], parsed["source_url_2"], platform_signals):
        print("[gate] Duplicate story (platform). Skipping.")
        append_log(args.log, f"{ts} | slot {args.slot} | {args.beat} | duplicate-platform | {parsed['headline'][:80]} | skipped")
        sys.exit(0)

    # Submit
    if parsed["headline"] and parsed["source_url_1"]:
        submitted = submit_signal(parsed, args.beat, args.btc_address)
        status = "submitted" if submitted else "submit-failed"
    else:
        print("[warn] Could not parse signal fields from LLM output.")
        status = "parse-failed"

    append_log(
        args.log,
        f"{ts} | slot {args.slot} | {args.beat} | score {score} | {parsed['headline'][:80] or 'no headline'} | {parsed['source_url_1'] or '—'} | {status}"
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
