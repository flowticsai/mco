# MCO — Multi-Channel Outreach System
**Full System Documentation**
**Date:** 2026-05-16
**Instance:** n8n-1404.n8n.whiteserverdns.com

---

## 1. What MCO Is

MCO is a unified outreach automation system that coordinates LinkedIn, email, voice, and SMS interactions across a single shared memory store. Every agent in the system — whether it's replying to a LinkedIn message, sending an email follow-up, or placing a phone call — reads from and writes to the same database before acting. This means no agent ever repeats what another already said, and every reply is informed by the full cross-channel history of that lead.

The core problem MCO solves is channel amnesia. Without it, a lead might get a LinkedIn reply that ignores the email they already responded to, or a voice call that re-introduces a product they already expressed interest in. MCO eliminates that by making cross-channel context available to every agent at the moment it needs it.

---

## 2. Architecture — The WAT Framework

MCO is built on three layers:

**Layer 1 — Workflows (Instructions)**
Markdown SOPs in `workflows/` that define what to do, when, and how. These are the operating manuals for the system.

**Layer 2 — Agents (Decision-Making)**
n8n workflows that read the SOPs, call the right tools in sequence, handle failures, and coordinate between services.

**Layer 3 — Tools (Execution)**
Python scripts in `tools/` and HTTP API calls that do the deterministic work — database writes, API calls, data transformations.

The separation matters because AI handles reasoning unreliably at scale. By keeping probabilistic decisions (Claude generating a reply, Gemini classifying intent) separate from deterministic execution (writing to Supabase, calling Retell), the system stays reliable even as it grows.

---

## 3. Infrastructure

### 3.1 Supabase Database
**Project URL:** `https://hkqssbomrcbtfbdowtgj.supabase.co`
**Auth:** Service role key (never the anon key — service role bypasses RLS for server-side writes)

Supabase is the single source of truth for the entire system. Every channel writes here. Every agent reads from here before responding.

---

#### Table: `leads`
One row per lead. The canonical identity record.

| Column | Type | Purpose |
|--------|------|---------|
| `lead_email` | text (PK) | Primary key. For LinkedIn leads with no known email, uses the format `urn_{public_identifier}@linkedin.placeholder` |
| `full_name` | text | Lead's full name |
| `company` | text | Company name |
| `linkedin_urn` | text | LinkedIn URN identifier |
| `linkedin_profile_url` | text | Full LinkedIn profile URL (`https://linkedin.com/in/...`) |
| `phone_e164` | text | Phone number in E.164 format (`+15551234567`). Required for voice calls |
| `first_channel` | text | The first channel this lead was seen on (`linkedin`, `email`, `voice`, `sms`) |
| `first_seen_at` | timestamptz | When the lead first appeared in the system |
| `last_activity_at` | timestamptz | Timestamp of most recent interaction across any channel |
| `last_active_channel` | text | Which channel had the most recent interaction |
| `monday_item_id` | text | The Monday.com item ID for this lead on board 18399476470 |
| `clay_enriched` | boolean | Whether Clay has enriched this lead's data |
| `overall_intent` | text | The highest intent level ever reached. Never demotes. Values: `unknown → no_action → not_interested → referral → already_have_contract → interested → booking` |
| `overall_intent_updated_at` | timestamptz | When intent last changed |
| `aimfox_campaign` | text | Which Aimfox campaign this lead belongs to |
| `email_sender_inbox` | text | Which Gmail inbox was used to email this lead |
| `notes` | text | Free-form notes |
| `created_at` | timestamptz | Row creation timestamp |

**Current data:** 3 leads (all test data — `direct.test@example.com`, `mco-test-lead@example.com`, `e2e.test@example.com`)

**Key behaviour:** Lead records are upserted on every write event via the Postgres RPC function `upsert_lead()`. If the lead exists, only fields that have changed are updated. `overall_intent` never demotes — if a lead was `interested` and a new event comes in with `no_action`, the intent stays `interested`.

---

#### Table: `conversations`
Every single interaction across every channel. The complete cross-channel timeline.

| Column | Type | Purpose |
|--------|------|---------|
| `event_id` | text (PK) | Deduplication key. Safe to retry writes — duplicate event_id is ignored |
| `lead_email` | text (FK → leads) | Which lead this interaction belongs to |
| `timestamp` | timestamptz | When the interaction happened (from the source system, not write time) |
| `channel` | text | `linkedin`, `email`, `voice`, `sms` |
| `direction` | text | `inbound` (lead to us) or `outbound` (us to lead) |
| `content` | text | The message body, transcript, or summary |
| `content_type` | text | `message`, `transcript`, or `summary` |
| `sender_name` | text | Name of who sent the message |
| `intent` | text | Intent classification for this specific event |
| `metadata_json` | jsonb | Arbitrary metadata (call_id, duration_ms, recording_url, agent_id, etc.) |
| `workflow_execution_id` | text | n8n execution ID for traceability |
| `written_at` | timestamptz | When this row was written to Supabase |

