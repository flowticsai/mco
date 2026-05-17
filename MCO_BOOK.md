# The Book of MCO
### A Complete Developer Reference — Architecture, Workflows, Design Decisions, and Everything In Between

**Last updated:** 2026-05-17  
**System status:** All 10 workflows active, fully tested end-to-end

---

## Table of Contents

1. [What MCO Is and Why It Exists](#1-what-mco-is-and-why-it-exists)
2. [The Mental Model — One Brain, Many Channels](#2-the-mental-model--one-brain-many-channels)
3. [Infrastructure](#3-infrastructure)
4. [The Data Model](#4-the-data-model)
5. [The Three Paths Through the System](#5-the-three-paths-through-the-system)
6. [Workflow Deep Dives](#6-workflow-deep-dives)
   - [Write Conversation Event](#61-write-conversation-event--the-single-write-path)
   - [Fetch Cross-Channel Context](#62-fetch-cross-channel-context--the-memory-api)
   - [FollowUp Queue Dispatcher](#63-followup-queue-dispatcher--the-heartbeat)
   - [Centralized Follow-Up Coordinator](#64-centralized-follow-up-coordinator--the-router)
   - [Aimfox Connection Accepted Handler](#65-aimfox-connection-accepted-handler--the-handshake)
   - [Aimfox Reply Agent](#66-aimfox-reply-agent--linkedin-reactive)
   - [Gmail Reply Agent](#67-gmail-reply-agent--email-reactive)
   - [Call Agent](#68-call-agent--voice-proactive)
   - [Post Call Analysis](#69-post-call-analysis--voice-outcome-handler)
   - [Aimfox Responded](#610-aimfox-responded--label-only)
7. [Critical Design Decisions](#7-critical-design-decisions)
8. [Lead Identity and the Upsert Pattern](#8-lead-identity-and-the-upsert-pattern)
9. [Intent Promotion — The One-Way Ratchet](#9-intent-promotion--the-one-way-ratchet)
10. [The n8n Data Flow Problem](#10-the-n8n-data-flow-problem)
11. [neverError Nodes — The Silent Failure Zone](#11-nevererror-nodes--the-silent-failure-zone)
12. [Debugging Playbook](#12-debugging-playbook)
13. [How to Add a New Channel](#13-how-to-add-a-new-channel)
14. [Known Constraints and Edge Cases](#14-known-constraints-and-edge-cases)
15. [What's Not Built Yet](#15-whats-not-built-yet)
16. [Bugs Fixed and What They Taught Us](#16-bugs-fixed-and-what-they-taught-us)

---

## 1. What MCO Is and Why It Exists

**MCO stands for Multi-Channel Outreach.**

The problem it solves: when you reach out to a lead on LinkedIn, then email them, then call them — three separate tools (Aimfox, Instantly AI / Gmail, Retell) each know only their slice of the conversation. The LinkedIn tool doesn't know about the email. The email tool doesn't know about the call. When an AI agent replies to a LinkedIn message, it's flying blind about what happened on the other channels.

MCO fixes this by being the shared memory layer that every channel writes to and reads from. It doesn't replace the channel tools — it wraps them. Every message in, every message out, every call, every connection — all written to one place (Supabase). Every AI agent reads from that same place before replying.

**The result:** every agent — LinkedIn reply, email reply, voice call — has the complete conversation history across all channels before it says a single word.

---

## 2. The Mental Model — One Brain, Many Channels

Think of it like this:

```
                    ┌─────────────────────┐
                    │      SUPABASE       │
                    │   (shared memory)   │
                    │                     │
                    │  leads              │
                    │  conversations      │
                    │  follow_up_queue    │
                    │  phone_map          │
                    └──────────┬──────────┘
                               │
            ┌──────────────────┼──────────────────┐
            │                  │                  │
       WRITE PATH         READ PATH          DISPATCH PATH
            │                  │                  │
    Every message      AI agents fetch      Queue → Dispatcher
    in/out calls       context before       → Coordinator
    /mco-write-event   replying via         → send on right
                       /mco-fetch-context   channel
```

**Write path** — every workflow, when it sends or receives a message, calls `POST /mco-write-event`. This records the interaction in Supabase and Notion CRM.

**Read path** — before any AI generates a reply, it calls `POST /mco-fetch-context` to get the last 20 interactions across all channels, formatted as a text block it can inject into the prompt.

**Dispatch path** — when a lead shows interest, a row is inserted into `follow_up_queue`. The Dispatcher picks it up every 15 minutes and calls the Coordinator, which sends the right message on the right channel.

Everything else is detail.

---

## 3. Infrastructure

### n8n
**Instance:** `https://n8n-1404.n8n.whiteserverdns.com`  
**API:** `GET/PUT /api/v1/workflows/:id` with header `X-N8N-API-KEY`

n8n is the automation engine. All business logic lives in n8n workflows. Supabase is storage. The channel tools (Aimfox, Gmail, Retell) are the delivery mechanisms.

### Supabase
**URL:** `https://hkqssbomrcbtfbdowtgj.supabase.co`  
**Auth:** service role key (stored in each workflow's Config/Setup node and in `.env`)  
**Fresh setup:** run `tools/schema.sql` in the Supabase SQL Editor — single idempotent script that builds all tables, indexes, RLS, and RPCs from scratch.

Supabase is the single source of truth. All reads and writes go through Postgres RPCs (not raw SQL from n8n) so the business logic lives in the database, not scattered across workflows.

### External Services

| Service | What It Does | Key Detail |
|---|---|---|
| **Aimfox** `api.aimfox.com/api/v2` | LinkedIn messaging — send DMs, start conversations, add to campaigns | Token stored in workflow nodes |
| **Retell AI** | Outbound phone calls, transcription, post-call webhooks | Agent ID: `agent_ff863b1414049444c174360809` (Maya), From: `+15722124790` |
| **Gmail** `team@flowticsai.com` | Email send/receive | n8n credential `Gmail account` ID `IC6TPjXMVxTyn2R9` |
| **Anthropic** | AI reply generation (email, LinkedIn DM) | n8n credential `Anthropic account 2`, model `claude-sonnet-4-5-20250929` |
| **OpenAI** `gpt-4o-mini` | Summarises conversation context before Retell calls | Used in Call Agent only |
| **Notion** | CRM — lead pages + conversation entries | Database ID `362dc227-748f-8184-892a-c6f8f3151b07` |
| **Google Sheets** | Conversation state gate for Aimfox Reply Agent | Credential `Google Sheets account 2` |
| **Slack** | Internal notifications from Gmail + Aimfox Reply Agents | Credential `Slack account 3` ID `EczWlUTWwagmArbp` |
| **Instantly AI** | Cold email campaigns (external, no n8n workflow) | Set Reply-To = `team@flowticsai.com` so replies hit Gmail trigger |

### Workflow Registry

| Workflow | n8n ID | Entry | Purpose |
|---|---|---|---|
| Write Conversation Event | `5qOo5YzrPnW8Uj9g` | `POST /webhook/mco-write-event` | Single write path |
| Fetch Cross-Channel Context | `UJoDCfkmD3NJHktk` | `POST /webhook/mco-fetch-context` | Memory API |
| FollowUp Queue Dispatcher | `3ju6z4oJcWJqskBN` | Schedule (every 15 min) | Heartbeat |
| Centralized Follow-Up Coordinator | `KXKcCYRnK4V8v9k7` | `POST /webhook/mco-followup` | Channel router |
| Aimfox Connection Accepted Handler | `WTbIAJCZGtppAT91` | `POST /webhook/mco-aimfox-accepted` | LinkedIn handshake |
| Aimfox Reply Agent | `SPN1NLyHH1LcfViD` | Aimfox webhook (new_reply) | LinkedIn reactive |
| Gmail Reply Agent | `mFBOGdMAsXRKD1Pv` | Gmail trigger | Email reactive |
| Call Agent | `xE8mFF8HxPaSXNmi` | `POST /webhook/3adf4681-...` + Schedule 4h | Voice proactive |
| Post Call Analysis | `r8XKHCnL4vju2E4j` | `POST /webhook/9cdd28e8-...` | Voice outcome |
| Aimfox Responded | `Zw7iTErdMMJjiM7g` | Aimfox responded webhook | Label only |

---

## 4. The Data Model

### `leads` table

One row per lead. This is the canonical identity record.

| Column | Type | Notes |
|---|---|---|
| `lead_id` | UUID PK | Auto-generated. Always use this for cross-table joins. |
| `lead_email` | text UNIQUE | Nullable. Lowercase. A lead can exist with no email (LinkedIn-only). |
| `linkedin_profile_url` | text UNIQUE | Nullable. Full URL. |
| `linkedin_urn` | text UNIQUE | Nullable. Aimfox-format URN. |
| `phone_e164` | text UNIQUE | Nullable. E.164 format. |
| `linkedin_conversation_urn` | text | The LinkedIn thread URN. Written by Connection Accepted Handler. Used by Coordinator to reply in the existing thread. |
| `full_name` | text | Lead's display name. |
| `company` | text | Lead's company. |
| `overall_intent` | text | Current highest intent. Never demotes. Values: `unknown → no_action → not_interested → referral → interested → booking`. |
| `last_active_channel` | text | Most recently active channel. |
| `last_activity_at` | timestamp | When the lead last interacted. |

**Key rule:** a lead can be identified by any of the five identifiers. The `upsert_lead()` RPC tries them in order and merges records if a match is found. You never need to know in advance which identifier you have — just pass what you know.

### `conversations` table

One row per message, any direction, any channel.

| Column | Type | Notes |
|---|---|---|
| `event_id` | UUID PK | Caller-generated. Used as dedup key — if you write the same event_id twice, the second write is a no-op. This makes every write safe to retry. |
| `lead_id` | UUID FK → leads | Always resolved by Write Event before insert. |
| `channel` | text | `email`, `linkedin`, `voice`, `sms` |
| `direction` | text | `inbound` (lead sent), `outbound` (we sent) |
| `content` | text | The message body, transcript, or summary. |
| `intent` | text | Intent classification for this specific interaction. |
| `sender_name` | text | Display name of whoever sent this. |
| `metadata` | JSONB | Anything extra: call_id, queue_id, campaign_id, triggered_by, etc. |
| `timestamp` | timestamp | When the interaction actually happened (not when it was recorded). |

**Key rule:** `event_id` dedup is the only protection against duplicate writes. Every caller must generate a UUID and pass it. Aimfox sends the same webhook up to 6 times — if the `event_id` is stable (from the Aimfox event's own `id` field), only the first write succeeds.

### `follow_up_queue` table

The dispatch backlog. Every pending cross-channel follow-up lives here.

| Column | Type | Notes |
|---|---|---|
| `queue_id` | UUID PK | Auto-generated. Passed to the Coordinator and embedded in Retell call metadata. |
| `lead_id` | UUID FK → leads | The lead this follow-up is for. |
| `lead_email` | text | Denormalised for Dispatcher convenience. |
| `target_channel` | text | `email`, `linkedin`, `voice` — which channel to use for this follow-up. |
| `status` | text | `pending`, `sent`, `skipped`, `failed`. Dispatcher only picks up `pending` rows where `scheduled_for <= NOW()`. |
| `scheduled_for` | timestamp | When to send. Set to `NOW() + delay` on insert. |
| `follow_up_context` | text (JSON string) | Channel-specific context: `conversation_urn`, `aimfox_account_id`, `linkedin_urn`, `linkedin_profile_url`. Parsed by Dispatcher's Split Rows node. |
| `trigger_event_id` | UUID FK → conversations.event_id | The conversation event that triggered this queue row. NOT NULL — must be a valid event UUID. |
| `outcome` | text | Written by Post Call Analysis after the call ends. Values: `answered`, `voicemail`, `no_answer`, `failed`. Null until the call completes. Links the queue row to its call result without changing `status`. |
| `created_at` | timestamp | When this row was inserted. |

**Key rule:** `trigger_event_id` has a FK constraint to `conversations.event_id`. You cannot insert a queue row with a `trigger_event_id` that doesn't exist in conversations. Always write the conversation event first, then insert the queue row using that event's ID.

### `phone_map` table

Maps phone numbers to leads. Used by Retell/voice to resolve a caller.

| Column | Type | Notes |
|---|---|---|
| `phone_e164` | text PK | The phone number. |
| `lead_id` | UUID FK → leads | Which lead owns this number. |
| `lead_email` | text | Denormalised. |

---

## 5. The Three Paths Through the System

Understanding these three paths covers 95% of everything MCO does.

### Path 1 — Inbound (lead sends a message)

```
Lead sends LinkedIn DM
  → Aimfox fires webhook to Aimfox Reply Agent
    → Fetch context (all channels)
    → AI generates reply
    → Send reply via Aimfox
    → Write inbound event to Supabase
    → Write outbound event to Supabase
    → Cancel pending follow-up queue items (lead just replied, no need to follow up)

Lead replies to email
  → Gmail trigger fires on team@flowticsai.com
    → Check if sender is one of our leads (Supabase gate)
    → Fetch context (all channels)
    → AI generates reply
    → Send reply via Gmail
    → Write inbound event
    → Write outbound event
    → Cancel pending follow-up queue items
```

### Path 2 — Proactive (we send a message unprompted)

```
Lead showed interest (intent=interested or booking)
  → Write Event queues a follow_up_queue row (trigger_cross_channel=true)

15 minutes later, Dispatcher fires
  → Fetches pending rows where scheduled_for <= NOW()
  → For each row: POST to Coordinator

Coordinator receives queue row
  → Fetch context (all channels)
  → Route by target_channel:
      email   → AI generates email → Gmail sends → Write Event
      linkedin (has conversation_urn) → AI generates DM → Aimfox sends → Write Event
      linkedin (no conversation_urn)  → Add to Aimfox campaign → Write Event (log connection request)
      voice   → Trigger Call Agent → Retell calls the lead
  → Mark queue row as sent/skipped
```

### Path 3 — Connection Acceptance (LinkedIn handshake)

```
Aimfox sends connection request (from Coordinator → Add to Campaign)
  → Lead accepts on LinkedIn
  → Aimfox fires accepted webhook to Connection Accepted Handler
    → Fetch lead's custom variables (LEAD_EMAIL) from Aimfox
    → Send fixed "thanks for connecting" message via Aimfox
    → Extract conversation_urn from Aimfox response
    → Write event to Supabase (stores conversation_urn on lead record)
    → Insert follow_up_queue row (24h later, linkedin channel)

24 hours later, Dispatcher picks up the queue row
  → Coordinator fires
  → Finds conversation_urn on lead record
  → Takes LinkedIn DM path (not campaign path)
  → Replies in the existing conversation thread
```

---

## 6. Workflow Deep Dives

### 6.1 Write Conversation Event — The Single Write Path

**ID:** `5qOo5YzrPnW8Uj9g` | **Entry:** `POST /webhook/mco-write-event`

This is the most important workflow. Every other workflow calls this one. You never write directly to Supabase from a channel workflow — you always go through here.

**Why?** Because this workflow handles lead resolution (finding or creating the lead), intent promotion, Notion CRM sync, phone map updates, and cross-channel queue creation. If you wrote directly to Supabase, you'd have to replicate all of that everywhere.

**The node sequence in plain English:**

1. **Webhook** — receives the payload
2. **Setup & Validate** — checks required fields. Requires: `event_id`, `channel`, `direction`, `content`, `timestamp`, and at least one of: `lead_id`, `lead_email`, `linkedin_profile_url`, `linkedin_urn`, `phone_e164`. Normalises email to lowercase.
3. **Upsert Lead** — calls `upsert_lead()` Postgres RPC. This is atomic — it finds the lead by any identifier, creates if not found, promotes intent if the new intent is higher, and returns the `lead_id`. You never need to check if a lead exists first.
4. **Merge Lead Data** — takes the `lead_id` from the RPC response and merges it back into the working data for downstream nodes.
5. **Insert Conversation** — calls `insert_conversation_event()` RPC. Dedup on `event_id` — if this event was already written, returns `was_new: false` and stops.
6. **Was Duplicate?** — if `was_new: false`, short-circuits and returns 200. Safe to retry any event.
7. **Has Phone? → Upsert PhoneMap** — if `phone_e164` was provided, ensures the phone→lead mapping exists.
8. **Notion sync** — queries Notion by `lead_email`. If found, updates the lead page with latest intent/name/company. If not found, creates a new lead page. Either way, creates a new conversation entry in Notion. Notion failure does NOT prevent the 200 response.
9. **Cancel Pending On Inbound** — if `direction=inbound`, finds all `pending` follow_up_queue rows for this lead and pushes their `scheduled_for` forward. This prevents the Dispatcher from sending a follow-up right after a lead has replied.
10. **Cross-Channel?** — if `trigger_cross_channel=true`, inserts one `follow_up_queue` row per channel in `target_channels` with `status=pending` and `scheduled_for=NOW()+30min`.
11. **Return OK** — 200 with `lead_id`, `lead_email`, `lead_created`, `overall_intent`.

**The `event_id` contract:** the caller generates the UUID. Pass the same UUID on retry. The RPC will return `was_new: false` and the workflow exits cleanly. This means you can retry any write without worrying about duplicates. Aimfox sends the same webhook 6 times — the first write succeeds, the next 5 are no-ops.

**When to set `trigger_cross_channel: true`:** only when intent is `interested` or `booking`. Sending cross-channel follow-ups for `no_action` intent leads would be spam.

---

### 6.2 Fetch Cross-Channel Context — The Memory API

**ID:** `UJoDCfkmD3NJHktk` | **Entry:** `POST /webhook/mco-fetch-context`

This is the simplest workflow — 5 nodes — but it's called by nearly every AI agent.

**What it returns:** a `context_block` text string ready to paste into an AI prompt, plus the full lead object (which includes `linkedin_conversation_urn`).

**The node sequence:**

1. **Webhook** — receives `{ lead_id, lead_email, linkedin_profile_url, phone_e164, requesting_channel, max_events }`
2. **Setup & Validate** — requires at least one identifier
3. **Fetch Context RPC** — calls `fetch_lead_context()` with whichever identifier you provided. Returns the lead record + last N conversations.
4. **Format Context Block** — reverses the order (DB returns newest-first, AI reads oldest-first), prepends a channel-awareness header if `requesting_channel` is provided, and formats each event as a readable line.
5. **Return Context** — 200 with `context_block`, `lead`, `event_count`, `overall_intent`

**Important:** the `lead` object in the response always includes `linkedin_conversation_urn`. The Coordinator reads this field to decide whether to DM (URN exists) or add to campaign (URN is null). Always extract this from the Fetch Context response rather than doing a separate leads table lookup.

**If the lead doesn't exist:** returns `{ context_block: "(No prior conversation history found for this lead.)", lead: null, event_count: 0 }`. This is not an error — it's a valid response for a brand new lead.

**Performance:** `lead_id` is the fastest lookup. Pass it whenever you have it.

---

### 6.3 FollowUp Queue Dispatcher — The Heartbeat

**ID:** `3ju6z4oJcWJqskBN` | **Runs:** Schedule every 15 minutes

The Dispatcher has one job: pick up pending queue rows and fire the Coordinator.

**Node sequence:**

1. **Schedule** — fires every 15 minutes
2. **Fetch Pending Rows** — Supabase REST, fetches all `follow_up_queue` rows where `status=pending` AND `scheduled_for <= NOW()`
3. **Split Rows** — Code node, parses `follow_up_context` JSON string and spreads each row's data for downstream nodes
4. **SplitInBatches** — processes one row at a time
5. **Call Coordinator** — `POST /webhook/mco-followup` with full row data (lead_email, lead_id, target_channel, queue_id, plus all LinkedIn identifiers from follow_up_context)
6. **Loop back** — continues until all rows processed

**What the Coordinator receives from the Dispatcher:**

```json
{
  "queue_id": "uuid",
  "lead_id": "uuid",
  "lead_email": "john@acme.com",
  "target_channel": "linkedin",
  "aimfox_account_id": "774180197",
  "conversation_urn": "urn:li:msg:abc123",
  "linkedin_urn": "urn:li:person:AbCdEfGhIj",
  "linkedin_profile_url": "https://linkedin.com/in/johndoe"
}
```

All LinkedIn identifiers come from the `follow_up_context` JSON string that was embedded when the queue row was inserted. The Dispatcher doesn't do any Supabase lookups for these — it reads them directly from the queue row. This is why it's critical that queue row inserts include all needed identifiers in `follow_up_context`.

**If a queue row is stuck:** it means either the Dispatcher didn't run (check schedule) or the Coordinator threw an error (check Coordinator executions). A row that stays `pending` more than 30 minutes after `scheduled_for` is a signal something is wrong.

---

### 6.4 Centralized Follow-Up Coordinator — The Router

**ID:** `KXKcCYRnK4V8v9k7` | **Entry:** `POST /webhook/mco-followup`

The Coordinator is the most complex workflow. It receives a queue row from the Dispatcher, fetches cross-channel context, generates a message, routes to the right channel, sends it, logs it, and marks the queue row sent.

**Node sequence:**

1. **Webhook** — receives payload from Dispatcher
2. **Setup & Validate** — Code node. Requires: `queue_id`, `target_channel`, and at least one of `lead_email`, `lead_id`, `linkedin_profile_url`. Extracts all LinkedIn identifiers. Outputs `FETCH_CONTEXT_URL` and `WRITE_EVENT_URL` constants.
3. **Fetch Context** — `POST /mco-fetch-context`. Gets `context_block` and full lead object including `linkedin_conversation_urn`.
4. **Merge Context** — Code node. Merges Setup output with Context output. Extracts `conversation_urn` from lead record (`lead.linkedin_conversation_urn`) with fallback to Setup payload value. Extracts `lead_id` from lead record. Sets `lead_name`.
5. **Route by Channel** — Switch node:
   - Output 0: `email`
   - Output 1: `linkedin`
   - Output 2: `voice`

#### Email path
6. **Email: Claude Model** — Anthropic credential
7. **Email: Generate Message** — LLM chain with full context in system prompt
8. **Email: Extract Message** — Code node, extracts clean text
9. **Send Email (Gmail)** — from `team@flowticsai.com`
10. → After Send → Log → Mark Queue Sent → Return OK

#### LinkedIn path
6. **LinkedIn: Has Conversation URN?** — IF node (typeVersion 2, loose)
   - Checks `$json.conversation_urn isNotEmpty`

   **YES (existing thread — lead is connected and has a DM history):**
   7. AI generates a reply
   8. **Reply to Existing Conversation** — `POST Aimfox /accounts/:aimfox_account_id/conversations/:conversation_urn` body: `{ message }` — **NO `/messages` suffix** (this is a verified gotcha)
   9. → After Send → Log → Mark Queue Sent → Return OK

   **NO (no connection yet — email-sourced lead without LinkedIn connection):**
   7. **Add to LinkedIn Campaign** — `POST Aimfox /campaigns/6e2feb86-b9c6-4c18-87fa-c5fe5e41682f/audience` body: `{ profile_url }` — **no `/accounts/:id` prefix** (another verified gotcha)
   8. **Log Connection Request** — Write Event logs the connection request attempt
   9. **Mark Queue Skipped** — status set to `skipped` (not `sent` — this was not a message, just a connection request)
   10. **Return OK (Campaign)** — does NOT go through After Send

#### Voice path
6. **Voice: Trigger Call Agent** — `POST /webhook/3adf4681-...` with: `lead_email`, `lead_id`, `phone_e164`, `queue_id`, `target_channel: "voice"`, `trigger_channel`, `follow_up_context`
7. **Voice: Prepare Result** — stamps `generated_message: "Voice call follow-up initiated via Call Agent"`, `send_status: "sent"`
8. → After Send → Log → Mark Queue Sent → Return OK

#### Shared tail (all sending paths)
- **After Send** — Code node. Spreads the send API response. Picks up `generated_message` via try-catch from whichever Extract Message node ran. This is critical — after an HTTP node, `$json` becomes the HTTP response and all prior data is lost. The try-catch rescues `generated_message` before that data disappears.
- **Log to Supabase** — `POST /mco-write-event`. Uses `$json.generated_message` (from After Send). Also passes `linkedin_urn` and `linkedin_profile_url` so Write Event can resolve LinkedIn-only leads that have no email.
- **Mark Queue Sent** — `PATCH Supabase follow_up_queue`, status: `sent`. Headers reference `$('Merge Context').first().json.SUPABASE_KEY` — NOT `$json.SUPABASE_KEY` (which is undefined after HTTP node overwrites `$json`).
- **Return OK** — 200 response

**Why `Mark Queue Skipped` (not `sent`) for campaign adds:** adding a lead to a campaign is a connection request, not a sent message. Marking it `sent` would falsely indicate a conversation happened. The connection request is the start of the funnel — the actual follow-up message comes after the connection is accepted (24h later, via Connection Accepted Handler).

**Why voice is marked `sent` immediately:** the Coordinator only triggers the call. It doesn't know if it was answered. Post Call Analysis handles the outcome — if not answered, it re-queues. The Coordinator's job is done when the call is triggered.

**Why the Call Agent skips `Mark Queue Sent` on the webhook path:** the Coordinator passes `triggered_by_coordinator: true` in the webhook body. The Call Agent's `Skip Mark Queue?` IF node checks this — if true (webhook path), skips its own `Mark Queue Sent` because the Coordinator already owns that step. If false (schedule path), runs `Mark Queue Sent` normally. Without this, the same `queue_id` would be marked `sent` twice, corrupting any future call-count or retry-limit logic.

---

### 6.5 Aimfox Connection Accepted Handler — The Handshake

**ID:** `WTbIAJCZGtppAT91` | **Entry:** `POST /webhook/mco-aimfox-accepted`

Fires when a LinkedIn connection request is accepted. This is the bridge between the campaign path (connection request) and the DM path (ongoing conversation).

**Node sequence:**

1. **Webhook** — receives Aimfox `accepted` event
2. **Extract Fields** — Code node. Parses the real Aimfox payload structure:
   - `account_id` from `event.account.id`
   - `lead_id`, `lead_urn`, `lead_name` from `event.target`
   - `linkedin_profile_url` constructed from `target.public_identifier`
   - Falls back to flat format for test payloads
3. **Get Lead Custom Variables** — `GET Aimfox /accounts/:id/leads/:lead_urn/custom-variables`. Gets `LEAD_EMAIL` if it was stored in Aimfox.
4. **Extract LEAD_EMAIL** — Code node, reads LEAD_EMAIL from custom variables response. `lead_email` may be null — the handler works for LinkedIn-only leads.
5. **Send Thanks Message** — `POST Aimfox /accounts/:id/conversations` body: `{ message: "Hi {lead_name}, great to connect! ...", recipients: [lead_id] }`. Fixed text — not AI-generated. The `lead_id` here is Aimfox's internal lead ID, not the Supabase UUID.
6. **Extract Conversation URN** — Code node. Reads `conversation_urn` from Aimfox response (checks multiple possible field names). Sets `log_event_id` from `raw.id` (the Aimfox webhook payload's top-level `id` field — stable across Aimfox's retry attempts, so Supabase dedup works correctly).
7. **Log Connection Accepted** — `POST /mco-write-event`. Stores the thanks message and writes `linkedin_conversation_urn` to the lead record (via `upsert_lead()` inside Write Event).
8. **Queue LinkedIn Follow-Up** — inserts `follow_up_queue` row with `status=pending`, `scheduled_for=NOW()+24h`, `target_channel=linkedin`. The `trigger_event_id` is set to `log_event_id` (the stable Aimfox event UUID) to satisfy the FK constraint.
9. **Return OK** — 200

**What this enables:** after this handler runs, the lead record has `linkedin_conversation_urn` set. The next time the Coordinator fires for this lead (24h later), it checks for `conversation_urn` in the Fetch Context response, finds it, and takes the DM path instead of the campaign path. The conversation thread continues.

**The `log_event_id` / dedup story:** Aimfox retries webhooks up to 6 times. If we used a random UUID as `event_id`, each retry would create a duplicate conversation row. We use `raw.id` (the stable Aimfox event ID) so only the first webhook write succeeds. `Math.random()` is only the fallback if Aimfox doesn't send an `id` field.

**Note on `require('crypto')`:** the n8n task runner blocks Node's `crypto` module. Don't use it. Use `Math.random()` UUID generation as the fallback instead.

---

### 6.6 Aimfox Reply Agent — LinkedIn Reactive

**ID:** `SPN1NLyHH1LcfViD` | **Entry:** Aimfox webhook (new_reply event)

Fires when a lead sends a LinkedIn message. This is reactive — we always reply when a lead reaches out.

**Gate chain (before any AI):**

1. **Code2 (48h guard)** — drops webhooks older than 48 hours. Aimfox retries webhooks many times, and stale retries shouldn't trigger replies days later.
2. **Google Sheets gate** — looks up the conversation in Google Sheets by conversation URN. If the sheet row is missing or the conversation is closed, stops silently. This is the "are we actively managing this conversation?" check.
3. **If5 / If6** — secondary gate checks based on sheet state.

**The key gate:** `If13` checks whether the lead's reply qualifies for AI processing (i.e., is not a system message or noise). If YES, goes to AI. If NO, goes to Text Classifier for more nuanced routing.

**The AI path (If13 = YES):**

1. **MCO: Fetch Context** — `POST /mco-fetch-context` with `linkedin_profile_url` from the Aimfox webhook sender. Gets cross-channel history.
2. **AI Agent1** — Anthropic. Prompt: lead name + full LinkedIn thread (from Aimfox Thread node) + MCO cross-channel history. System prompt: Nextus ITAD persona, keep reply focused on this LinkedIn thread, do NOT reference other channels even though you have their full history.
3. **Send Reply** — `POST Aimfox /accounts/:id/conversations/:urn` (no `/messages` suffix).
4. → MCO write nodes

**Why Google Sheets (not Supabase) as the gate:** Aimfox Reply Agent was built before the full MCO system, and Google Sheets was used as the conversation state store. It's intentional — Sheets gives you easy manual control over which conversations the agent is actively handling. Supabase is the record of what happened; Sheets is the control plane for this agent.

**Critical failure mode:** if the Google Sheets credential expires, all LinkedIn replies are silently dropped. Check credentials regularly.

**Cross-channel context (added 2026-05-17):** the AI now receives the full MCO history before replying, but is told to use it for context only — not to reference other channels in the LinkedIn reply. This means it knows if the lead has been emailed or called, which informs tone and approach, but the reply reads as a natural LinkedIn conversation.

---

### 6.7 Gmail Reply Agent — Email Reactive

**ID:** `mFBOGdMAsXRKD1Pv` | **Entry:** Gmail trigger on `team@flowticsai.com`

Fires when any email arrives at the inbox. Uses a two-layer gate to ensure it only replies to leads from our campaigns.

**Two scenarios that hit the Gmail trigger:**

- **Scenario 1 — Instantly outbound CC:** Instantly sends an email to the lead and CCs `team@flowticsai.com`. Gmail trigger fires. Email shape: From=Instantly sender, To=lead, Cc=team@flowticsai.com. The lead is in the **To** field.
- **Scenario 2 — Lead replies:** Lead replies to our email. Email shape: From=lead, To=team@flowticsai.com. The lead is in the **From** field.

In both cases we want to reply to the lead. The lead is whoever is in Supabase — regardless of which field they appear in.

**Gate chain:**

1. **Extract & Filter** — parses both `from_email` and `to_email` (outputs both, does not decide `lead_email` yet). Drops: `noreply`/`no-reply` senders, non-replies (no `Re:` prefix or `inReplyTo` header), empty body. Also normalises Gmail trigger header casing — Gmail outputs `From`, `To`, `Subject` capitalised; code reads `email.From || email.from` etc.
2. **Supabase: Check Outbound** — queries `leads?or=(lead_email.eq.{from_email},lead_email.eq.{to_email})&select=lead_id,lead_email,full_name&limit=1`. Checks both FROM and TO. Whichever matches is the lead.
3. **Filter: Our Thread Only** — if Supabase returns empty → drops. If match found → sets `lead_email` and `lead_name` from the Supabase result and passes downstream.

**After the gate:**

4. **MCO: Fetch Context** — gets cross-channel history using the resolved `lead_email`
5. **Merge Context** — merges email fields with context, `lead_email` from Filter result
6. **MCO: Write Inbound** — logs the inbound email against the correct lead
7. **Text Classifier** — classifies intent. If should reply → Reply Agent. Otherwise → No Operation.
8. **Reply Agent** — Anthropic `claude-sonnet-4-20250514`. Prompt: lead name + subject + email body + cross-channel history. System message: use cross-channel history to understand intent only, keep reply focused on this email thread, do NOT mention other channels.
9. **Send a message (Slack)** — human review/approval step
10. **Send Gmail Reply** — sends to `lead_email` (the resolved Supabase match)
11. **MCO: Write Outbound** — logs the sent reply

**Fail-safe behaviour:** if Supabase is unavailable, `Filter: Our Thread Only` checks `Array.isArray(rows) && rows.length > 0` — a non-array error response fails this check and returns `[]`, dropping the email rather than replying blindly.

**Instantly AI integration:** no separate workflow. In every Instantly campaign, set Reply-To = `team@flowticsai.com`. The Instantly workflow must also write the lead to Supabase (`upsert_lead`) at send time — this is the prerequisite for the Gmail Reply Agent to recognise the lead when the CC arrives.

---

### 6.8 Call Agent — Voice Proactive

**ID:** `xE8mFF8HxPaSXNmi` | **Entry:** `POST /webhook/3adf4681-...` — webhook only

Places outbound Retell phone calls. Triggered exclusively by the Coordinator via webhook — one execution per lead. If multiple leads need calling simultaneously, the Coordinator fires multiple webhook requests and n8n runs them as parallel executions. No schedule, no batch loop.

**Entry path:**

```
Coordinator POSTs to webhook
      |
Normalize Webhook Input (parse body fields)
      |
Unified Input (normalise data shape)
      |
[rest of workflow]
```

**Node sequence after Unified Input:**

5. **Fetch Lead Record** — Supabase REST, fetches lead by `lead_id` (always present in Unified Input). Returns `lead_id`, `lead_email`, `full_name`, `company`, `phone_e164`, `overall_intent`. Using `lead_id` means LinkedIn-only leads (no email) are resolved correctly — previously a missing email caused this lookup to return empty, leaving `full_name` and `company` blank for the GPT-4o-mini summary.
6. **POST /mco-fetch-context** — gets last 20 cross-channel conversations for the lead.
7. **Build OpenAI Request** — assembles prompt for GPT-4o-mini.
8. **Summarize Prior Conversation** — OpenAI `gpt-4o-mini`, generates a ≤180-word prior conversation brief. This is what Maya (the Retell agent) reads before the call to know what's been discussed.
9. **Prepare Retell Variables** — Set node, builds dynamic variables for Retell:
   - `previous_conversation_summary` — from OpenAI
   - `lead_email`, `first_name`, `company_name` — from lead record
   - `phone_e164` — from `Fetch Lead Record` with fallback to `Unified Input` (important: NOT from the Loop node, which doesn't execute on the webhook path)
   - `booking_link` — `https://calendly.com/mahfujurrahman511351/30min`
10. **Build Retell Request** — assembles Retell API payload: `agent_id`, `from_number`, `to_number`, `metadata: { queue_id, lead_email, source: "n8n_flowtics_followup" }`. The `queue_id` in metadata is critical — Post Call Analysis needs it to mark the queue row and handle retries.
11. **Retell: Create Phone Call** — POST to Retell API, places the call.
12. **Mark Queue Sent** — PATCH queue row to `status: "sent"`.
13. **Loop** (schedule path) — continues to next lead.

**The `phone_e164` flow (critical):** must flow through `Normalize Webhook Input` → `Unified Input` → `Prepare Retell Variables`. The reference is `$('Fetch Lead Record').first().json.phone_e164 || $('Unified Input').item.json.phone_e164`. The `Fetch Lead Record` result is a flat object (from the `fetch_lead_context()` RPC), not an array — use `json.phone_e164`, not `json[0].phone_e164`.

**What happens after the call:** Retell fires `call_analyzed` to Post Call Analysis when the call ends.

---

### 6.9 Post Call Analysis — Voice Outcome Handler

**ID:** `r8XKHCnL4vju2E4j` | **Entry:** `POST /webhook/9cdd28e8-...`

Receives Retell's post-call webhook, logs the call to Supabase, and handles retries for unanswered calls.

**Node sequence:**

1. **Webhook** — receives Retell `call_analyzed` event
2. **Extract Call Data** — Code node, parses `call_id`, `transcript`, `summary`, `disconnection_reason`, `queue_id` (from Retell metadata), `lead_email`, `duration`
3. **Write Call to Supabase** — `POST /mco-write-event` with `channel: "voice"`, `direction: "outbound"`, `content: summary/transcript`, `intent` from Retell's analysis. Metadata includes `queue_id` and `disconnection_reason` so the conversation row is traceable back to the queue row.
4. **Check Retry** — Code node, checks `disconnection_reason` against unanswered set: `dial_no_answer`, `voicemail`, `dial_failed`, `busy`
5. **Write Call Outcome** — PATCH `follow_up_queue` row with `outcome` field:
   - `answered` — call was picked up
   - `voicemail` — hit voicemail
   - `no_answer` — rang out or busy
   - `failed` — `dial_failed`
   - `neverError: true` — if `queue_id` is missing or PATCH fails, workflow continues
6. **Needs Retry?** — IF node
   - YES → **Re-Queue Voice Call** — inserts new `follow_up_queue` row with `status=pending`, `scheduled_for=NOW()+4h`, `target_channel=voice`
   - NO → stop (answered call, original queue row is already `sent`)

**Why the original queue row is marked `sent` before the call is answered:** the Call Agent marks the queue row `sent` as soon as Retell confirms the call was initiated (not answered). This is correct — the row represents "did we attempt to call?" not "did they answer?". If they don't answer, Post Call Analysis creates a *new* queue row for the retry. The retry is a fresh attempt, not a resurrection of the old row.

**The `queue_id` handoff:** the Call Agent embeds `queue_id` in Retell's call metadata. Retell echoes this back in the `call_analyzed` webhook. Post Call Analysis reads it from `$json.metadata.queue_id`. This is how Post Call Analysis knows which queue row to write the `outcome` back to, and which row to check for re-queuing.

---

### 6.10 Aimfox Responded — Label Only

**ID:** `Zw7iTErdMMJjiM7g` | **Entry:** Aimfox responded webhook

Two nodes. Receives Aimfox's "responded" event (which fires when a lead replies) and applies a label in Aimfox. No Supabase write. No AI.

This is intentional — the Aimfox Reply Agent handles the actual response and any Supabase writes. This workflow just manages the Aimfox label state.

---

## 7. Critical Design Decisions

### Why a single write endpoint?
All 10 workflows call `POST /mco-write-event` instead of writing to Supabase directly. The benefit: lead resolution, intent promotion, Notion sync, phone map, and cross-channel queue creation happen in one place. The cost: an extra HTTP hop. The tradeoff is worth it — it's the difference between consistent data and chaos.

### Why Postgres RPCs instead of raw SQL?
Three reasons: (1) atomic intent promotion — you can't safely promote intent across multiple statements without a transaction; (2) multi-identifier lead resolution — matching a lead by email OR LinkedIn URL OR phone in a single call; (3) dedup on `event_id` — the RPC returns `was_new` so callers know if they're retrying. None of this is possible with raw SQL from n8n.

### Why `queue_id` in Retell metadata?
The Retell call and the Post Call Analysis webhook are connected only through this ID. Retell fires the webhook after the call ends — at that point, n8n has no memory of which queue row triggered the call. By embedding `queue_id` in Retell's metadata, Post Call Analysis can read it back and close the loop.

### Why voice is marked `sent` before we know if it was answered?
Because the queue row represents a dispatch attempt, not a conversation. "Sent" means "the call was triggered." If it wasn't answered, that's a new event (handled by Post Call Analysis re-queuing). This keeps the queue semantics clean.

### Why LinkedIn campaign adds are marked `skipped` (not `sent`)?
Because adding to a campaign is a connection request, not a message. The queue item for "send a LinkedIn follow-up" was satisfied by sending a connection request — the actual follow-up message comes after acceptance. Marking it `sent` would make the conversation log show a message that was never sent.

### Why the Aimfox Reply Agent uses Google Sheets as a gate?
Sheets gives easy manual control over which conversations the bot manages. You can open or close a conversation from a spreadsheet without touching n8n. Supabase is the record of what happened; Sheets is the active management layer for this specific agent.

---

## 8. Lead Identity and the Upsert Pattern

A lead can be identified by five things: `lead_id`, `lead_email`, `linkedin_profile_url`, `linkedin_urn`, `phone_e164`. Any one of them is enough. You'll often have different identifiers depending on where the lead came from:

- Email campaign lead: has `lead_email` only at first
- LinkedIn lead: has `linkedin_profile_url` and `linkedin_urn`, may not have email
- Voice lead: has `phone_e164`, may have email
- Lead from all channels: has all five

The `upsert_lead()` RPC handles this transparently. It tries each identifier in order and merges the record if any match is found. When a LinkedIn-only lead eventually gives you their email, the next call to `upsert_lead()` with both `linkedin_profile_url` and `lead_email` updates the existing record rather than creating a duplicate.

**The merge rule:** if identifier A matches lead 1 and identifier B matches lead 2, the RPC merges them into one record. This handles the case where you've been talking to the same person on two channels without knowing it was the same person.

**Pass all identifiers you have, every time.** Don't withhold identifiers because "we already have them." The RPC is idempotent — passing the same data twice is fine, and new data enriches the record.

---

## 9. Intent Promotion — The One-Way Ratchet

Intent values, in order: `unknown → no_action → not_interested → referral → interested → booking`

The rule: intent never goes down. If a lead is `interested` and their next message is ambiguous, intent stays `interested`. This is enforced atomically in the `upsert_lead()` Postgres RPC using a CASE expression — it only updates if the new intent is higher than the current one.

**Why atomic?** If two events arrive simultaneously for the same lead (race condition), two separate `UPDATE SET intent = X` statements could both read the current value and both write, with the lower-intent one potentially winning if it commits last. The RPC uses a single atomic `UPDATE ... WHERE ... AND (new_intent_rank > current_intent_rank)` — only one can win.

**Never implement intent promotion in n8n Code nodes.** If you write `lead.intent = newIntent` in JavaScript and then update Supabase, you've bypassed the atomicity guarantee. Always go through `upsert_lead()`.

---

## 10. The n8n Data Flow Problem

This is the #1 source of bugs in n8n workflows. After any HTTP node executes, `$json` becomes the HTTP response body. All data that was in `$json` before the HTTP call is gone unless you explicitly saved it.

**Example:** in the Coordinator, after `Reply to Existing Conversation` (Aimfox HTTP call), `$json` is the Aimfox response (`{ status: "ok", message_urn: "..." }`). The `generated_message` that was in `$json` before the call is gone.

**The fix pattern:** use explicit node references instead of `$json` for anything that needs to survive across HTTP nodes:

```js
// WRONG - $json is overwritten after the HTTP call
const key = $json.SUPABASE_KEY;

// CORRECT - reference the specific node where the data lives
const key = $('Merge Context').first().json.SUPABASE_KEY;
```

**The `generated_message` carry-forward pattern (After Send node):**
```js
const d = $input.first().json;
let generated_message = null;
try { generated_message = $('LinkedIn: Extract Message').first().json.generated_message; } catch(e) {}
try { if (!generated_message) generated_message = $('Email: Extract Message').first().json.generated_message; } catch(e) {}
try { if (!generated_message) generated_message = $('Voice: Prepare Result').first().json.generated_message; } catch(e) {}
return [{ json: { ...d, send_status: d.send_status || 'sent', generated_message } }];
```

The try-catch is necessary because only one Extract Message node runs per execution — the others throw "hasn't been executed" and the catch prevents that error from stopping execution.

**The `Mark Queue Sent` header pattern:**
```js
// Headers must reference Merge Context, not $json
apikey: ={{ $('Merge Context').first().json.SUPABASE_KEY }}
Authorization: ={{ 'Bearer ' + $('Merge Context').first().json.SUPABASE_KEY }}
```

**General rule:** if a value needs to survive past an HTTP node, either (a) reference the node where you set it using `$('NodeName').first().json.field`, or (b) use a Code node before the HTTP call to explicitly carry it through in the output.

---

## 11. neverError Nodes — The Silent Failure Zone

Some nodes have `neverError: true`. These nodes will show green (success) in n8n even when the underlying API call failed with a 4xx or 5xx. The error is swallowed.

Always inspect the **output JSON** of these nodes, not just the status colour.

| Workflow | Node | What it calls | Silent failure looks like |
|---|---|---|---|
| Connection Accepted Handler | Send Thanks Message | Aimfox POST /conversations | `{ "status": "fail", "error": { "code": 401 } }` |
| Connection Accepted Handler | Get Lead Custom Variables | Aimfox GET /custom-variables | `{ "status": "fail" }` |
| Coordinator | Reply to Existing Conversation | Aimfox POST /conversations/:urn | `{ "error": "..." }` |
| Coordinator | Add to LinkedIn Campaign | Aimfox POST /campaigns/:id/audience | `{ "error": "..." }` |
| Gmail Reply Agent | Supabase: Check Outbound | Supabase REST | Non-array response — treated as no match |
| Aimfox Reply Agent | MCO: Fetch Context | POST /mco-fetch-context | `context_block` falls back to "(No cross-channel history found.)" |
| Write Event | Insert Conversation | Supabase RPC | Empty array `[]` if dedup blocked |

**Rule:** after any `neverError` node, the next node should check the output. If it doesn't, errors propagate silently through the workflow and the lead doesn't receive the message — but the queue row still gets marked sent.

---

## 12. Debugging Playbook

### Step 1 — Where did it break?

Open the n8n workflow → Executions tab. Every execution shows:
- Status: `success` / `error` / `running`
- Which node failed (highlighted red)
- Exact error message
- Input/output data for every node

**Quick lookup via API:**
```
GET https://n8n-1404.n8n.whiteserverdns.com/api/v1/executions?workflowId=<ID>&limit=10
```

### Step 2 — Check Supabase for the lead

```sql
-- Find the lead
SELECT lead_id, lead_email, linkedin_urn, linkedin_profile_url, phone_e164,
       overall_intent, full_name, company, linkedin_conversation_urn
FROM leads
WHERE lead_email = 'their@email.com'
   OR linkedin_profile_url LIKE '%their-slug%';

-- See all their conversations
SELECT channel, direction, timestamp, content, intent, sender_name
FROM conversations
WHERE lead_id = '<lead_id>'
ORDER BY timestamp DESC LIMIT 20;

-- See their queue rows
SELECT queue_id, target_channel, status, scheduled_for, created_at, follow_up_context
FROM follow_up_queue
WHERE lead_id = '<lead_id>'
ORDER BY created_at DESC;
```

### Step 3 — Failure signatures

| Symptom | Likely cause |
|---|---|
| Lead never got a reply | Reply Agent didn't trigger, or intent check blocked it |
| Queue row stuck as `pending` | Dispatcher not running, or Coordinator threw an error |
| Queue row shows `failed` | Coordinator missing required field |
| Thanks message not sent after connection | `neverError` on Send Thanks swallowed Aimfox 401 — check HTTP response in that node |
| Duplicate message sent | Missing dedup — `event_id` must be stable across retries |
| Context block is empty | Lead resolved by wrong identifier; check `lead_id` in Fetch Context call |
| Voice call fired but no transcript | Retell webhook URL misconfigured |
| LinkedIn DM not sent | Check Aimfox token; check `Reply to Existing Conversation` output JSON (neverError) |
| Email not sent | Check Anthropic API key; check Gmail OAuth token |
| `conversation_urn` null on lead | Connection Accepted Handler failed at Send Thanks step (Aimfox error) |

### Re-triggering a failed event

**Option A — Re-send the webhook** (safest):
```bash
curl -X POST https://n8n-1404.n8n.whiteserverdns.com/webhook/mco-write-event \
  -H "Content-Type: application/json" \
  -d '{ ...original payload... }'
```
`event_id` dedup makes this safe — if already written, returns `{ "status": "already_written" }`.

**Option B — Fix the queue row:**
```sql
UPDATE follow_up_queue
SET scheduled_for = NOW(), status = 'pending'
WHERE queue_id = '<queue_id>';
```
The Dispatcher will pick it up within 15 minutes.

**Option C — n8n UI retry:** open the execution → click Retry. Replays with the same input.

### Queue health queries

```sql
-- Overall status breakdown
SELECT status, COUNT(*) FROM follow_up_queue GROUP BY status ORDER BY count DESC;

-- Overdue rows (should be 0 — if not, Dispatcher or Coordinator has a problem)
SELECT q.queue_id, l.lead_email, q.target_channel, q.scheduled_for
FROM follow_up_queue q JOIN leads l ON q.lead_id = l.lead_id
WHERE q.status = 'pending' AND q.scheduled_for < NOW() - INTERVAL '30 minutes'
ORDER BY q.scheduled_for ASC;

-- Leads with LinkedIn URN but no conversation_urn (will use campaign path instead of DM)
SELECT lead_email, full_name FROM leads
WHERE linkedin_urn IS NOT NULL AND linkedin_conversation_urn IS NULL;

-- Recent activity (last 24h)
SELECT l.full_name, c.channel, c.direction, c.timestamp, c.intent
FROM conversations c JOIN leads l ON c.lead_id = l.lead_id
WHERE c.timestamp > NOW() - INTERVAL '24 hours'
ORDER BY c.timestamp DESC LIMIT 20;
```

---

## 13. How to Add a New Channel

If you add SMS, WhatsApp, or any other channel, follow this pattern:

**1. Write path** — call `POST /mco-write-event` with `channel: "sms"` (or your new channel name). No changes to Write Event needed — it accepts any channel string.

**2. Read path** — call `POST /mco-fetch-context` before generating any AI reply. Inject `context_block` into the AI prompt.

**3. Dispatch path** — if you want the Coordinator to send proactive messages on the new channel:
- Add the new `target_channel` value (e.g., `"sms"`) to the queue row inserts
- Add a new output to the Coordinator's `Route by Channel` Switch node
- Build the sending path (generate AI message → send via new channel API → After Send → Log → Mark Queue Sent)

**4. Reactive path** — if you want to respond to inbound messages on the new channel:
- Build a trigger (webhook from the channel provider, or a polling node)
- Add a gate (equivalent to Supabase: Check Outbound for email, or Google Sheets for LinkedIn)
- Call Fetch Context, merge, run AI, send reply, call Write Event twice (inbound + outbound)
- Cancel pending queue items on inbound (same as Write Event's Cancel Pending On Inbound does)

**What you don't need to change:** Write Event, Fetch Context, Dispatcher. These are channel-agnostic.

---

## 14. Known Constraints and Edge Cases

**LinkedIn-only leads (no email):** fully supported. `lead_email` is nullable throughout the system. Write Event accepts `lead_id`, `linkedin_profile_url`, or `linkedin_urn` as the only identifier. The Coordinator logs to Supabase with `linkedin_urn` and `linkedin_profile_url` for these leads.

**`conversation_urn` null after thanks message failure:** if Aimfox returns an error on the Send Thanks Message call (neverError swallows it), `conversation_urn` is never written to the lead record. The Coordinator will add them to the campaign again on the next run instead of DMing them. This is degraded but not broken — the next campaign addition is idempotent in Aimfox.

**Aimfox campaign state:** campaign `6e2feb86-b9c6-4c18-87fa-c5fe5e41682f` accepts audience adds in ACTIVE, PAUSED, DONE, and CREATED states. Only GROUP MESSAGE and EVENT MESSAGE types block adds.

**Dispatcher fires every 15 min:** a pending queue row will be picked up within 15 minutes of `scheduled_for`. If it's been >30 minutes and the row is still pending, there's a problem.

**Call Agent dedup gap:** if the Dispatcher fires twice for the same voice queue row before the Call Agent can mark it `sent`, two Retell calls go out. Low probability given the 4h call schedule and fast execution, but possible in theory.

**Google Sheets credential expiry (Aimfox Reply Agent):** all LinkedIn replies silently stop. There's no alert. Check credential health periodically.

**Aimfox webhook retries:** Aimfox retries webhooks up to 6 times. The `event_id` dedup in Write Event (using the stable Aimfox event UUID from `raw.id`) prevents duplicate conversation rows. The 48h guard in the Aimfox Reply Agent prevents stale retries from triggering replies.

**Instantly AI integration:** no n8n workflow. Set Reply-To = `team@flowticsai.com` in every Instantly campaign. Gmail trigger handles all inbound from Instantly leads automatically.

---

## 15. What's Not Built Yet

| Feature | Notes |
|---|---|
| Retool / monitoring dashboard | Currently need to run SQL queries in Supabase directly to see queue health, lead intent breakdown, recent activity |
| SMS channel | No workflow. Would need a provider (Twilio, etc.) and a new Coordinator path |
| WhatsApp channel | Same as SMS |
| Lead scoring / prioritisation | Dispatcher treats all pending rows equally. No prioritisation by intent level or recency |
| Unsubscribe handling | No explicit unsubscribe flow. If a lead says "stop emailing me," a human needs to update their intent to `not_interested` in Supabase |
| A/B testing for AI prompts | Single fixed prompt per channel. No testing infrastructure |
| Alerting | No Slack/email alerts when queue rows go overdue or workflow error rates spike |

---

## 16. Bugs Fixed and What They Taught Us

Every bug in this section taught us something about n8n, Aimfox, or the system's design. They're documented here so we don't repeat them.

### The `$json` overwrite problem (multiple workflows)
**Bug:** nodes after HTTP calls used `$json.SUPABASE_KEY`, `$json.generated_message`, `$json.queue_id`, etc. After an HTTP call, `$json` is the HTTP response — all prior data is gone.  
**Fix:** use explicit node references: `$('Merge Context').first().json.SUPABASE_KEY`.  
**Lesson:** never rely on `$json` after an HTTP node for data that was set before the HTTP call. Always anchor to a named node.

### Aimfox `/messages` suffix (Coordinator)
**Bug:** `Reply to Existing Conversation` URL was `POST /accounts/:id/conversations/:urn/messages`. Aimfox returned "Cannot POST".  
**Fix:** correct endpoint is `POST /accounts/:id/conversations/:urn` (no `/messages` suffix).  
**Lesson:** verified live. The suffix does not exist in Aimfox API v2.

### Aimfox campaign URL with `/accounts/:id` prefix (Coordinator)
**Bug:** `Add to LinkedIn Campaign` URL was `POST /accounts/:id/campaigns/:campaign_id/audience`. Aimfox returned 404.  
**Fix:** correct endpoint is `POST /campaigns/:id/audience` (no `/accounts/:id` prefix). Body is `{ profile_url }` not `{ leads: [urn] }`.  
**Lesson:** Aimfox has two different URL patterns — account-scoped endpoints use `/accounts/:id/...` prefix; campaign-level endpoints don't.

### LinkedIn IF node typeVersion (Coordinator)
**Bug:** `Has Conversation URN?` was using IF node typeVersion 1 (strict equality). An empty string `""` was not equal to `null`, so leads with empty conversation_urn strings fell through to the wrong branch.  
**Fix:** deleted and recreated with typeVersion 2 (loose comparison). `isNotEmpty` on `""` now correctly returns false.  
**Lesson:** n8n IF node typeVersion 2 uses loose comparisons. Always use typeVersion 2 for empty/not-empty checks.

### `require('crypto')` blocked (Connection Accepted Handler)
**Bug:** `Extract Conversation URN` used `require('crypto').randomUUID()`. n8n's task runner blocks the `crypto` module. Every real execution crashed.  
**Fix:** replaced with a `Math.random()`-based UUID generator.  
**Lesson:** n8n Code nodes cannot use Node.js built-in modules that are blocked by the task runner. Use pure JavaScript alternatives.

### Aimfox payload structure mismatch (Connection Accepted Handler)
**Bug:** `Extract Fields` read `body.account_id` and `body.lead`. The real Aimfox accepted webhook uses `event.account.id` and `event.target`. The handler worked in testing with a flat test payload but crashed on every real Aimfox webhook.  
**Fix:** updated to read `event.account.id` and `event.target`, with fallback to flat format for test payloads.  
**Lesson:** always test with the real external payload shape, not a simplified test payload.

### `trigger_event_id` FK constraint (Connection Accepted Handler)
**Bug:** Queue LinkedIn Follow-Up was generating a random UUID for `trigger_event_id`. This UUID had no corresponding row in `conversations.event_id`, violating the FK constraint.  
**Fix:** use `log_event_id` (from the stable Aimfox event UUID) as `trigger_event_id` — the same UUID used in Write Event, which creates the corresponding conversation row.  
**Lesson:** FK constraints require the referenced row to exist before you insert the referencing row. Write the conversation first, then insert the queue row using that event's ID.

### `json[0]?.phone_e164` array access (Call Agent)
**Bug:** `Prepare Retell Variables` used `$('Fetch Lead Record').first().json[0]?.phone_e164`. The `fetch_lead_context()` RPC returns a flat object, not an array — `json[0]` is always undefined.  
**Fix:** `$('Fetch Lead Record').first().json.phone_e164`.  
**Lesson:** Supabase REST queries return arrays; Supabase RPCs return flat objects. Know which you're calling.

### Loop node reference on webhook path (Call Agent)
**Bug:** `Prepare Retell Variables` referenced `$('Loop One Lead at a Time').item.json.phone_e164`. The Loop node doesn't execute when the Call Agent is triggered via webhook (only on the schedule path). Every webhook-triggered call threw "Loop One Lead at a Time hasn't been executed".  
**Fix:** changed reference to `$('Unified Input').item.json.phone_e164`.  
**Lesson:** in workflows with two entry paths, any node that can run from either path must reference the convergence point (`Unified Input`), not a node that only exists on one path.

### Dispatcher extra nodes (Dispatcher simplification)
**Bug:** Dispatcher had nodes `LinkedIn?`, `Fetch LinkedIn Meta`, `Merge LinkedIn Meta` that queried Supabase separately for `aimfox_account_id` and `conversation_urn`. These fields were already in `follow_up_context` — the extra nodes were fetching data that was already there.  
**Fix:** removed all three nodes. `Split Rows` now parses `follow_up_context` directly.  
**Lesson:** `follow_up_context` is the right place to store channel-specific data at queue-insert time. Don't add lookup nodes to the Dispatcher — it should be a thin forwarder.

### `Log Connection Request` missing data after HTTP nodes (Coordinator)
**Bug:** `Log Connection Request` and `Mark Queue Skipped` used `$json` references for `queue_id`, `lead_email`, etc. By the time these nodes ran, `$json` was the Aimfox campaign add HTTP response. All lead data was undefined.  
**Fix:** changed all references to `$('Merge Context').first().json.*`.  
**Lesson:** same `$json` overwrite problem. When in doubt, always reference a named node.

### Write Event not accepting `lead_id` as identifier
**Bug:** `Setup & Validate` checked for `lead_email`, `linkedin_profile_url`, `linkedin_urn`, `phone_e164` — but not `lead_id`. LinkedIn-only leads passed only `lead_id` from the Coordinator → Write Event returned 400 "At least one identifier required".  
**Fix:** added `hasLeadId` check to the validation block.  
**Lesson:** `lead_id` is a valid identifier. Any new validation that checks "do we know who this lead is?" should include all five identifier types.

---

### Double `Mark Queue Sent` on voice webhook path (Call Agent + Coordinator)
**Bug:** when the Coordinator triggered the Call Agent via webhook, both workflows marked the same `queue_id` as `sent` — the Coordinator immediately after firing the webhook, and the Call Agent after the Retell call was placed. Harmless now but would corrupt any future call-count or retry-limit logic.  
**Fix:** Coordinator passes `triggered_by_coordinator: true` in the webhook body. Call Agent's new `Skip Mark Queue?` IF node checks this flag — if true, skips its own `Mark Queue Sent`. If false (direct webhook call), runs it as a fallback.  
**Lesson:** when two workflows share ownership of the same state write, decide explicitly which one owns it and use a flag to prevent the other from double-writing.

### Call Agent 4h schedule trigger (Call Agent)
**Bug:** the Call Agent had a schedule trigger that fired every 4 hours, fetching all pending voice rows from the queue and calling them in batch. This conflicted with the Coordinator's on-demand webhook trigger — the same lead could be called by the schedule and the webhook simultaneously.  
**Fix:** removed the schedule trigger, `Supabase: Get Pending Voice Follow-Ups`, and `Loop One Lead at a Time` nodes entirely. The Call Agent is now webhook-only. Multiple simultaneous calls are handled by n8n running parallel executions.  
**Lesson:** don't give a workflow two entry paths if one of them can conflict with the other. Explicit is better than scheduled.

### `Fetch Lead Record` used `lead_email` as lookup (Call Agent)
**Bug:** `Fetch Lead Record` queried Supabase by `lead_email`. For LinkedIn-only leads (no email), this returned empty — `full_name` and `company` were blank, so GPT-4o-mini generated a call summary with no context about who the lead was. Maya called the lead without knowing their name or company.  
**Fix:** changed query filter from `lead_email=eq.{email}` to `lead_id=eq.{lead_id}`. `lead_id` is always present in `Unified Input`.  
**Lesson:** always use `lead_id` as the primary lookup identifier. It's always available and works for every lead type. `lead_email` is optional and can be null.

### No call outcome recorded on `follow_up_queue` (Post Call Analysis)
**Bug:** after a call, the original `follow_up_queue` row stayed frozen at `status=sent` with no record of what happened. There was no link between the queue row and the call outcome — you couldn't query "how many calls were answered?" from the queue table.  
**Fix:** added `outcome` column (`text`, nullable) to `follow_up_queue`. Post Call Analysis now PATCHes the original queue row with `outcome=answered/voicemail/no_answer/failed` after every call. Also added `queue_id` and `disconnection_reason` to the conversation row metadata so the two records are traceable to each other.  
**Lesson:** status and outcome are different things. `status` tracks dispatch state (pending → sent). `outcome` tracks what actually happened after dispatch. They belong in separate columns.

---

*End of The Book of MCO. If something in the system changes, update this document. If something breaks in a new way, add it to Section 16. The goal is that anyone reading this from scratch should be able to understand, operate, debug, and extend the system without asking anyone.*
