# MCO System — Connections Reference
### Every component, URL, credential, and data flow in one place
**Last updated:** 2026-05-17 (synced to live n8n state)

---

## n8n Instance

**URL:** `https://n8n-1404.n8n.whiteserverdns.com`

---

## Webhook Entry Points

| Workflow | n8n ID | Webhook URL | Trigger |
|---|---|---|---|
| Write Conversation Event | `5qOo5YzrPnW8Uj9g` | `POST /webhook/mco-write-event` | Called by all other workflows after any interaction |
| Fetch Cross-Channel Context | `UJoDCfkmD3NJHktk` | `POST /webhook/mco-fetch-context` | Called before any AI reply generation |
| Centralized Follow-Up Coordinator | `KXKcCYRnK4V8v9k7` | `POST /webhook/mco-followup` | Called by Dispatcher every 15 min |
| Aimfox Connection Accepted Handler | `WTbIAJCZGtppAT91` | `POST /webhook/mco-aimfox-accepted` | Aimfox `accepted` event webhook |
| Flowtics AI Call Agent — MCO | `xE8mFF8HxPaSXNmi` | `POST /webhook/3adf4681-721b-452e-94b3-5618887a15c4` | On-demand + schedule (every 4h) |
| Post Call Analysis — MCO | `r8XKHCnL4vju2E4j` | `POST /webhook/9cdd28e8-7cfd-4765-a623-cda2d1b9f7a7` | Retell `call_analyzed` webhook |
| Aimfox Data Fetching MCO | `o9l5PClHznNgZIK8` | `POST /webhook/3e030494-4719-491d-8291-2c9523e71c3d` | On-demand |
| MCO - Aimfox Responded | `Zw7iTErdMMJjiM7g` | Aimfox responded webhook | Aimfox `responded` event |
| Aimfox Nextus AI Reply Agent — MCO | `SPN1NLyHH1LcfViD` | Aimfox webhook (new reply received) | Inbound LinkedIn message |
| MCO - Gmail Reply Agent | `mFBOGdMAsXRKD1Pv` | Gmail trigger | New email to `team@flowticsai.com` |
| FollowUp Queue Dispatcher | `3ju6z4oJcWJqskBN` | *(schedule only — no webhook)* | Every 15 minutes |

---

## Supabase