**Current data:** 10 rows — LinkedIn messages, email outbound, and 4 voice call entries from Retell

**Key behaviour:** The `event_id` deduplication means the write endpoint can be called multiple times for the same event without double-writing. This is critical because n8n retries on timeout.

---

#### Table: `follow_up_queue`
Queued cross-channel follow-ups waiting to be dispatched.

| Column | Type | Purpose |
|--------|------|---------|
| `queue_id` | uuid (PK) | Unique identifier |
| `lead_email` | text (FK → leads) | Which lead to follow up with |
| `trigger_channel` | text | Which channel triggered this follow-up (e.g. `linkedin`) |
| `target_channel` | text | Which channel to send the follow-up on (`email`, `voice`, `linkedin`, `sms`) |
| `trigger_event_id` | text | The conversation event that triggered this queue entry |
| `scheduled_for` | timestamptz | When this follow-up should be sent (dispatchers check `<= now()`) |
| `status` | text | `pending` or `sent` |
| `follow_up_context` | text | Context string passed to the Coordinator to personalise the message |
| `sent_at` | timestamptz | When it was dispatched |
| `created_at` | timestamptz | When the row was created |

**Current data:** 3 rows — 2 stale pending rows from May 4 tests (email + sms for `mco-test-lead@example.com`), 1 voice row for `e2e.test@example.com`

**Note:** The 2 stale rows from May 4 should be cleared: `DELETE FROM follow_up_queue WHERE created_at < '2026-05-10' AND status = 'pending'`

**How rows are created:** When a Write Conversation Event call includes `trigger_cross_channel: true` and `target_channels: ['email', 'voice']`, the workflow inserts one queue row per channel with `scheduled_for = now() + 30 minutes`.

---

#### Table: `phone_map`
Maps phone numbers to lead emails. Used by voice/SMS channels to resolve who called.

| Column | Type | Purpose |
|--------|------|---------|
| `phone_e164` | text (PK) | Phone number in E.164 format |
| `lead_email` | text (FK → leads) | Resolved lead identity |
| `source` | text | Where this mapping came from (`retell`, `clay`, `manual`) |
| `confidence` | text | `high`, `medium`, or `low` |

**Current data:** 0 rows — no real voice traffic yet

**How it gets populated:** When a Write Conversation Event call includes a `phone_e164` field, a phone_map row is upserted automatically.

---

### 3.2 n8n Self-Hosted Instance
**URL:** `https://n8n-1404.n8n.whiteserverdns.com`
**API Version:** v1

All workflows run on this instance. It replaced the previous cloud instance (`nextus.app.n8n.cloud`). All webhook URLs and internal references have been updated to point to this instance.

**Credentials configured in n8n:**
- `gmailOAuth2` — Gmail account for sending/receiving emails
- `anthropicApi` — Claude (Sonnet) for reply generation
- `googlePalmApi` — Gemini for email classification
- `slackApi` — Slack for human approval messages

---

### 3.3 GitHub Repository
**URL:** `https://github.com/flowticsai/mco`
**Branch:** `main`

All n8n workflow JSONs are exported here. All hardcoded secrets are replaced with `REDACTED_*` placeholders before committing. The `.env` file and `.tmp/` directory are gitignored and never committed.

**Files in `n8n_workflows/`:**
- `MCO_Write_Conversation_Event.json`
- `MCO_Fetch_Cross_Channel_Context.json`
- `MCO_FollowUp_Queue_Dispatcher.json`
- `MCO_Centralized_FollowUp_Coordinator.json`
- `MCO_Aimfox_Connection_Accepted_Handler.json`
- `Aimfox_Nextus_AI_Reply_Agent_MCO.json`
- `MCO_Gmail_Reply_Agent.json`
- `Flowtics_AI_Call_Agent_MCO.json`
- `Post_Call_Analysis_MCO.json`
- `Aimfox_Data_Fetching_MCO.json`
- `MCO_Aimfox_Responded.json`

---

### 3.4 External Services

| Service | Role | Key Details |
|---------|------|-------------|
| **Aimfox** | LinkedIn automation | Sends LinkedIn messages, manages campaigns, fires webhooks on new replies and connection accepts |
| **Retell AI** | Voice calls | Agent `agent_ff863b1414049444c174360809`, from number `+15722124790`, booking link `calendly.com/mahfujurrahman511351/30min` |
| **Monday.com** | CRM board | Board ID `18399476470`. Every interaction posts an update. Columns: last_active_channel, overall_intent, first_channel, phone_e164 |
| **Notion** | Lead dashboard | Database `362dc227-748f-8184-892a-c6f8f3151b07`. One page per lead, one entry per conversation |
| **Slack** | Human approval | Channel `#linkedin_notifications` (`C0B465KKGKU`). Gmail replies require approval before sending |
| **Clay** | Data enrichment | Referral webhook configured. Used for phone → email resolution when a caller isn't in phone_map |
| **OpenAI** | Pre-call summarisation | gpt-4o-mini. Condenses cross-channel context into a 180-word brief before Retell places a call |
| **Google Sheets** | Lead data log | Aimfox Data Fetching workflow appends lead data here |

