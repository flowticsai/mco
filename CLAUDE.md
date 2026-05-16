# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## What This System Does

**MCO (Multi-Channel Outreach)** is an automation layer that unifies LinkedIn, email, voice, and SMS interactions for every lead into a single shared memory store (Supabase). Every AI agent in the system reads from and writes to the same store, so it always knows the full cross-channel history before replying.

The three paths:
- **Write path** — every interaction is recorded in Supabase via `POST /mco-write-event`
- **Read path** — AI agents fetch cross-channel context before replying via `POST /mco-fetch-context`
- **Dispatch path** — when a lead shows interest, a follow-up is queued in Supabase and fired by the 15-minute cron dispatcher

---

## The WAT Architecture

You operate inside the **WAT framework** (Workflows, Agents, Tools):

- **Workflows** (`workflows/`) — Markdown SOPs defining the objective, required inputs, node sequence, outputs, and error handling for each n8n workflow
- **Agents** — that's you. Read the relevant workflow, run tools in order, handle failures, update the workflow when you learn something new
- **Tools** (`tools/`) — Python scripts for deterministic execution (API calls, data writes, infrastructure setup)

**Before building anything new**, check `tools/` for existing scripts. Only create new scripts when nothing exists for the task.

**Don't update or create workflows without asking** unless explicitly told to. Workflows are living instructions — they should be refined, not discarded.

---

## Running Tests

```bash
python tools/test_workflows.py
```

Reads `WRITE_EVENT_WEBHOOK_URL`, `FETCH_CONTEXT_WEBHOOK_URL`, `SUPABASE_URL`, and `SUPABASE_SERVICE_ROLE_KEY` from `.env`. Runs 7 end-to-end tests against live n8n webhooks and Supabase:
1. Write basic event
2. Duplicate event is idempotent (`event_id` dedup)
3. Second event on different channel
4. Cross-channel trigger (queues `follow_up_queue` rows)
5. Intent demotion prevention
6. Fetch cross-channel context
7. Unknown lead returns gracefully

**Before re-running any script that makes paid API calls (Anthropic, Aimfox) — check with the user first.**

---

## Infrastructure

