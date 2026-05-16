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
**Instance:** `nextus.app.n8n.cloud`

| Workflow | n8n ID | Status | Entry |
|---|---|---|---|
| Write Conversation Event | `GUUEUvfjFwLojoA0` | Active | `POST /mco-write-event` |
| Fetch Cross-Channel Context | `JXpvRl8WTAqigVfi` | Active | `POST /mco-fetch-context` |
| FollowUp Queue Dispatcher | `BQJkE0sa0yRKWDjM` | Active | Schedule (every 15 min) |
| Centralized Follow-Up Coordinator | `R9bkR97Xt5fHSN4K` | Active | `POST /mco-followup` |
| Aimfox Connection Accepted Handler | `8MxgrCTDN6IZ98iF` | Active | Aimfox webhook (`accepted` event) |
| Aimfox Nextus AI Reply Agent — MCO | `mAGUFlmJZ0gwge3s` | Active | Aimfox webhook (new reply) |
| Gmail Reply Agent | `7pTHnRue85rqTvXk` | **INACTIVE** | Gmail trigger |

### Webhook URLs
```
Write Event:   https://nextus.app.n8n.cloud/webhook/mco-write-event
Fetch Context: https://nextus.app.n8n.cloud/webhook/mco-fetch-context
Coordinator:   https://nextus.app.n8n.cloud/webhook/mco-followup
Aimfox Accept: https://nextus.app.n8n.cloud/webhook/mco-aimfox-accepted
```

### External Services
- **Monday.com** board `18399476470` — pipeline tracking. Write Event creates items, posts updates, sets intent + channel columns.
- **Aimfox API** `https://api.aimfox.com/api/v2` — LinkedIn messaging. Token stored in workflow nodes.
- **Gmail** `anik@nextus.ai` — warm follow-up emails sent by the Coordinator.
- **Anthropic** credential ID in n8n: `tFYLbQt9S6IzWYNd`, model: `claude-sonnet-4-6`

---

## Key Workflows (SOPs in `workflows/`)

### `write_conversation_event.md`
The single write path for the entire system. Required fields: `event_id` (UUID, dedup key), `lead_email`, `channel`, `direction`, `content`, `timestamp`. Optional: `trigger_cross_channel: true` + `target_channels: ["email"]` to queue a follow-up. Always set `trigger_cross_channel` only for `interested` or `booking` intent.

### `fetch_cross_channel_context.md`
Returns a formatted `context_block` text for injection into AI agent prompts. Required: `lead_email`. Optional: `requesting_channel` (prepends a channel-awareness header), `max_events` (default 20).

### `retell_webhook_handler.md`
Pending — workflow not built yet. Waiting for Retell AI JSON workflow files.

---

## What's Live vs. Not Built

| Scenario | Status |
|---|---|
| Lead replies on LinkedIn → AI replies → logged to Supabase | Live |
| Lead marked Interested on LinkedIn → email follow-up queued | Live |
| Dispatcher fires every 15 min → Coordinator sends email follow-up | Live |
| Connection accepted → personalised opening LinkedIn message sent | Live |
| Lead replies to follow-up email → AI replies → logged | **Not active** (Gmail agent needs credential + activation) |
| Voice/SMS call ends → logged to Supabase | **Not built** (Retell handler pending) |

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