---

## 4. Complete A-Z Flow

This is the full lifecycle of a lead through the MCO system, from first LinkedIn reply to multi-channel follow-up.

```
[LEAD REPLIES ON LINKEDIN]
         │
         ▼
Aimfox fires webhook → Aimfox Nextus AI Reply Agent — MCO
         │
         ├─ AI reads thread, classifies intent
         ├─ Generates reply, sends it via Aimfox
         │
         ├─ [EVERY REPLY — returning lead]
         │   MCO: Write Returning
         │   → Checks Supabase: lead EXISTS?
         │       YES → Write inbound + outbound to Supabase
         │       NO  → Do nothing (not our tracked lead yet)
         │
         └─ [LEAD MARKED INTERESTED — new lead]
             MCO: Write First Interested
             → Checks Supabase: lead EXISTS?
                 YES → Do nothing (returning lead, already handled above)
                 NO  → 1. Write full historical thread (seed from Build Seed B1)
                        2. Write current inbound (intent=interested,
                           trigger_cross_channel=true,
                           target_channels=['email','voice'])
                        3. Write our outbound reply
                        4. → MCO Write Conversation Event fires
```

```
[MCO WRITE CONVERSATION EVENT — triggered by any channel]
         │
         ▼
Webhook receives payload
         │
         ├─ Setup & Validate
         │   Validates required fields: event_id, lead_email, channel,
         │   direction, content, timestamp
         │   Validates channel ∈ {email, linkedin, voice, sms}
         │   Validates direction ∈ {inbound, outbound}
         │   Validates intent value
         │
         ├─ Upsert Lead (Postgres RPC: upsert_lead)
         │   Creates lead if new, updates if returning
         │   Intent never demotes
         │
         ├─ Merge Lead Data
         │   Pulls monday_item_id and overall_intent from RPC response
         │
         ├─ Insert Conversation
         │   Inserts row into conversations table
         │   If duplicate event_id → returns "already written", stops
         │
         ├─ Has Phone? → Upsert PhoneMap (if phone_e164 in payload)
         │
         ├─ Format Monday Update → Create/Update Monday.com item
         │   Every interaction posts a timestamped update to board 18399476470
         │
         ├─ Notion: Query Lead (by lead_email in leads database)
         │   → IF lead found → Notion: Update Lead (intent, last channel, last activity)
         │   → IF lead not found → Notion: Create Lead (new page with full profile)
         │   → Notion: Create Conversation (new entry in conversations DB)
         │
         └─ Cross-Channel? (if trigger_cross_channel=true)
             → Build Queue Rows
               One row per target_channel, scheduled_for = now + 30 min
             → Insert FollowUpQueue
             → Return OK
```

```
[FOLLOW-UP QUEUE DISPATCHER — runs every 15 minutes]
         │
         ▼
Fetches follow_up_queue WHERE status='pending' AND scheduled_for <= now()
         │
         ├─ Split rows, process one at a time
         │
         ├─ [target_channel = linkedin]
         │   Fetch LinkedIn metadata from Aimfox
         │   → Call Centralized Follow-Up Coordinator
         │
         └─ [target_channel = email / other]
             → Call Centralized Follow-Up Coordinator directly
```

```
[CENTRALIZED FOLLOW-UP COORDINATOR — called by Dispatcher]
         │
         ▼
Receives queued follow-up item
         │
         ├─ Fetch Cross-Channel Context (MCO Fetch Context webhook)
         │   Returns full timeline of all interactions for this lead
         │
         ├─ Route by Channel
         │
         ├─ [EMAIL]
         │   Claude generates personalised email using cross-channel context
         │   → Send via Gmail (md.imranhanik@gmail.com)
         │   → Log outbound to Supabase (MCO Write Event)
         │   → Mark queue row as sent
         │
         └─ [LINKEDIN]
             Claude generates personalised LinkedIn message
             → Has profile URL?
                 YES → Add to Aimfox campaign → message sent via LinkedIn
                 NO  → Skip (log reason)
             → Log outbound to Supabase
             → Mark queue row as sent
```

```
[LEAD REPLIES TO OUR EMAIL]
         │
         ▼
Gmail Trigger fires → MCO Gmail Reply Agent
         │
         ├─ Extract & Filter
         │   Skips: non-reply subjects (no "Re:"), noreply senders, empty body
         │
         ├─ Supabase: Check Outbound
         │   Looks for existing outbound email to this lead_email
         │   If none found → not our thread → stops silently
         │
         ├─ MCO: Fetch Context
         │   Gets full cross-channel history
         │
         ├─ MCO: Write Inbound
         │   Logs the lead's email reply to Supabase
         │
         ├─ Text Classifier (Gemini)
         │   Needs reply? → Reply Agent
         │   No reply needed? → No Operation, stops
         │
         ├─ Reply Agent (Claude + Knowledgebase)
         │   Generates reply using full cross-channel context
         │
         ├─ Slack: sendAndWait → #linkedin_notifications
         │   Human approves or disapproves
         │
         ├─ [Approved] → Send Gmail Reply (same thread via threadId)
         │               → MCO: Write Outbound (logs sent reply)
         │
         └─ [Disapproved] → stops silently
```