**Project URL:** `https://hkqssbomrcbtfbdowtgj.supabase.co`  
**Auth:** Service role key (stored inside every MCO workflow's Setup/Config node and in `.env`)

### Tables

| Table | Purpose | Primary Key | Key Columns |
|---|---|---|---|
| `leads` | One row per lead. Canonical identity + intent | `lead_id` UUID | `lead_email`, `linkedin_profile_url`, `linkedin_urn`, `phone_e164` (all nullable unique), `linkedin_conversation_urn`, `overall_intent`, `full_name`, `company` |
| `conversations` | One row per message, any channel | `event_id` (dedup key) | `lead_id` FK, `channel`, `direction`, `content`, `metadata` JSONB, `timestamp` |
| `follow_up_queue` | Scheduled cross-channel follow-ups | `queue_id` UUID | `lead_id` FK, `lead_email`, `target_channel`, `status` (pending/sent/skipped/failed), `scheduled_for`, `follow_up_context` |
| `phone_map` | Maps phone numbers to leads | `phone_e164` | `lead_id` FK, `source`, `confidence` |

### Postgres RPCs

| Function | Called by | What it does |
|---|---|---|
| `upsert_lead()` | Write Event | Creates or updates lead. Atomic intent promotion (never demotes). Multi-identifier resolution: matches by any of lead_id, email, linkedin_url, linkedin_urn, phone. Returns `lead_id`. |
| `insert_conversation_event()` | Write Event | Inserts one conversation row. Dedup on `event_id` — safe to retry. Accepts `lead_id` (preferred) or `lead_email`. |
| `fetch_lead_context()` | Fetch Context | Returns lead record + last N conversations DESC. Accepts lead_id, lead_email, linkedin_profile_url, or phone_e164. |

**Intent promotion order:** `unknown → no_action → not_interested → referral → interested → booking`  
Never implement intent promotion in n8n Code nodes — always goes through `upsert_lead()` RPC.

---

## Workflow Details

---

### 1. Write Conversation Event (`5qOo5YzrPnW8Uj9g`)
**Entry:** `POST /webhook/mco-write-event`  
**Node count:** 21 | **Status:** Active

**What it does:**
1. Setup & Validate — requires event_id, channel, direction, content, timestamp, and at least one lead identifier
2. Upsert Lead — RPC, multi-identifier resolution, atomic intent promotion
3. Merge Lead Data — merges RPC response back into payload
4. Insert Conversation — RPC, dedup on event_id
5. Check Insert Result / Was Duplicate? — stops early if duplicate
6. Has Phone? → Upsert PhoneMap — if phone_e164 present
7. Notion: Query Lead → Extract Lead ID → IF: Notion Lead Found? → Update Lead OR Create Lead → Create Conversation — keeps Notion CRM in sync
8. Cancel Pending On Inbound — if direction=inbound, reschedules any pending follow-up queue items so we don't follow up when the lead just responded
9. Cross-Channel? → Build Queue Rows → Insert FollowUpQueue — if `trigger_cross_channel: true`
10. Return OK

**Required payload fields:**
- `event_id` — UUID, dedup key
- `channel` — `email` | `linkedin` | `voice` | `sms`
- `direction` — `inbound` | `outbound`
- `content` — message text or transcript
- `timestamp` — ISO 8601 UTC
- At least one of: `lead_id`, `lead_email`, `linkedin_profile_url`, `linkedin_urn`, `phone_e164`

**Notable optional fields:**
- `linkedin_conversation_urn` — stored on lead record; Coordinator uses this to DM vs. add to campaign
- `trigger_cross_channel: true` + `target_channels: ["email"]` — queues follow-ups
- `intent` — fed into upsert_lead for intent promotion

---

### 2. Fetch Cross-Channel Context (`UJoDCfkmD3NJHktk`)
**Entry:** `POST /webhook/mco-fetch-context`  
**Node count:** 5 | **Status:** Active

**What it does:**
1. Setup & Validate — requires at least one lead identifier
2. Fetch Context RPC — calls `fetch_lead_context()`, returns lead record + last N conversations
3. Format Context Block — builds plain-text context block with timeline, channel icons, intent labels
4. Return Context — 200 with `context_block`, `lead` (full record including `linkedin_conversation_urn`), `event_count`, `overall_intent`

**Required payload:** at least one of `lead_id` (preferred), `lead_email`, `linkedin_profile_url`, `phone_e164`

---

### 3. FollowUp Queue Dispatcher (`3ju6z4oJcWJqskBN`)
**Entry:** Schedule, every 15 minutes  
**Node count:** 6 | **Status:** Active

**What it does:**
1. Config — Supabase URL + key + Coordinator URL
2. Fetch Pending Queue — `GET /follow_up_queue` where `status=pending` and `scheduled_for <= now()`
3. Split Rows — one item per queue row; parses `follow_up_context` JSON to extract `aimfox_account_id`, `conversation_urn`, `linkedin_urn`, `linkedin_profile_url`, `phone_e164` directly from the queue row
4. Call Coordinator — `POST /mco-followup` with `queue_id`, `lead_id`, `lead_email`, `target_channel`, `aimfox_account_id`, `conversation_urn` and all other fields
5. Log Result — logs dispatch outcome to console

---

### 4. Centralized Follow-Up Coordinator (`KXKcCYRnK4V8v9k7`)
**Entry:** `POST /webhook/mco-followup`  
**Node count:** 26 | **Status:** Active

**What it does:**
1. Setup & Validate — requires at least one identifier (lead_id, lead_email, or linkedin_profile_url) + queue_id + target_channel
2. Fetch Context — `POST /mco-fetch-context` with lead_id or lead_email
3. Merge Context — extracts `conversation_urn` from lead record (`lead.linkedin_conversation_urn`), merges all identifiers
4. Route by Channel (Switch) — email / linkedin / voice

**Email path:**
- Claude AI (Email: Generate Message via LLM chain)
- Email: Extract Message
- Send Email (Gmail) — from `team@flowticsai.com`
- After Send → Log to Supabase → Mark Queue Sent → Return OK

**LinkedIn path:**
- `LinkedIn: Has Conversation URN?` (IF, typeVersion 2, loose) — checks `conversation_urn` from Merge Context
  - **YES** (existing conversation — lead is connected, has DM thread):
    - LinkedIn: Generate Message (Claude AI)
    - LinkedIn: Extract Message
    - Reply to Existing Conversation — `POST Aimfox /accounts/:id/conversations/:urn/messages`
    - After Send → Log to Supabase → Mark Queue Sent → Return OK
  - **NO** (not yet connected — email-sourced lead):
    - Add to LinkedIn Campaign — `POST Aimfox /accounts/:id/campaigns/39a3ee2f-8474-4e9a-b498-81d14e319788/leads`
    - Log Connection Request — `POST /mco-write-event` (channel=linkedin, content="Connection request sent via Aimfox campaign", intent=no_action)
    - Mark Queue Skipped — `PATCH follow_up_queue` status=skipped
    - Return OK (Campaign) — separate 200, does NOT go through After Send

**Voice path:**
- Voice: Trigger Call Agent — `POST /webhook/3adf4681-...` with `lead_email`, `lead_id`, `phone_e164`, `queue_id`, `follow_up_context`
- Voice: Prepare Result — stamps `generated_message = "Voice call follow-up initiated via Call Agent"`
- After Send → Log to Supabase → Mark Queue Sent → Return OK

**Shared tail (email + LinkedIn DM + voice):**
- After Send — normalises `send_status`
- Log to Supabase — `POST /mco-write-event` with `lead_id`, `lead_email`, channel, content
- Mark Queue Sent — `PATCH follow_up_queue` status=sent (or skipped if send_status=skipped)
- Return OK

---

### 5. Aimfox Connection Accepted Handler (`WTbIAJCZGtppAT91`)
**Entry:** `POST /webhook/mco-aimfox-accepted`  
**Node count:** 9 | **Status:** Active

**What it does:**
1. Webhook — Aimfox `accepted` event
2. Extract Fields — pulls `account_id`, `lead_id` (Aimfox ID), `lead_urn`, `linkedin_profile_url` from payload
3. Get Lead Custom Variables — `GET Aimfox /accounts/:id/leads/:urn/custom-variables`
4. Extract LEAD_EMAIL — reads LEAD_EMAIL and lead_name from Aimfox custom variables
5. Send Thanks Message — `POST Aimfox /accounts/:id/conversations` with fixed message: `"Hi {lead_name}, great to connect! Looking forward to learning more about what you're working on."`
6. Extract Conversation URN — reads `conversation_urn` from Aimfox response (tries multiple shapes)
7. Log Connection Accepted — `POST /mco-write-event` with `linkedin_conversation_urn` (stored on lead record for future DMs), `lead_email`, `lead_urn`, `linkedin_profile_url`, intent=no_action
8. Queue LinkedIn Follow-Up — `POST Supabase /follow_up_queue` with `scheduled_for = NOW() + 24h`, reads `aimfox_account_id` and `conversation_urn` from Extract Conversation URN node (not from HTTP response)
9. Return OK

**Key behaviours:**
- The thanks message is fixed text — no AI generation
- `conversation_urn` is stored on the lead record via Write Event so the Coordinator always finds it
- The 24h queued follow-up is picked up by the Dispatcher → Coordinator takes the LinkedIn DM path (has URN → Reply to Existing Conversation)
- `lead_email` may be null for LinkedIn-only leads — handler works for both

---

### 6. Aimfox Nextus AI Reply Agent — MCO (`SPN1NLyHH1LcfViD`)
**Entry:** Aimfox webhook (new reply received)  
**Node count:** 37 | **Status:** Active

**What it does:** Handles inbound LinkedIn replies from leads. Uses Google Sheets to track conversation state and decide whether to reply (only replies if we are not the last sender). Uses AI (Anthropic Claude + Google Gemini) to generate responses. On interested/returning leads, writes to MCO via `mco-write-event` (nodes: `MCO: Build Seed B1`, `MCO: Write Returning`, `MCO: Write First Interested`). Uses Clay webhook for lead enrichment. Sends Slack notifications on key events.

**Design principle:** This workflow is reactive — it only fires when a lead sends a message. It uses Google Sheets (not Supabase) to track "should we reply?" state. MCO write events are only called for leads who show interest, seeding the Supabase store with their history.

---

### 7. MCO - Gmail Reply Agent (`mFBOGdMAsXRKD1Pv`)
**Entry:** Gmail trigger — new email to `team@flowticsai.com`  
**Node count:** 18 | **Status:** Active

**What it does:**
1. Gmail Trigger — fires on new inbound email
2. Extract & Filter — parses lead email from `From:` header, filters out system/bounce emails
3. MCO: Fetch Context — `POST /mco-fetch-context` with lead_email
4. Merge Context — merges extracted fields with context
5. MCO: Write Inbound — `POST /mco-write-event` to log the inbound email
6. Supabase: Check Outbound — checks if we already sent an outbound email (dedup guard)
7. Text Classifier — classifies intent (interested, not_interested, etc.)
8. Reply Agent (AI Agent with Anthropic) — generates reply using cross-channel context
9. Send Gmail Reply — sends from `team@flowticsai.com`
10. MCO: Write Outbound — `POST /mco-write-event` to log the sent reply
11. Slack notification on key events

---

### 8. Flowtics AI Call Agent — MCO (`xE8mFF8HxPaSXNmi`)
**Entry:** Schedule (every 4h) + `POST /webhook/3adf4681-721b-452e-94b3-5618887a15c4`  
**Node count:** 14 | **Status:** Active

**Schedule path:**
1. Schedule Trigger (every 4h)
2. Supabase: Get Pending Voice Follow-Ups — reads `follow_up_queue` where `target_channel=voice`, `status=pending`
3. Loop One Lead at a Time
4. Unified Input — normalises data

**Webhook path:**
1. Webhook trigger
2. Normalize Webhook Input — reads `lead_email`, `queue_id`, `lead_id`, `phone_e164`, `follow_up_context`
3. Unified Input

**Shared path (both):**
4. Fetch Lead Record — Supabase `leads` table lookup
5. POST /mco-fetch-context — full cross-channel context
6. Build OpenAI Request — builds GPT-4o-mini prompt
7. Summarize Prior Conversation — OpenAI `gpt-4o-mini`, produces ≤180-word brief
8. Prepare Retell Variables (Set node)
9. Build Retell Request — `queue_id` passed in `metadata.queue_id` and dynamic variables include `lead_email`, `first_name`, `company_name`, `booking_link`, `previous_conversation_summary`. Skips leads with no `phone_e164`.
10. Retell: Create Phone Call — `POST https://api.retellai.com/v2/create-phone-call`
11. Mark Queue Sent — `PATCH follow_up_queue` status=sent

---

### 9. Post Call Analysis — MCO (`r8XKHCnL4vju2E4j`)
**Entry:** `POST /webhook/9cdd28e8-7cfd-4765-a623-cda2d1b9f7a7`  
**Node count:** 7 | **Status:** Active

**What it does:**
1. Webhook1 — Retell fires this after every call with `call_analyzed` event
2. Filter — only processes `call_analyzed` events
3. Build MCO Write Payload — extracts from Retell payload:
   - `lead_email` from `retell_llm_dynamic_variables.lead_email`
   - `phone_e164` from `call.to_number`
   - `content` from `call_analysis.call_summary` or transcript
   - `intent` from `call_analysis.custom_analysis_data.qualified_status`
   - `disconnection_reason` from `call.disconnection_reason`
   - `queue_id` from `call.metadata.queue_id`
   - `was_answered` — false if disconnection_reason is `dial_no_answer`, `voicemail`, `dial_failed`, or `busy`
4. POST /mco-write-event — logs call summary/transcript to Supabase
5. Check Retry — reads Build MCO Write Payload output, determines `needs_retry`
6. Needs Retry? (IF) — branches on `needs_retry`
   - **YES (not answered):** Re-Queue Voice Call — `POST Supabase /follow_up_queue` with `scheduled_for = NOW() + 4h`, target_channel=voice, status=pending
   - **NO (answered):** execution ends

---

### 10. MCO - Aimfox Responded (`Zw7iTErdMMJjiM7g`)
**Entry:** Aimfox `responded` event webhook  
**Node count:** 2 | **Status:** Active

**What it does:** Marks the lead with an Aimfox label when they respond to a campaign message. This is Aimfox-internal state tracking only — it does not write to Supabase. The Aimfox Reply Agent handles the actual reply logic for these leads.

---

## External Services

### Aimfox
**API Base:** `https://api.aimfox.com/api/v2`  
**Token:** `Bearer 8e65df8c-3fe2-4ecf-bf05-8261ea85464b` (hardcoded in workflow nodes)  
**Campaign ID (new connections):** `39a3ee2f-8474-4e9a-b498-81d14e319788`

| Endpoint | Used by | Purpose |
|---|---|---|
| `GET /accounts/:id/leads/:urn/custom-variables` | Connection Accepted Handler | Read LEAD_EMAIL + lead_name |
| `POST /accounts/:id/conversations` | Connection Accepted Handler | Send thanks message (opens new thread) |
| `POST /accounts/:id/conversations/:urn/messages` | Coordinator (LinkedIn DM path) | Reply to existing conversation thread |
| `POST /accounts/:id/campaigns/:campaign_id/leads` | Coordinator (no URN path) | Add lead to connection request campaign |
| `PUT /leads/:id/labels/:label_id` | Aimfox Responded | Mark lead as having responded |
| `GET /accounts/:id/conversations/:urn` | Reply Agent | Fetch conversation thread for context |

### Gmail
**Account:** `team@flowticsai.com`  
**Sender name:** `Flowtics AI`  
**n8n credential:** `Gmail account` (ID `IC6TPjXMVxTyn2R9`)  
**Used by:** Coordinator (email path outbound), Gmail Reply Agent (inbound trigger + reply)

### Anthropic (Claude)
**n8n credential:** `Anthropic account 2` (ID `WEpOCYlwQtWIw3jK`)  
**Model:** `claude-sonnet-4-5-20250929`  
**Used by:** Coordinator (Email: Generate Message, LinkedIn: Generate Message), Gmail Reply Agent (Reply Agent AI)

### Retell AI
**Outbound number:** `+15722124790`  
**Agent:** `agent_ff863b1414049444c174360809` (Maya — Flowtics AI)  
**Booking link:** `https://calendly.com/mahfujurrahman511351/30min`  
**Dynamic variables per call:** `first_name`, `company_name`, `booking_link`, `previous_conversation_summary`, `lead_email`  
**Metadata per call:** `queue_id`, `lead_email`, `source: "n8n_flowtics_followup"`  
**Post-call webhook:** fires `call_analyzed` → Post Call Analysis workflow

### OpenAI
**Model:** `gpt-4o-mini`  
**Used by:** Call Agent — summarises cross-channel context into ≤180-word brief before Retell call

### Notion (CRM)
**API Base:** `https://api.notion.com/v1`  
**Leads database:** `362dc227-748f-8184-892a-c6f8f3151b07`  
**Token:** hardcoded in Notion nodes inside Write Event workflow  
**Used by:** Write Conversation Event (queries by email → updates or creates lead page + conversation entry)

### Google Sheets
**n8n credential:** `Google Sheets account 2`  
**Used by:** Aimfox Reply Agent (conversation state tracking — should we reply?), Aimfox Data Fetching MCO

### Slack
**n8n credential:** `Slack account 3` (ID `EczWlUTWwagmArbp`)  
**Used by:** Gmail Reply Agent, Aimfox Reply Agent (internal notifications)

### Clay
**Webhook:** `https://api.clay.com/v3/sources/webhook/pull-in-data-from-a-webhook-82271fb1-214c-40d4-b428-828feded561a`  
**Used by:** Aimfox Reply Agent (lead enrichment)

---

## How Workflows Call Each Other

```
Aimfox (connection accepted)
  -> POST /mco-aimfox-accepted  (Connection Accepted Handler)
      -> GET Aimfox custom variables
      -> POST Aimfox start conversation  (fixed thanks message)
      -> POST /mco-write-event           (logs message, stores conversation_urn on lead)
      -> POST Supabase follow_up_queue   (24h follow-up scheduled)

Schedule (every 15 min)
  -> Dispatcher reads follow_up_queue
      -> POST /mco-followup  (Coordinator)
          -> POST /mco-fetch-context
          -> Email path: Anthropic + Gmail + POST /mco-write-event + PATCH queue
          -> LinkedIn DM path: Anthropic + Aimfox reply + POST /mco-write-event + PATCH queue
          -> LinkedIn no-URN path: Aimfox campaign add + POST /mco-write-event + PATCH queue skipped
          -> Voice path: POST Call Agent webhook + POST /mco-write-event + PATCH queue

Aimfox (new LinkedIn reply received)
  -> Aimfox Reply Agent
      -> Google Sheets check (should we reply?)
      -> Anthropic/Gemini generate reply
      -> POST Aimfox send reply
      -> [if interested] POST /mco-write-event

Gmail (new email to team@flowticsai.com)
  -> Gmail Reply Agent
      -> POST /mco-fetch-context
      -> POST /mco-write-event  (inbound)
      -> Anthropic generate reply
      -> Gmail send reply
      -> POST /mco-write-event  (outbound)

Schedule (every 4h) OR POST /webhook/3adf4681-...
  -> Call Agent
      -> Supabase: get pending voice queue
      -> POST /mco-fetch-context
      -> OpenAI summarize context
      -> Retell: create phone call (queue_id in metadata)

Retell (call_analyzed event)
  -> POST /webhook/9cdd28e8-...  (Post Call Analysis)
      -> POST /mco-write-event   (logs transcript/summary)
      -> if not answered: POST Supabase follow_up_queue  (retry in 4h)

Aimfox (responded event)
  -> Aimfox Responded
      -> PUT Aimfox label  (Aimfox-internal only, no Supabase write)
```

---

## Credentials Reference

| Service | Type | Stored in | Used by |
|---|---|---|---|
| Supabase service role key | In Setup/Config nodes + `.env` | All MCO workflows | All |
| Notion API token | Hardcoded in Notion nodes | Write Event | Write Event |
| Aimfox API token | Hardcoded in nodes | Coordinator, Connection Accepted Handler, Reply Agent | LinkedIn paths |
| Anthropic (Claude) | n8n credential `Anthropic account 2` (ID `WEpOCYlwQtWIw3jK`) | Coordinator, Gmail Reply Agent | AI generation |
| Gmail OAuth2 | n8n credential `Gmail account` (ID `IC6TPjXMVxTyn2R9`) | Coordinator, Gmail Reply Agent | Email send |
| Google Sheets | n8n credential `Google Sheets account 2` | Aimfox Reply Agent, Data Fetching | Sheets read/write |
| Slack | n8n credential `Slack account 3` (ID `EczWlUTWwagmArbp`) | Gmail Reply Agent, Aimfox Reply Agent | Notifications |
| OpenAI | Hardcoded in Call Agent Summarize node | Call Agent | Context brief |
| Retell | Hardcoded in Call Agent Build Request node | Call Agent | Outbound calls |

---

## What's Live vs. Not Built

| Scenario | Status |
|---|---|
| Lead replies on LinkedIn → AI replies → logged to Supabase (when interested) | Live |
| Lead marks interested on LinkedIn → email follow-up queued | Live |
| Dispatcher fires every 15 min → Coordinator sends email from team@flowticsai.com | Live |
| Connection accepted → fixed thanks message sent → 24h follow-up queued | Live |
| Lead replies to follow-up email → AI replies → logged | Live |
| Post-call analysis triggered via Retell → logged to Supabase | Live |
| Unanswered voice calls → automatically re-queued 4h later | Live |
| Outbound call agent → schedule (every 4h) + on-demand webhook | Live |
| Aimfox responded event → Aimfox label update | Live (by design, Supabase write not needed) |
| Aimfox data fetched → written to Google Sheets | Live |
| Retool dashboard | Not built |