### Supabase
**URL:** `https://hkqssbomrcbtfbdowtgj.supabase.co`  
Auth: service role key (stored inside each n8n workflow's Config/Setup node and in `.env`)

| Table | Purpose | Key |
|---|---|---|
| `leads` | One row per lead. Canonical identity + intent | `lead_email` (PK) |
| `conversations` | One row per message, any channel | `event_id` (PK, dedup key) |
| `follow_up_queue` | Queued cross-channel follow-ups | `queue_id` (PK) |
| `phone_map` | Maps phone numbers → lead emails (for Retell/SMS) | `phone_e164` (PK) |

**Intent promotion** is handled atomically by the `upsert_lead()` Postgres RPC — intent never demotes. Order: `unknown → no_action → not_interested → referral → interested → booking`. Never implement this logic in n8n Code nodes.

Postgres RPCs called by MCO workflows (not raw SQL):
- `upsert_lead()` — creates or updates lead record with atomic intent promotion
- `insert_conversation_event()` — inserts one conversation row, dedup on `event_id`
- `fetch_lead_context()` — returns lead record + last N conversations ordered by timestamp desc

### n8n
**Instance:** `https://n8n-1404.n8n.whiteserverdns.com`

| Workflow | n8n ID | Status | Entry |
|---|---|---|---|
| Write Conversation Event | `5qOo5YzrPnW8Uj9g` | Active | `POST /webhook/mco-write-event` |
| Fetch Cross-Channel Context | `UJoDCfkmD3NJHktk` | Active | `POST /webhook/mco-fetch-context` |
| FollowUp Queue Dispatcher | `3ju6z4oJcWJqskBN` | Active | Schedule (every 15 min) |
| Centralized Follow-Up Coordinator | `KXKcCYRnK4V8v9k7` | Active | `POST /webhook/mco-followup` |
| Aimfox Connection Accepted Handler | `WTbIAJCZGtppAT91` | Active | Aimfox webhook (`accepted` event) |
| Aimfox Nextus AI Reply Agent — MCO | `SPN1NLyHH1LcfViD` | Active | Aimfox webhook (new reply) |
| MCO - Gmail Reply Agent | `mFBOGdMAsXRKD1Pv` | Active | Gmail trigger (team@flowticsai.com) |
| Aimfox Data Fetching MCO | `o9l5PClHznNgZIK8` | Active | Webhook → writes to Google Sheets |
| Post Call Analysis — MCO | `r8XKHCnL4vju2E4j` | Active | Webhook (post-call) |
| Flowtics AI Call Agent — MCO | `xE8mFF8HxPaSXNmi` | Active | Webhook + Schedule |
| MCO - Aimfox Responded | `Zw7iTErdMMJjiM7g` | Active | Aimfox responded webhook |

### Webhook URLs
```
Write Event:   https://n8n-1404.n8n.whiteserverdns.com/webhook/mco-write-event
Fetch Context: https://n8n-1404.n8n.whiteserverdns.com/webhook/mco-fetch-context
Coordinator:   https://n8n-1404.n8n.whiteserverdns.com/webhook/mco-followup
Aimfox Accept: https://n8n-1404.n8n.whiteserverdns.com/webhook/mco-aimfox-accepted
Call Agent:    https://n8n-1404.n8n.whiteserverdns.com/webhook/3adf4681-721b-452e-94b3-5618887a15c4
Post Call:     https://n8n-1404.n8n.whiteserverdns.com/webhook/9cdd28e8-7cfd-4765-a623-cda2d1b9f7a7
```

### External Services
- **Aimfox API** `https://api.aimfox.com/api/v2` — LinkedIn messaging. Token stored in workflow nodes.
- **Gmail** `team@flowticsai.com` (sender name: `Flowtics AI`) — n8n credential `Gmail account` (ID `IC6TPjXMVxTyn2R9`). Used by Coordinator and Gmail Reply Agent.
- **Anthropic** n8n credential `Anthropic account 2` (ID `WEpOCYlwQtWIw3jK`), model: `claude-sonnet-4-5-20250929`
- **Retell AI** — outbound calls from `+15722124790`, agent `agent_ff863b1414049444c174360809` (Maya - Flowtics AI). Booking link: `https://calendly.com/mahfujurrahman511351/30min`. Post-call webhook logs to Supabase via Write Event.
- **OpenAI** `gpt-4o-mini` — used by Call Agent to summarise prior conversation context before each Retell call.

---

## Key Workflows (SOPs in `workflows/`)

### `write_conversation_event.md`
The single write path for the entire system. Required fields: `event_id` (UUID, dedup key), `lead_email`, `channel`, `direction`, `content`, `timestamp`. Optional: `trigger_cross_channel: true` + `target_channels: ["email"]` to queue a follow-up. Always set `trigger_cross_channel` only for `interested` or `booking` intent.

### `fetch_cross_channel_context.md`
Returns a formatted `context_block` text for injection into AI agent prompts. Required: `lead_email`. Optional: `requesting_channel` (prepends a channel-awareness header), `max_events` (default 20).

### Call Agent + Post Call Analysis
The call pipeline is fully built. The Call Agent (`xE8mFF8HxPaSXNmi`) runs on schedule every 4 hours AND accepts on-demand webhook triggers. It fetches pending `voice` follow-ups from `follow_up_queue`, summarises prior context with GPT-4o-mini, then fires a Retell outbound call. After the call, Retell fires `call_analyzed` to the Post Call Analysis webhook (`r8XKHCnL4vju2E4j`), which writes the call summary to Supabase. The `Unified Input` node in the Call Agent normalises data from both the schedule and webhook paths so downstream nodes work identically.

---

## What's Live vs. Not Built

| Scenario | Status |
|---|---|
| Lead replies on LinkedIn → AI replies → logged to Supabase | Live |
| Lead marked Interested on LinkedIn → email follow-up queued | Live |
| Dispatcher fires every 15 min → Coordinator sends email from team@flowticsai.com | Live |
| Connection accepted → personalised opening LinkedIn message sent | Live |
| Lead replies to follow-up email → AI replies → logged | Live (Gmail Reply Agent active) |
| Post-call analysis triggered via webhook → logged to Supabase | Live |
| Flowtics AI outbound call agent — schedule (every 4h) + on-demand webhook | Live |
| Aimfox data fetched → written to Google Sheets | Live (Aimfox Data Fetching MCO) |
| Aimfox responded event handling | Live (MCO - Aimfox Responded) |
| Voice/SMS call → logged to Supabase via Retell | **Not built** (Retell handler pending) |
| Retool dashboard | **Not built** |

---

## File Structure

```
workflows/          # Markdown SOPs — read these before touching any n8n workflow
tools/              # Python scripts for deterministic execution
  test_workflows.py # E2E test suite — run this to verify Write + Fetch workflows
  setup_infrastructure.py  # One-time Supabase setup (already run)
  supabase_setup.sql        # Schema for Supabase (already applied)
docs/
  MCO_System_Guide.md       # Plain-English architecture overview
  MCO_Connections_Reference.md  # Every URL, credential, and data flow
MCO_AUDIT.md        # Point-in-time system audit (2026-05-12)
SETUP.md            # Phase-by-phase infrastructure setup guide
.env                # API keys and webhook URLs — never store secrets elsewhere
.tmp/               # Disposable intermediate files (Python patch scripts, JSON payloads)
```

**Deliverables go to cloud services** (n8n, Supabase, Monday.com, Google Sheets). Everything in `.tmp/` is regenerable and disposable.

---

## Self-Improvement Loop

When a tool or workflow fails:
1. Read the full error trace
2. Fix the script or workflow
3. Verify the fix (run the test suite or a targeted curl)
4. Update the relevant `workflows/*.md` with what you learned (rate limits, unexpected behaviour, new constraints)
5. If the fix involved a paid API call — check with the user before re-running

This is how the system gets more reliable over time. Every failure is input.