```
[LINKEDIN CONNECTION ACCEPTED]
         │
         ▼
Aimfox fires webhook → MCO Aimfox Connection Accepted Handler
         │
         ├─ Extract Fields from Aimfox payload
         │
         ├─ Get Lead Custom Variables (LEAD_EMAIL from Aimfox)
         │   No LEAD_EMAIL? → Skip → Return OK
         │
         ├─ MCO: Fetch Context
         │   Gets cross-channel history for this lead
         │
         ├─ Generate Message (Claude)
         │   Personalised opening message avoiding repeating anything
         │   already said on other channels
         │
         ├─ Send via Aimfox: Start Conversation
         │
         └─ Log to Supabase (MCO Write Event, channel=linkedin, direction=outbound)
```

```
[VOICE FOLLOW-UP — Flowtics AI Call Agent, runs every 4 hours]
         │
         ▼
Fetch follow_up_queue WHERE target_channel='voice' AND status='pending'
AND scheduled_for <= now() LIMIT 25
         │
         ├─ Loop one lead at a time
         │
         ├─ Fetch Lead Record from Supabase
         │   Gets: full_name, company, phone_e164, overall_intent
         │
         ├─ phone_e164 missing? → SKIP (phone guard) — return []
         │
         ├─ MCO: Fetch Context
         │   Gets full cross-channel history
         │
         ├─ Build OpenAI Request
         │   Prepares prompt for gpt-4o-mini
         │
         ├─ Summarize Prior Conversation (OpenAI gpt-4o-mini)
         │   Condenses context into ≤180-word brief covering:
         │   who the lead is, what they're interested in, pain points,
         │   objections, commitments made, last channel + recency
         │
         ├─ Prepare Retell Variables
         │   Sets: first_name, company_name, phone_e164, lead_email,
         │   previous_conversation_summary, queue_id
         │
         ├─ Build Retell Request
         │   from_number: +15722124790
         │   to_number: lead's phone_e164
         │   override_agent_id: agent_ff863b1414049444c174360809
         │   dynamic_variables: first_name, company_name, booking_link,
         │                       previous_conversation_summary, lead_email
         │
         ├─ Retell: Create Phone Call (POST /v2/create-phone-call)
         │
         └─ Mark Queue Sent (PATCH follow_up_queue status='sent')
```

```
[AFTER THE CALL ENDS — Post Call Analysis MCO]
         │
         ▼
Retell fires post-call webhook →
https://n8n-1404.n8n.whiteserverdns.com/webhook/9cdd28e8-7cfd-4765-a623-cda2d1b9f7a7
         │
         ├─ Filter: event == 'call_analyzed' only
         │   (Retell sends multiple events — only process the final analysis)
         │
         ├─ Build MCO Write Payload
         │   Extracts from Retell payload:
         │   - lead_email from retell_llm_dynamic_variables.lead_email
         │   - event_id: deterministic UUID from call_id
         │   - content: call_analysis.call_summary or transcript
         │   - intent: qualified_status=true → 'interested', else 'unknown'
         │   - phone_e164: call.to_number (validated E.164)
         │   - sender_name: 'Maya (Flowtics AI)'
         │   - metadata: call_id, agent_id, qualified_status, recording_url
         │
         └─ POST /mco-write-event
             Logs the call to Supabase conversations table
             Updates lead record (phone_e164, last_active_channel=voice)
             Posts update to Monday.com
             Updates Notion lead page
```

---

## 5. Workflow Reference — All 11 Workflows

### 5.1 MCO - Write Conversation Event
**ID:** `5qOo5YzrPnW8Uj9g` | **Status:** ACTIVE | **Nodes:** 20
**Webhook:** `POST https://n8n-1404.n8n.whiteserverdns.com/webhook/mco-write-event`

The single write path for the entire system. Every channel, every agent, every direction calls this one endpoint to record interactions.

**Required payload fields:**
```json
{
  "event_id": "unique-dedup-key",
  "lead_email": "lead@example.com",
  "channel": "linkedin | email | voice | sms",
  "direction": "inbound | outbound",
  "content": "message text or transcript",
  "timestamp": "2026-05-16T10:00:00Z"
}
```

**Optional fields:**
```json
{
  "intent": "interested",
  "sender_name": "John Smith",
  "full_name": "John Smith",
  "company": "Acme Corp",
  "phone_e164": "+15551234567",
  "linkedin_profile_url": "https://linkedin.com/in/johnsmith",
  "content_type": "message | transcript | summary",
  "trigger_cross_channel": true,
  "target_channels": ["email", "voice"],
  "follow_up_context": "Lead replied interested on LinkedIn"
}
```

