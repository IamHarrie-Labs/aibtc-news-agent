# AIBTC News Agent — GitHub Actions

Automated signal filing for the AIBTC News $50K competition.
Runs 6 times per UTC day. Each run scrapes leading web3 and AI media,
ranks articles by beat relevance, and passes the top candidates to
a Claude Code agent that researches, fact-checks, writes, and submits.

---

## Repo structure

```
.github/
  workflows/
    aibtc-news-agent.yml   # Scheduler + job definition
scripts/
  scrape_sources.py        # RSS scraper — pulls from 15+ outlets
  run_agent.py             # Builds prompt, calls Claude CLI, logs result
news-log.md                # Append-only signal log (auto-committed)
```

---

## Setup

### 1. Fork or clone this repo into your GitHub account

### 2. Add these repository secrets (Settings → Secrets → Actions)

| Secret | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key from console.anthropic.com |
| `BTC_ADDRESS` | Your agent's BTC address (bc1q...) |
| `STX_ADDRESS` | Your agent's Stacks address (SP...) |
| `WALLET_PASSWORD` | Password used to encrypt your ~/.aibtc wallet |
| `AGENT_BEAT` | Exact beat name you claimed, e.g. `DeFi and Protocol Updates` |
| `AGENT_PRIMARY_SKILL` | Skill name for beat sweeps, e.g. `aibtc-news-correspondent` |

### 3. Make sure your agent is registered at Level 2 Genesis

Check: `curl -s https://aibtc.com/api/verify/{your-btc-address}`

The response must show `"registered": true` and `"level": 2`.

### 4. Enable GitHub Actions on the repo

Actions → Enable workflows

### 5. Test a manual run

Actions → AIBTC News Agent → Run workflow → set `slot_override` to `1`

---

## Schedule

| UTC time | Slot | Source buckets |
|---|---|---|
| 06:00 | 1 | arXiv, SSRN papers |
| 09:00 | 2 | Web3 media, AI outlets |
| 12:00 | 3 | On-chain data (Glassnode, Dune, Token Terminal) |
| 15:00 | 4 | Web3 media, research |
| 18:00 | 5 | Web3 media, AI outlets (scout mode) |
| 21:00 | 6 | Deal flow, ecosystem wrap |

---

## Sources scraped

**Web3 media:** The Block, Decrypt, CoinDesk, Blockworks, Bitcoin Magazine, Messari, DeFi Llama

**AI/research:** VentureBeat AI, MIT Technology Review, Import AI, Last Week in AI, Hacker News

**On-chain:** Glassnode, Dune Analytics, Token Terminal, Kaito

**Research:** arXiv cs.CR, cs.AI, q-fin, SSRN crypto

**Deal flow:** Crunchbase crypto, The Block, Blockworks

---

## Signal log format

Each approved or submitted signal is appended to `news-log.md`:

```
2026-04-04T06:12:00Z | slot 1 | Bitcoin Infrastructure | [headline] | [source URL] | submitted
```

---

## Adjusting sources

Edit `scripts/scrape_sources.py`:
- Add sources to any bucket in the `SOURCES` dict
- Adjust `BEAT_KEYWORDS` to tune relevance scoring for your beat
- Change `max_age_hours` in `fetch_feed()` (default: 48h)

---

## Notes

- The agent never exceeds 6 signals per UTC day — hard-coded guard in `run_agent.py`
- Slots with no relevant content exit cleanly with no submission
- All output is visible in the GitHub Actions run log
- The `news-log.md` file is auto-committed after each run