**Node flow:**
1. `Webhook` — receives POST
2. `Setup & Validate` — validates all fields, normalises phone
3. `Upsert Lead` — RPC call to Postgres `upsert_lead()` function
4. `Merge Lead Data` — merges RPC response (monday_item_id, overall_intent)
5. `Insert Conversation` — inserts into conversations table
6. `Check Insert Result` — was it a new row or duplicate?
7. `Was Duplicate?` — if duplicate → Return Already Written, stops
8. `Has Phone?` — if phone present → `Upsert PhoneMap`
9. `Format Monday Update` — builds formatted update text
10. `Create Monday Item` — creates or updates Monday.com item
11. `Extract Monday ID` — pulls item ID from response
12. `Has Monday Item?` — posts update if item exists
13. `Notion: Query Lead` — searches Notion leads database by email
14. `Notion: Extract Lead ID` — parses query response, builds update props
15. `IF: Notion Lead Found?` — branches on whether lead page exists
16. `Notion: Update Lead` OR `Notion: Create Lead` — upserts lead page
17. `Notion: Create Conversation` — appends conversation entry to Notion
18. `Cross-Channel?` — if trigger_cross_channel=true → queue follow-ups
19. `Build Queue Rows` — creates one row per target_channel
20. `Insert FollowUpQueue` — inserts into follow_up_queue table

---

### 5.2 MCO - Fetch Cross-Channel Context
**ID:** `UJoDCfkmD3NJHktk` | **Status:** ACTIVE | **Nodes:** 5
**Webhook:** `POST https://n8n-1404.n8n.whiteserverdns.com/webhook/mco-fetch-context`

Called by every AI agent before generating any reply. Returns the complete cross-channel history for a lead in a formatted text block.

**Required payload:**
```json
{ "lead_email": "lead@example.com" }
```

**Response:**
```json
{
  "context_block": "=== Cross-Channel Conversation History ===\nLead: John Smith...",
  "lead": { "full_name": "...", "overall_intent": "interested", "last_active_channel": "linkedin" },
  "event_count": 7,
  "overall_intent": "interested"
}
```

**Node flow:**
1. `Webhook` — receives POST
2. `Setup & Validate` — extracts and validates lead_email
3. `Fetch Context RPC` — calls Postgres RPC `get_lead_context(lead_email)`
4. `Format Context Block` — builds human-readable text block for AI consumption
5. `Return Context` — responds with structured JSON

---

### 5.3 MCO - FollowUp Queue Dispatcher
**ID:** `3ju6z4oJcWJqskBN` | **Status:** ACTIVE | **Nodes:** 9
**Trigger:** Cron every 15 minutes

Polls `follow_up_queue` for pending items whose `scheduled_for` has passed and routes them to the Centralized Coordinator.

**Node flow:**
1. `Every 15 Minutes` — cron trigger
2. `Config` — sets base URLs and credentials
3. `Fetch Pending Queue` — `SELECT * FROM follow_up_queue WHERE status='pending' AND scheduled_for <= now()`
4. `Split Rows` — processes one item at a time
5. `LinkedIn?` — branches on target_channel
6. `[LinkedIn]` → `Fetch LinkedIn Meta` → `Merge LinkedIn Meta` → `Call Coordinator`
7. `[Email/Other]` → `Call Coordinator` directly
8. `Log Result` — logs dispatch outcome

---

### 5.4 MCO - Centralized Follow-Up Coordinator
**ID:** `KXKcCYRnK4V8v9k7` | **Status:** ACTIVE | **Nodes:** 19
**Webhook:** Internal (called by Dispatcher only)

Receives a queued follow-up, fetches full context, generates a personalised message using Claude, sends it on the right channel, and marks the queue item as sent.

**Node flow:**
1. `Webhook` — receives from Dispatcher
2. `Setup & Validate` — extracts lead_email, target_channel, context
3. `Fetch Context` — calls MCO Fetch Context webhook
4. `Merge Context` — merges queue data with context response
5. `Route by Channel` — Switch node on target_channel
6. **Email path:**
   - `Email: Claude Model` — Anthropic Claude
   - `Email: Generate Message` — AI Agent generates email
   - `Email: Extract Message` — parses reply from AI
   - `Send Email (Gmail)` — sends via md.imranhanik@gmail.com
7. **LinkedIn path:**
   - `LinkedIn: Claude Model` — Anthropic Claude
   - `LinkedIn: Generate Message` — AI Agent generates message
   - `LinkedIn: Extract Message` — parses reply
   - `LinkedIn: Has Profile URL?` — checks if profile URL exists
   - `Add to LinkedIn Campaign` (Aimfox) OR `LinkedIn: Skip (No URL)`
8. `After Send` — merges both paths
9. `Log to Supabase` — calls MCO Write Event (direction=outbound)
10. `Mark Queue Sent` — PATCH follow_up_queue status='sent'
11. `Return OK`

---

### 5.5 MCO - Aimfox Connection Accepted Handler
**ID:** `WTbIAJCZGtppAT91` | **Status:** ACTIVE | **Nodes:** 15
**Webhook:** Aimfox connection accepted event

Fires when a LinkedIn connection request is accepted. Reads the lead's email from Aimfox custom variables, fetches cross-channel context, generates a personalised opening message, and sends it.

**Node flow:**
1. `Webhook` — Aimfox fires this when connection accepted
2. `Extract Fields` — pulls profile data from payload
3. `Get Lead Custom Variables` — fetches Aimfox custom vars for this lead
4. `Extract LEAD_EMAIL` — reads `LEAD_EMAIL` from custom variables
5. `Has Email?` — if no LEAD_EMAIL → `Skip → Return OK`
6. `Fetch Context` — calls MCO Fetch Context
7. `Merge Context` — combines lead data with context
8. `LinkedIn: Claude Model` — Anthropic Claude
9. `Generate Message` — crafts personalised opening message avoiding repeats
10. `Extract Message` — parses AI output
11. `Send via Start Conversation` — Aimfox API call
12. `Log to Supabase` — calls MCO Write Event (channel=linkedin, direction=outbound)
13. `Return OK`

**Why LEAD_EMAIL matters:** LinkedIn leads arrive as profile URNs, not emails. Aimfox custom variables is the bridge that links a LinkedIn identity to a known email address in MCO.

---

### 5.6 Aimfox Nextus AI Reply Agent — MCO
**ID:** `SPN1NLyHH1LcfViD` | **Status:** ACTIVE | **Nodes:** 37
**Trigger:** Aimfox webhook (new LinkedIn message received)

The main LinkedIn reply agent. Receives every new LinkedIn reply, classifies intent, generates an AI response, sends it, and writes to Supabase according to whether the lead is new or returning.

**MCO integration logic (critical):**

After `Send Reply`, the workflow checks Supabase:
- **Lead EXISTS** → `MCO: Write Returning` — writes inbound + outbound to Supabase. Does NOT trigger cross-channel (already in system).
- **Lead NOT in Supabase** → does nothing here.

After `Mark Interested`:
- **Lead NOT in Supabase** → `MCO: Write First Interested` — writes the entire historical thread (seed), then the interested inbound event with `trigger_cross_channel=true, target_channels=['email','voice']`, then the outbound reply. This queues both an email and a voice follow-up.
- **Lead EXISTS** → does nothing (handled by Write Returning above).

**Intent → Action mapping:**
| Intent | Action |
|--------|--------|
| `interested` | Write First Interested → email + voice follow-up queued |
| `not_interested` | No MCO write |
| `referral` | Slack notification + AI Agent5 (referral handling) |
| `booking` | Slack notification |
| `no_action` | Write Returning only |

**Build Seed B1:** Always runs on every reply, builds a formatted payload of the full historical thread. Output is only used when Write First Interested fires (new interested lead). For returning leads, it runs but output is discarded. Negligible cost.

---

### 5.7 MCO - Gmail Reply Agent
**ID:** `mFBOGdMAsXRKD1Pv` | **Status:** ACTIVE | **Nodes:** 18
**Trigger:** Gmail — new email received in connected inbox

Monitors the Gmail inbox for replies to emails sent by the Coordinator. Verifies the email is part of a thread we started, fetches context, classifies, generates reply, gets human approval via Slack, then sends.

**Node flow:**
1. `Gmail Trigger` — fires on every new email
2. `Extract & Filter` — parses from address, skips: non-replies, noreply senders, empty body
3. `Supabase: Check Outbound` — queries conversations for existing outbound email to this lead
4. `Filter: Our Thread Only` — if no outbound record found → stops (not our thread)
5. `MCO: Fetch Context` — gets full cross-channel history
6. `Merge Context` — combines email data with context block
7. `MCO: Write Inbound` — logs the lead's reply to Supabase
8. `Text Classifier (Gemini)` — needs reply? or no reply needed?
9. `[No reply needed]` → `No Operation`
10. `[Needs reply]` → `Reply Agent (Claude + Knowledgebase)`
11. `Edit Fields` — formats draft for Slack
12. `Send a message (Slack sendAndWait)` — posts to `#linkedin_notifications`, waits for human decision
13. `Switch` — Approve or Disapprove
14. `[Approve]` → `Send Gmail Reply` (uses threadId for proper threading) → `MCO: Write Outbound`
15. `[Disapprove]` → stops silently

**Credentials:** Gmail OAuth2, Anthropic, Google Gemini (Gemini for classification, Claude for reply generation), Slack

---

### 5.8 Flowtics AI Call Agent - MCO
**ID:** `xE8mFF8HxPaSXNmi` | **Status:** ACTIVE | **Nodes:** 12
**Trigger:** Cron every 4 hours

The outbound voice caller. Polls for pending voice follow-ups, enriches each lead with cross-channel context, summarises the history for the voice agent, and places the call via Retell.

**Node flow:**
1. `Schedule Trigger` — every 4 hours
2. `Supabase: Get Pending Voice Follow-Ups` — fetches follow_up_queue WHERE target_channel='voice' AND status='pending' AND scheduled_for <= now() LIMIT 25
3. `Loop One Lead at a Time` — SplitInBatches (1 at a time)
4. `Fetch Lead Record` — GET leads WHERE lead_email = queue.lead_email (gets phone, name, company)
5. `POST /mco-fetch-context` — fetches full cross-channel history
6. `Build OpenAI Request` — formats context into OpenAI chat prompt
7. `Summarize Prior Conversation` — POST to OpenAI gpt-4o-mini, returns ≤180-word brief
8. `Prepare Retell Variables` — sets: first_name, company_name, phone_e164, lead_email, previous_conversation_summary, queue_id
9. `Build Retell Request` — assembles Retell API body. **Phone guard:** if phone_e164 is empty → returns [] → item skipped, no call placed
10. `Retell: Create Phone Call` — POST to `https://api.retellai.com/v2/create-phone-call`
11. `Mark Queue Sent` — PATCH follow_up_queue status='sent', sent_at=now()
12. Loop continues to next item

**Retell call payload:**
```json
{
  "from_number": "+15722124790",
  "to_number": "lead's phone_e164",
  "override_agent_id": "agent_ff863b1414049444c174360809",
  "retell_llm_dynamic_variables": {
    "first_name": "...",
    "company_name": "...",
    "booking_link": "https://calendly.com/mahfujurrahman511351/30min",
    "previous_conversation_summary": "180-word brief from OpenAI",
    "lead_email": "lead@example.com"
  },
  "metadata": {
    "source": "n8n_flowtics_followup",
    "queue_id": "...",
    "lead_email": "..."
  }
}
```

---

### 5.9 Post Call Analysis - MCO
**ID:** `r8XKHCnL4vju2E4j` | **Status:** ACTIVE | **Nodes:** 4
**Webhook:** `POST https://n8n-1404.n8n.whiteserverdns.com/webhook/9cdd28e8-7cfd-4765-a623-cda2d1b9f7a7`
**Registered in Retell:** Agent `agent_ff863b1414049444c174360809` post-call webhook

Dedicated post-call handler. Retell fires this after every call ends. Extracts the call data and writes the interaction to Supabase via MCO.

**Node flow:**
1. `Webhook1` — receives Retell POST (Retell sends multiple event types)
2. `Filter` — only passes through `event == 'call_analyzed'` (fired after Retell finishes analysis, includes summary and qualified_status)
3. `Build MCO Write Payload` — code node that:
   - Extracts `lead_email` from `call.retell_llm_dynamic_variables.lead_email`
   - Generates deterministic `event_id` UUID from `call.call_id`
   - Sets `channel='voice'`, `direction='outbound'`
   - Uses `call_analysis.call_summary` as content (falls back to transcript)
   - Maps `qualified_status=true` → `intent='interested'`, else `intent='unknown'`
   - Validates `to_number` as E.164 for `phone_e164`
   - Builds metadata: `{call_id, agent_id, qualified_status, recording_url, source:'retell_flowtics'}`
4. `POST /mco-write-event` — writes to Supabase, Notion, Monday.com

---

### 5.10 Aimfox Data Fetching - MCO
**ID:** `o9l5PClHznNgZIK8` | **Status:** ACTIVE | **Nodes:** 4
**Trigger:** Aimfox webhook

Fetches lead data from Aimfox API when a lead event fires and appends the enriched data to a Google Sheet.

**Node flow:**
1. `Webhook` — Aimfox event fires
2. `HTTP Request` — fetches lead details from Aimfox accounts API
3. `HTTP Request1` — second Aimfox API call for additional data
4. `Append row in sheet` — writes to Google Sheets

---

### 5.11 MCO - Aimfox Responded
**ID:** `Zw7iTErdMMJjiM7g` | **Status:** ACTIVE | **Nodes:** 2
**Trigger:** Aimfox webhook (lead responded event)

Simple two-node workflow. When a lead responds to an Aimfox campaign message, marks them as "initiated" in Aimfox via the API.

**Node flow:**
1. `Responded` (Webhook) — Aimfox fires when lead responds
2. `Mark Initiated` — POST to Aimfox leads API to update lead status

---

## 6. Workflow Relationship Map

```
┌─────────────────────────────────────────────────────────────────────┐
│                        ENTRY POINTS                                 │
│  LinkedIn Reply  │  Email Reply  │  Connection Accepted  │  Voice  │
└────────┬─────────┴──────┬────────┴──────────┬────────────┴────┬────┘
         │                │                   │                 │
         ▼                ▼                   ▼                 ▼
   Aimfox Reply    Gmail Reply Agent   Connection Accepted   Post Call
   Agent — MCO      (active, Slack     Handler — MCO        Analysis
                     approval)                               — MCO
         │                │                   │                 │
         └────────────────┴───────────────────┴─────────────────┘
                                   │
                                   ▼
                    ┌──────────────────────────────┐
                    │  MCO Write Conversation Event │ ◄── All channels write here
                    │  (Supabase + Monday + Notion) │
                    └──────────────────────────────┘
                                   │
                         trigger_cross_channel?
                                   │ YES
                                   ▼
                         follow_up_queue (Supabase)
                          ┌────────┴────────┐
                          │                 │
                     target=email      target=voice
                          │                 │
                          ▼                 ▼
                    FollowUp Queue    Flowtics AI
                    Dispatcher        Call Agent
                    (every 15 min)    (every 4 hrs)
                          │
                          ▼
                    Centralized
                    Follow-Up
                    Coordinator
                    ┌────┴────┐
                    │         │
                  Email   LinkedIn
                  (Gmail)  (Aimfox)
                    │
                    ▼
              Lead replies to email
                    │
                    ▼
            Gmail Reply Agent
            (Slack approval → send)
```

**MCO Fetch Context** is called by:
- Centralized Coordinator (before generating follow-up messages)
- Connection Accepted Handler (before generating opening message)
- Gmail Reply Agent (before generating email reply)
- Flowtics Call Agent (before summarising context for voice agent)

**MCO Write Event** is called by:
- Aimfox Reply Agent (Write Returning + Write First Interested)
- Gmail Reply Agent (Write Inbound + Write Outbound)
- Connection Accepted Handler (Log to Supabase)
- Centralized Coordinator (Log to Supabase after send)
- Post Call Analysis (after call ends)

---

## 7. Current System Status

| Workflow | Status | Trigger |
|----------|--------|---------|
| MCO - Write Conversation Event | ✅ ACTIVE | Webhook (called by all agents) |
| MCO - Fetch Cross-Channel Context | ✅ ACTIVE | Webhook (called by all agents) |
| MCO - FollowUp Queue Dispatcher | ✅ ACTIVE | Cron every 15 min |
| MCO - Centralized Follow-Up Coordinator | ✅ ACTIVE | Webhook (called by Dispatcher) |
| MCO - Aimfox Connection Accepted Handler | ✅ ACTIVE | Aimfox webhook |
| Aimfox Nextus AI Reply Agent — MCO | ✅ ACTIVE | Aimfox webhook |
| MCO - Gmail Reply Agent | ✅ ACTIVE | Gmail trigger |
| Flowtics AI Call Agent - MCO | ✅ ACTIVE | Cron every 4 hours |
| Post Call Analysis - MCO | ✅ ACTIVE | Retell post-call webhook |
| Aimfox Data Fetching - MCO | ✅ ACTIVE | Aimfox webhook |
| MCO - Aimfox Responded | ✅ ACTIVE | Aimfox webhook |

---

## 8. Data Flow Per Lead Intent

**Lead is new, marks interested on LinkedIn:**
1. Aimfox Reply Agent detects → AI replies on LinkedIn
2. Write First Interested → writes seed + inbound (interested) + outbound
3. follow_up_queue gets 2 rows: email (30 min) + voice (30 min)
4. Dispatcher picks up email row → Coordinator sends personalised email via Gmail
5. Flowtics picks up voice row (next 4-hour window) → places Retell call
6. Retell calls lead → post-call webhook fires → call logged to Supabase
7. Lead replies to email → Gmail Agent → Slack approval → reply sent + logged

**Lead is returning (already in Supabase):**
1. Any new reply on any channel → Write Returning (or respective channel handler)
2. Context is fetched before every reply
3. No cross-channel trigger unless explicitly set

---

## 9. Known Gaps and Pending Items

| Item | Detail |
|------|--------|
| **Stale queue rows** | 2 pending rows from May 4 test (`mco-test-lead@example.com`). Should be cleared: `DELETE FROM follow_up_queue WHERE created_at < '2026-05-10' AND status = 'pending'` |
| **phone_e164 for LinkedIn leads** | LinkedIn leads arrive with no phone number. Voice follow-ups are queued but skipped by the phone guard until a phone is provided. Needs enrichment via Clay or manual update |
| **LinkedIn follow-up delay** | When a lead is interested on LinkedIn, the LinkedIn channel is not yet in target_channels. Plan: add `linkedin` with a 5–7 day scheduled_for delay to re-engage if no reply. Not yet implemented — delay duration TBC |
| **Retell webhook test** | Post-call webhook is registered in Retell. Hit "Test" in Retell dashboard to confirm n8n receives and processes correctly |
| **Gmail Disapprove path** | If Slack approval is denied, the workflow stops silently with no log. A future improvement would write a `disapproved` note to Supabase |
| **phone_map empty** | No real voice traffic yet. Will populate automatically once real Retell calls begin and leads have phone numbers |
