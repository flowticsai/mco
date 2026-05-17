# MCO — Multi-Channel Outreach System
**Full System Documentation**
**Last Updated:** 2026-05-17 (Session 3 — synced to live n8n state)
**Instance:** n8n-1404.n8n.whiteserverdns.com

---

## 1. What MCO Is

MCO is a unified outreach automation system that coordinates LinkedIn, email, voice, and SMS interactions across a single shared memory store. Every agent in the system — whether it's replying to a LinkedIn message, sending an email follow-up, or placing a phone call — reads from and writes to the same database before acting. This means no agent ever repeats what another already said, and every reply is informed by the full cross-channel history of that lead.

The core problem MCO solves is channel amnesia. Without it, a lead might get a LinkedIn reply that ignores the email they already responded to, or a voice call that re-introduces a product they already expressed interest in. MCO eliminates that by making cross-channel context available to every agent at the moment it needs it.

---

## 2. Architecture — The WAT Framework

MCO is built on three layers:

**Layer 1 — Workflows (Instructions)**
Markdown SOPs in `workflows/` that define what to do, when, and how.

**Layer 2 — Agents (Decision-Making)**
n8n workflows that read the SOPs, call the right tools in sequence, handle failures, and coordinate between services.

**Layer 3 — Tools (Execution)**
Python scripts in `tools/` and HTTP API calls that do the deterministic work — database writes, API calls, data transformations.

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
| `lead_id` | uuid (PK) | Primary key (UUID). Never changes. Used as FK by conversations and follow_up_queue. |
| `lead_email` | text (unique, nullable) | Lowercase canonical email. Null for LinkedIn-only leads. |
| `full_name` | text | Lead's full name |
| `company` | text | Company name |
| `linkedin_urn` | text | LinkedIn URN identifier |
| `linkedin_profile_url` | text | Full LinkedIn profile URL (`https://linkedin.com/in/...`) |
| `phone_e164` | text | Phone number in E.164 format (`+15551234567`). Required for voice calls |
| `first_channel` | text | The first channel this lead was seen on (`linkedin`, `email`, `voice`, `sms`) |
| `first_seen_at` | timestamptz | When the lead first appeared in the system |
| `last_activity_at` | timestamptz | Timestamp of most recent interaction across any channel |
| `last_active_channel` | text | Which channel had the most recent interaction |
| `linkedin_conversation_urn` | text | URN of the active LinkedIn conversation thread. Set by Connection Accepted Handler. Read by Coordinator to decide DM vs campaign path. |
| `notion_page_id` | text | Notion CRM page ID for this lead. Written by Write Event. |
| `clay_enriched` | boolean | Whether Clay has enriched this lead's data |
| `overall_intent` | text | The highest intent level ever reached. Never demotes. Values: `unknown → no_action → not_interested → referral → already_have_contract → interested → booking` |
| `overall_intent_updated_at` | timestamptz | When intent last changed |
| `aimfox_campaign` | text | Which Aimfox campaign this lead belongs to |
| `email_sender_inbox` | text | Which Gmail inbox was used to email this lead |
| `notes` | text | Free-form notes |
| `created_at` | timestamptz | Row creation timestamp |

**Key behaviour:** Lead records are upserted on every write event via the Postgres RPC function `upsert_lead()`. `overall_intent` never demotes — if a lead was `interested` and a new event comes in with `no_action`, the intent stays `interested`.

---

#### Table: `conversations`
Every single interaction across every channel. The complete cross-channel timeline.

| Column | Type | Purpose |
|--------|------|---------|
| `event_id` | text (PK) | Deduplication key. Safe to retry writes — duplicate event_id is ignored |
| `lead_id` | uuid (FK → leads.lead_id) | Which lead this interaction belongs to. Preferred over email for lookups. |
| `lead_email` | text | Denormalised for convenience. Not FK — lead_id is the FK. |
| `timestamp` | timestamptz | When the interaction happened (from the source system, not write time) |
| `channel` | text | `linkedin`, `email`, `voice`, `sms` |
| `direction` | text | `inbound` (lead to us) or `outbound` (us to lead) |
| `content` | text | The message body, transcript, or summary |
| `content_type` | text | `message`, `transcript`, or `summary` |
| `sender_name` | text | Name of who sent the message |
| `intent` | text | Intent classification for this specific event |
| `metadata` | jsonb | Arbitrary metadata (call_id, duration_ms, recording_url, aimfox_account_id, conversation_urn, queue_id, etc.) |
| `workflow_execution_id` | text | n8n execution ID for traceability |
| `written_at` | timestamptz | When this row was written to Supabase |

**Key behaviour:** The `event_id` deduplication means the write endpoint can be called multiple times for the same event without double-writing. This is critical because n8n retries on timeout.

---

#### Table: `follow_up_queue`
Queued cross-channel follow-ups waiting to be dispatched.

| Column | Type | Purpose |
|--------|------|---------|
| `queue_id` | uuid (PK) | Unique identifier |
| `lead_id` | uuid (FK → leads.lead_id, nullable) | Which lead to follow up with. Preferred identifier. |
| `lead_email` | text | Denormalised for convenience. |
| `trigger_channel` | text | Which channel triggered this follow-up (e.g. `linkedin`) |
| `target_channel` | text | Which channel to send the follow-up on (`email`, `voice`, `linkedin`) |
| `trigger_event_id` | text | The conversation event that triggered this queue entry |
| `scheduled_for` | timestamptz | When this follow-up should be sent (dispatchers check `<= now()`) |
| `status` | text | `pending`, `sent`, `skipped`, or `failed` |
| `follow_up_context` | text | Context string passed to the Coordinator to personalise the message |
| `sent_at` | timestamptz | When it was dispatched |
| `created_at` | timestamptz | Row creation timestamp |

**Follow-up rules (as of 2026-05-17):**
- **Delay:** 3 minutes (testing). Change `3*60*1000` → `30*60*1000` in `Build Queue Rows` node for production (30 min)
- **Max follow-ups:** 2 per lead per channel. After 2 sent rows exist for a lead+channel, no more are queued
- **On inbound reply:** All `pending` rows for that lead on the **same channel** are rescheduled to `now + 3 min`. Other channels are unaffected
- **Sequential queueing:** After each follow-up is sent, the Coordinator's `Queue Next Follow-Up?` node checks sent count and inserts the next row if under the limit

**How rows are created:** When a Write Conversation Event call includes `trigger_cross_channel: true` and `target_channels: ['email', 'voice']`, the workflow inserts one queue row per channel. Initial rows are inserted at trigger time. Subsequent rows are inserted by the Coordinator after each send.

---

#### Table: `phone_map`
Maps phone numbers to lead emails. Used by voice/SMS channels to resolve who called.

| Column | Type | Purpose |
|--------|------|---------|
| `phone_e164` | text (PK) | Phone number in E.164 format |
| `lead_email` | text (FK → leads) | Resolved lead identity |
| `source` | text | Where this mapping came from (`retell`, `clay`, `manual`) |
| `confidence` | text | `high`, `medium`, or `low` |

**How it gets populated:** When a Write Conversation Event call includes a `phone_e164` field, a phone_map row is upserted automatically.

---

### 3.2 n8n Self-Hosted Instance
**URL:** `https://n8n-1404.n8n.whiteserverdns.com`
**API Version:** v1

All workflows run on this instance. All webhook URLs and internal references point to this instance.

**Credentials configured in n8n:**
- `gmailOAuth2` — Gmail account for sending/receiving emails
- `anthropicApi` — Claude (Sonnet) for reply generation
- `googlePalmApi` — Gemini for email classification
- `slackApi` — Slack for human approval messages

---

### 3.3 GitHub Repository
**URL:** `https://github.com/flowticsai/mco`
**Branch:** `main`

All n8n workflow JSONs are exported here with `REDACTED_*` placeholders replacing hardcoded secrets. The `.env` file and `.tmp/` directory are gitignored.

---

### 3.4 External Services

| Service | Role | Key Details |
|---------|------|-------------|
| **Aimfox** | LinkedIn automation | API key: `Bearer e5cc0625-fe79-4bb8-b816-f23f80bbdbb6`. Sends messages, manages campaigns, fires webhooks on replies and connection accepts |
| **Retell AI** | Voice calls | Agent `agent_ff863b1414049444c174360809`, from `+15722124790`, booking link `calendly.com/mahfujurrahman511351/30min` |
| **Notion** | Lead dashboard | Database `362dc227-748f-8184-892a-c6f8f3151b07`. One page per lead, one entry per conversation |
| **Slack** | Human approval | Channel `#linkedin_notifications` (`C0B465KKGKU`). Gmail replies require approval before sending |
| **Clay** | Data enrichment | Referral webhook configured. Used for phone → email resolution |
| **OpenAI** | Pre-call summarisation | gpt-4o-mini. Condenses cross-channel context into ≤180-word brief before Retell places a call |
| **Google Sheets** | Aimfox lead log | Sheet: "Aimfox - Flowtics Demo" → tab "Data Capture". Columns: ID, Account ID, Lead ID, Conversation URN, Campaign Name. Written by Data Fetching workflow |
| **Monday.com** | ~~CRM~~ | **Removed.** No longer used. `monday_item_id` column remains in Supabase schema but MCO no longer writes to it |

---

### 3.5 MCP Servers (Claude Code)
Configured in `C:\Users\Washi\.claude\settings.json`:

| Server | URL | Auth | Purpose |
|--------|-----|------|---------|
| `n8n-mcp` | `https://n8n-1404.n8n.whiteserverdns.com/mcp-server/http` | Bearer token | Direct n8n workflow management |
| `aimfox` | `https://mcp.aimfox.com` | OAuth (requires login on first use) | Direct Aimfox API access — campaigns, leads, conversations |

---

## 4. LinkedIn Follow-Up Architecture

LinkedIn follow-ups have three paths in the Coordinator, depending on what we know about the lead.

### Scenario A — Has existing conversation (lead has replied before)
`conversation_urn` is stored in `conversations.metadata_json` via the Aimfox Reply Agent.

```
Has Conversation URN? → YES
  → LinkedIn: Reply to Conversation
    POST /api/v2/accounts/{aimfox_account_id}/conversations/{conversation_urn}
    Content-Type: application/json
    body: { message: claude_generated_text }
```

### Scenario B — Connection accepted, first message (has aimfox_lead_id)
Applies after a connection request is accepted. `aimfox_lead_id` and `aimfox_account_id` arrive via `follow_up_context` JSON in the queue row (written by the Connection Accepted Handler).

```
Has Conversation URN? → NO
  → Has Lead ID? → YES
    → LinkedIn: Send First Message
      POST /api/v2/accounts/{aimfox_account_id}/conversations
      body: { message: claude_generated_text, recipients: [aimfox_lead_id] }
```

### Scenario C — Not yet in Aimfox (lead from email/other source)
Lead has a `linkedin_profile_url` but has never been added to Aimfox.

```
Has Conversation URN? → NO
  → Has Lead ID? → NO
    → Has Profile URL? → YES
      → Add to LinkedIn Campaign
        POST /api/v2/campaigns/6e2feb86-b9c6-4c18-87fa-c5fe5e41682f/audience/multiple
        { type: 'profile_url', profiles: [{ profile_url, custom_variables: { CUSTOM_MESSAGE } }] }
        → Aimfox assigns lead_id, sends connection request automatically
      → Mark Connection Requested (send_status = 'connection_requested')
        → Queue Next Follow-Up? SKIPS — no next row queued
        → When connection is accepted → Aimfox fires `accepted` webhook
          → Connection Accepted Handler queues Scenario B follow-up
    → Has Profile URL? → NO → Skip
```

**Connection Accepted Handler → Write Event chain:**
When Aimfox fires the `accepted` webhook, the Connection Accepted Handler posts to Write Event with:
- `channel: 'linkedin'`, `direction: 'inbound'`, `content: 'LinkedIn connection request accepted.'`
- `trigger_cross_channel: true`, `target_channels: ['linkedin']`
- `follow_up_context: JSON.stringify({ aimfox_lead_id, aimfox_account_id })`

Write Event queues one `target_channel: 'linkedin'` row. Coordinator picks it up and runs Scenario B.

**Aimfox follow-up campaign:** `6e2feb86-b9c6-4c18-87fa-c5fe5e41682f`
- Has NO message steps — MCO sends all messages directly via API
- Campaign purpose only: enroll lead in Aimfox, trigger connection request

**Aimfox Webhooks Registered:**

| Name | Event | URL |
|------|-------|-----|
| New Reply | `new_reply` | `/webhook/ce611e21-3a9d-4a90-b75a-3c5b088c99a7` |
| Reply | `reply` | `/webhook/3e030494-4719-491d-8291-2c9523e71c3d` |
| Responded | `reply` | `/webhook/629054f9-748e-4411-8bc2-5931ac2436cf` |
| Connection Accepted | `accepted` | `/webhook/mco-aimfox-accepted` |

**Aimfox `accepted` event payload shape:**
```json
{
  "id": "uuid",
  "event_type": "accepted",
  "event": {
    "account": { "id": 685914315, "urn": "...", "email": "..." },
    "target": { "id": 153688726, "urn": "...", "public_identifier": "...", "first_name": "...", "last_name": "...", "email": null },
    "campaign": { "id": "...", "name": "..." }
  }
}
```
Note: `event.target.email` is often null for LinkedIn leads. Handler falls back to `urn_{public_identifier}@linkedin.placeholder`.

### Key Aimfox API Endpoints

| Action | Endpoint | Body |
|--------|----------|------|
| Reply to existing conversation | `POST /api/v2/accounts/{account_id}/conversations/{urn}` | `{ message }` (JSON) |
| Start new conversation | `POST /api/v2/accounts/{account_id}/conversations` | `{ message, recipients: [lead_id] }` |
| Add lead to campaign | `POST /api/v2/campaigns/{campaign_id}/audience/multiple` | `{ type: 'profile_url', profiles: [{ profile_url, custom_variables }] }` |

---

## 5. Complete A-Z Flow

### LinkedIn Lead — First Interested

```
[LEAD REPLIES ON LINKEDIN]
         │
         ▼
Aimfox fires webhook → Aimfox Nextus AI Reply Agent — MCO
         │
         ├─ AI reads thread, classifies intent
         ├─ Generates reply, sends via Aimfox
         │
         ├─ [RETURNING LEAD — already in Supabase]
         │   MCO: Write Returning → writes inbound + outbound
         │
         └─ [NEW LEAD — marked Interested, not in Supabase]
             MCO: Write First Interested
             → Writes full historical thread (seed)
             → Writes inbound event:
               intent=interested
               trigger_cross_channel=true
               target_channels=['email', 'voice']
             → Writes outbound reply
             → MCO Write Conversation Event fires → queues email + voice follow-ups
```

### Write Conversation Event

```
[MCO WRITE CONVERSATION EVENT]
         │
         ▼
Setup & Validate → fields validated, phone normalised
         │
         ▼
Cancel Pending On Inbound
  IF direction='inbound': reschedule all pending rows for this lead+channel
  to now + 3 min (same channel only, other channels unaffected)
         │
         ▼
Upsert Lead (RPC: upsert_lead) → intent never demotes
         │
         ▼
Merge Lead Data → pulls overall_intent from RPC response
         │
         ▼
Insert Conversation → deduplication on event_id
         │
         ├─ [DUPLICATE] → Return Already Written, stops
         │
         ▼
Has Phone? → Upsert PhoneMap (if phone_e164 present)
         │
         ▼
Notion: Query Lead → Update or Create lead page → Create Conversation entry
         │
         ▼
Cross-Channel? (trigger_cross_channel=true)
         │ YES
         ▼
Build Queue Rows → one row per target_channel
  scheduled_for = now + 3 min (testing) / now + 30 min (production)
         │
         ▼
Insert FollowUpQueue → Return OK
```

### Follow-Up Queue Processing

```
[DISPATCHER — every 15 minutes]
         │
         ▼
Fetch follow_up_queue WHERE status='pending' AND scheduled_for <= now()
         │
         ▼
For each row:
  Fetch LinkedIn metadata (aimfox_account_id, conversation_urn) from conversations
  → Call Centralized Follow-Up Coordinator

[COORDINATOR]
         │
         ▼
Fetch Cross-Channel Context → Merge Context
         │
         ▼
Route by Channel
  │
  ├─ [EMAIL]
  │   Claude generates email → Send via Gmail
  │   → Log to Supabase → Mark Queue Sent
  │   → Queue Next Follow-Up? (if sent count < 2, insert next row at now + 3 min)
  │
  └─ [LINKEDIN]
      Claude generates message
        │
        ├─ Has conversation_urn? → YES (Scenario A)
        │   → Reply to Conversation (JSON body: { message })
        │
        ├─ Has conversation_urn? → NO → Has aimfox_lead_id? → YES (Scenario B)
        │   → Send First Message via POST /conversations { message, recipients: [lead_id] }
        │
        └─ Has aimfox_lead_id? → NO → Has profile_url? → YES (Scenario C)
            → Add to Aimfox campaign → Mark connection_requested
            → Queue Next Follow-Up? SKIPS (waits for Connection Accepted webhook)
            Has profile_url? → NO → Skip
      │
      → Log to Supabase → Mark Queue Sent
      → Queue Next Follow-Up? (if sent count < 2, insert next row)
```

### Voice Follow-Up

```
[FLOWTICS AI CALL AGENT — every 4 hours]
         │
         ▼
Fetch follow_up_queue WHERE target_channel='voice' AND status='pending'
AND scheduled_for <= now() LIMIT 25
         │
         ▼
For each lead:
  Fetch lead record (phone_e164, name, company)
  phone_e164 missing? → SKIP (phone guard, return [])
         │
         ▼
Fetch Cross-Channel Context
  → Summarize via OpenAI gpt-4o-mini (≤180-word brief)
         │
         ▼
Build Retell Request
  from: +15722124790
  to: lead's phone_e164
  agent: agent_ff863b1414049444c174360809
  dynamic vars: first_name, company_name, booking_link,
                previous_conversation_summary, lead_email
         │
         ▼
Retell: Create Phone Call → Mark Queue Sent
```

### Post-Call Analysis

```
[POST CALL ANALYSIS — Retell webhook]
         │
         ▼
Filter: event == 'call_analyzed' only
         │
         ▼
Build MCO Write Payload:
  lead_email from retell_llm_dynamic_variables.lead_email
  event_id: deterministic UUID from call_id
  channel='voice', direction='outbound'
  content: call_analysis.call_summary
  intent: qualified_status=true → 'interested', else 'unknown'
  metadata: call_id, agent_id, qualified_status, recording_url
         │
         ▼
POST /mco-write-event → logs to Supabase + Notion
```

### Gmail Reply Flow

```
[GMAIL REPLY AGENT]
         │
         ▼
Gmail Trigger → Extract & Filter
  Skips: non-replies, noreply senders, empty body
         │
         ▼
Supabase: Check Outbound → not our thread? → stop
         │
         ▼
MCO: Fetch Context → Write Inbound
         │
         ▼
Text Classifier (Gemini) → needs reply?
  NO → stop
  YES → Claude generates reply
         │
         ▼
Slack sendAndWait → #linkedin_notifications (human approval)
  Approve → Send Gmail Reply → MCO: Write Outbound
  Disapprove → stop
```

### Connection Accepted Flow

```
[CONNECTION ACCEPTED HANDLER]
         │
         ▼
Aimfox fires `accepted` webhook
         │
         ▼
Extract Fields
  Parses: event.account.id → aimfox_account_id
          event.target.id  → aimfox_lead_id
          event.target.urn → lead_urn
          event.target.email (may be null) → lead_email
          event.target.public_identifier  → for linkedin_profile_url
  Email null? → lead_email = urn_{public_identifier}@linkedin.placeholder
         │
         ▼
Has Email? → NO → Skip → Return OK
         │ YES
         ▼
Trigger Write Event
  POST /mco-write-event with:
    channel: 'linkedin', direction: 'inbound'
    content: 'LinkedIn connection request accepted.'
    trigger_cross_channel: true
    target_channels: ['linkedin']
    follow_up_context: JSON.stringify({ aimfox_lead_id, aimfox_account_id })
         │
         ▼
Write Event queues linkedin follow-up row in follow_up_queue
  → Dispatcher picks it up → Coordinator runs Scenario B
  → Coordinator generates contextual first message → sends via API
```

---

## 6. Workflow Reference — All 11 Workflows

### 6.1 MCO - Write Conversation Event
**ID:** `5qOo5YzrPnW8Uj9g` | **Status:** ACTIVE | **Nodes:** 21
**Webhook:** `POST https://n8n-1404.n8n.whiteserverdns.com/webhook/mco-write-event`

**Required payload fields:**
```json
{
  "event_id": "unique-dedup-key",
  "lead_email": "lead@example.com",
  "channel": "linkedin | email | voice | sms",
  "direction": "inbound | outbound",
  "content": "message text or transcript",
  "timestamp": "2026-05-17T10:00:00Z"
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
3. `Cancel Pending On Inbound` — **NEW.** If direction='inbound', reschedules all pending queue rows for this lead+channel to now+3 min
4. `Upsert Lead` — RPC call to Postgres `upsert_lead()` function
5. `Merge Lead Data` — merges RPC response (overall_intent)
6. `Insert Conversation` — inserts into conversations table
7. `Check Insert Result` — was it a new row or duplicate?
8. `Was Duplicate?` — if duplicate → Return Already Written, stops
9. `Has Phone?` — if phone present → `Upsert PhoneMap`
10. `Notion: Query Lead` — searches Notion leads database by email
11. `Notion: Extract Lead ID` — parses query response
12. `IF: Notion Lead Found?` — branches on whether lead page exists
13. `Notion: Update Lead` OR `Notion: Create Lead` — upserts lead page
14. `Notion: Create Conversation` — appends conversation entry to Notion
15. `Cross-Channel?` — if trigger_cross_channel=true → queue follow-ups
16. `Build Queue Rows` — creates one row per target_channel, scheduled_for = now + 3 min (testing)
17. `Insert FollowUpQueue` — inserts into follow_up_queue table
18. `Return OK`

**Note:** Monday.com nodes fully removed. Notion is the only CRM integration active.

---

### 6.2 MCO - Fetch Cross-Channel Context
**ID:** `UJoDCfkmD3NJHktk` | **Status:** ACTIVE | **Nodes:** 5
**Webhook:** `POST https://n8n-1404.n8n.whiteserverdns.com/webhook/mco-fetch-context`

Called by every AI agent before generating any reply. Returns complete cross-channel history for a lead.

**Node flow:**
1. `Webhook` → 2. `Setup & Validate` → 3. `Fetch Context RPC` → 4. `Format Context Block` → 5. `Return Context`

---

### 6.3 MCO - FollowUp Queue Dispatcher
**ID:** `3ju6z4oJcWJqskBN` | **Status:** ACTIVE | **Nodes:** 9
**Trigger:** Cron every 15 minutes

Polls `follow_up_queue` for pending items and routes to the Coordinator.

**Node flow:**
1. `Every 15 Minutes` → 2. `Config` → 3. `Fetch Pending Queue` → 4. `Split Rows` → 5. `Fetch LinkedIn Meta` → 6. `Merge LinkedIn Meta` → 7. `Call Coordinator` → 8. `Log Result`

---

### 6.4 MCO - Centralized Follow-Up Coordinator
**ID:** `KXKcCYRnK4V8v9k7` | **Status:** ACTIVE | **Nodes:** 24
**Webhook:** Internal (called by Dispatcher only)

**Aimfox API key:** `Bearer e5cc0625-fe79-4bb8-b816-f23f80bbdbb6`
**LinkedIn follow-up campaign:** `6e2feb86-b9c6-4c18-87fa-c5fe5e41682f`

**Node flow:**
1. `Webhook` → 2. `Setup & Validate` → 3. `Fetch Context` → 4. `Merge Context` → 5. `Route by Channel`

`Merge Context` parses `follow_up_context` JSON to extract `aimfox_lead_id` and `aimfox_account_id` (set by Connection Accepted Handler via queue row).

**Email branch:**
6. `Email: Claude Model` → 7. `Email: Generate Message` → 8. `Email: Extract Message` → 9. `Send Email (Gmail)`

**LinkedIn branch:**
6. `LinkedIn: Claude Model` → 7. `LinkedIn: Generate Message` → 8. `LinkedIn: Extract Message`
9. `LinkedIn: Has Conversation URN?`
   - **TRUE → Scenario A:** `LinkedIn: Reply to Conversation`
     POST `…/conversations/{conversation_urn}` — JSON body `{ message }`
   - **FALSE → `LinkedIn: Has Lead ID?`**
     - **TRUE → Scenario B:** `LinkedIn: Send First Message`
       POST `…/conversations` — body `{ message, recipients: [aimfox_lead_id] }`
     - **FALSE → `LinkedIn: Has Profile URL?` (check via skipped flag)**
       - **TRUE → Scenario C:** `Add to LinkedIn Campaign` → `LinkedIn: Mark Connection Requested`
         Sets `send_status = 'connection_requested'` → Queue Next Follow-Up? skips
       - **FALSE:** `LinkedIn: Skip (No URL)`

**Convergence:**
10. `After Send` → 11. `Log to Supabase` → 12. `Mark Queue Sent`
13. `Queue Next Follow-Up?` — Counts sent rows for lead+channel. If < 2, inserts next row at now + 3 min. Skips if voice, skipped, or connection_requested.
14. `Return OK`

---

### 6.5 MCO - Aimfox Connection Accepted Handler
**ID:** `WTbIAJCZGtppAT91` | **Status:** ACTIVE | **Nodes:** 7
**Trigger:** Aimfox `accepted` webhook → `POST /webhook/mco-aimfox-accepted`

Fires when a LinkedIn connection request is accepted. Does NOT generate or send a message itself — instead queues a LinkedIn follow-up via Write Event so the Coordinator handles it with full context.

**Aimfox API key:** `Bearer e5cc0625-fe79-4bb8-b816-f23f80bbdbb6`

**Node flow:**
1. `Webhook` → 2. `Extract Fields`
   - Maps `event.account.id` → `aimfox_account_id`
   - Maps `event.target.id` → `aimfox_lead_id`
   - Maps `event.target.email` → `lead_email` (null → `urn_{public_identifier}@linkedin.placeholder`)
   - Maps `event.target.public_identifier` → `linkedin_profile_url`
3. `Has Email?` — NO → 4. `Skip — No Email` → 5. `Return OK (Skipped)`
6. `Trigger Write Event`
   - Posts connection_accepted event with `target_channels: ['linkedin']`
   - Passes `follow_up_context: JSON.stringify({ aimfox_lead_id, aimfox_account_id })`
7. `Return OK`

**Why this design:** The first LinkedIn message after connection acceptance is generated by the Coordinator (with full cross-channel context), not by this handler. This keeps message generation in one place.

---

### 6.6 Aimfox Nextus AI Reply Agent — MCO
**ID:** `SPN1NLyHH1LcfViD` | **Status:** ACTIVE | **Nodes:** 37
**Trigger:** Aimfox webhook (new LinkedIn message received)

Main LinkedIn reply agent. Classifies intent, generates reply, sends via Aimfox, writes to Supabase.

**target_channels currently:** `['email', 'voice']` — LinkedIn not yet added (pending sequential flow design)

**MCO integration logic:**
- **Returning lead** → `MCO: Write Returning` — writes inbound + outbound, no cross-channel trigger
- **New lead marked Interested** → `MCO: Write First Interested` — seeds full thread, writes inbound (trigger_cross_channel=true, target_channels=['email','voice']), writes outbound reply

**Intent → Action:**
| Intent | Action |
|--------|--------|
| `interested` | Write First Interested → email + voice follow-up queued |
| `not_interested` | No MCO write |
| `referral` | Slack notification + referral handling |
| `booking` | Slack notification |
| `no_action` | Write Returning only |

---

### 6.7 MCO - Gmail Reply Agent
**ID:** `mFBOGdMAsXRKD1Pv` | **Status:** ACTIVE | **Nodes:** 18
**Trigger:** Gmail — new email received

Monitors Gmail for replies to emails we sent. Gets human Slack approval before sending any reply.

**Node flow:**
1. `Gmail Trigger` → 2. `Extract & Filter` → 3. `Supabase: Check Outbound` → 4. `Filter: Our Thread Only`
5. `MCO: Fetch Context` → 6. `Merge Context` → 7. `MCO: Write Inbound`
8. `Text Classifier (Gemini)` → needs reply?
   - NO → `No Operation`
   - YES → 9. `Reply Agent (Claude)` → 10. `Edit Fields` → 11. `Slack sendAndWait (#linkedin_notifications)`
     - Approve → 12. `Send Gmail Reply` → 13. `MCO: Write Outbound`
     - Disapprove → stop

---

### 6.8 Flowtics AI Call Agent - MCO
**ID:** `xE8mFF8HxPaSXNmi` | **Status:** ACTIVE | **Nodes:** 12
**Trigger:** Cron every 4 hours

Outbound voice caller. Polls for pending voice follow-ups and places Retell calls.

**Phone guard:** if `phone_e164` is empty → returns `[]` → item skipped, no call placed.

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
    "previous_conversation_summary": "≤180-word OpenAI brief",
    "lead_email": "lead@example.com"
  }
}
```

---

### 6.9 Post Call Analysis - MCO
**ID:** `r8XKHCnL4vju2E4j` | **Status:** ACTIVE | **Nodes:** 4
**Webhook:** `POST https://n8n-1404.n8n.whiteserverdns.com/webhook/9cdd28e8-7cfd-4765-a623-cda2d1b9f7a7`

Receives Retell post-call webhook, filters for `call_analyzed` event, writes call summary to Supabase.

---

### 6.10 Aimfox Data Fetching - MCO
**ID:** `o9l5PClHznNgZIK8` | **Status:** ACTIVE | **Nodes:** 4
**Trigger:** Aimfox webhook

Fetches lead + conversation data from Aimfox when an event fires. Appends to Google Sheet:
- **Sheet:** "Aimfox - Flowtics Demo" → tab "Data Capture"
- **Columns:** ID, Account ID, Lead ID, Conversation URN, Campaign Name
- **Used by:** Aimfox Reply Agent checks Lead ID against this sheet to determine if it should run

---

### 6.11 MCO - Aimfox Responded
**ID:** `Zw7iTErdMMJjiM7g` | **Status:** ACTIVE | **Nodes:** 2
**Trigger:** Aimfox webhook (lead responded event)

Two-node workflow. When a lead responds to an Aimfox campaign message, labels them as "initiated" in Aimfox via the API (`label_id: c462f06b-c998-4741-821f-b0b2232c8a98`).

---

## 7. Workflow Relationship Map

```
┌─────────────────────────────────────────────────────────────────────┐
│                        ENTRY POINTS                                 │
│  LinkedIn Reply  │  Email Reply  │  Connection Accepted  │  Voice  │
└────────┬─────────┴──────┬────────┴──────────┬────────────┴────┬────┘
         │                │                   │                 │
         ▼                ▼                   ▼                 ▼
   Aimfox Reply    Gmail Reply Agent   Connection Accepted   Post Call
   Agent — MCO    (Slack approval)     Handler — MCO        Analysis
         │                │                   │                 │
         └────────────────┴───────────────────┴─────────────────┘
                                   │
                                   ▼
                    ┌──────────────────────────────┐
                    │  MCO Write Conversation Event │ ◄── All channels write here
                    │     (Supabase + Notion)       │
                    └──────────────────────────────┘
                                   │
                         trigger_cross_channel?
                                   │ YES
                                   ▼
                         follow_up_queue (Supabase)
                    ┌──────────────┴──────────────┐
                    │                             │
               target=email                  target=voice
               target=linkedin                    │
                    │                             ▼
                    ▼                       Flowtics AI
              FollowUp Queue                Call Agent
              Dispatcher                   (every 4 hrs)
              (every 15 min)
                    │
                    ▼
              Centralized
              Follow-Up Coordinator
         ┌──────────┴───────────┐
         │                      │
       Email              LinkedIn
       (Gmail)         Scenario A: reply to existing convo
                       Scenario B: campaign enroll → send direct
                    │
                    ▼ (Queue Next Follow-Up? — up to 2 per channel)
              next queue row inserted if < 2 sent
```

**MCO Fetch Context** called by: Coordinator, Connection Accepted Handler, Gmail Reply Agent, Flowtics Call Agent

**MCO Write Event** called by: Aimfox Reply Agent, Gmail Reply Agent, Connection Accepted Handler, Coordinator, Post Call Analysis

---

## 8. Current System Status

| Workflow | Status | Nodes | Trigger |
|----------|--------|-------|---------|
| MCO - Write Conversation Event | ACTIVE | 21 | Webhook |
| MCO - Fetch Cross-Channel Context | ACTIVE | 5 | Webhook |
| MCO - FollowUp Queue Dispatcher | ACTIVE | 9 | Cron every 15 min |
| MCO - Centralized Follow-Up Coordinator | ACTIVE | 24 | Webhook (Dispatcher) |
| MCO - Aimfox Connection Accepted Handler | ACTIVE | 7 | Aimfox `accepted` webhook |
| Aimfox Nextus AI Reply Agent — MCO | ACTIVE | 37 | Aimfox webhook |
| MCO - Gmail Reply Agent | ACTIVE | 18 | Gmail trigger |
| Flowtics AI Call Agent - MCO | ACTIVE | 12 | Cron every 4 hours |
| Post Call Analysis - MCO | ACTIVE | 4 | Retell webhook |
| Aimfox Data Fetching - MCO | ACTIVE | 4 | Aimfox webhook |
| MCO - Aimfox Responded | ACTIVE | 2 | Aimfox webhook |

---

## 9. Data Flow Per Lead Intent

**New lead marks interested on LinkedIn:**
1. Aimfox Reply Agent → AI replies on LinkedIn
2. Write First Interested → seeds full thread + inbound (interested) + outbound
3. follow_up_queue gets 2 rows: email (3 min) + voice (3 min)
4. Dispatcher picks up email row → Coordinator sends personalised email via Gmail
5. Flowtics picks up voice row (next 4-hour window) → places Retell call
6. Post Call Analysis logs call → Supabase updated
7. Lead replies to email → Gmail Agent → Slack approval → reply sent + logged
8. Any reply on any channel → pending follow-ups for that channel rescheduled +3 min
9. After 2 sent follow-ups per channel → no more queued for that channel

**Returning lead:**
1. Any new reply on any channel → Write Returning (or respective channel handler)
2. Context is fetched before every reply — full cross-channel history always used
3. No cross-channel trigger unless explicitly set

---

## 10. Known Gaps and Pending Items

| Item | Detail |
|------|--------|
| **LinkedIn not in target_channels** | `target_channels` currently `['email', 'voice']` in Write First Interested node. LinkedIn follow-up not yet activated — Scenario C (campaign enroll) fires only when Coordinator explicitly receives a linkedin queue row. To activate: add `'linkedin'` to target_channels and decide delay relative to email/voice |
| **Sequential channel logic** | All channels fire independently at the same time. Desired: email first → LinkedIn second → voice third. Not yet implemented |
| **phone_e164 for LinkedIn leads** | LinkedIn leads have no phone number. Voice follow-ups queued but skipped by phone guard. Needs Clay enrichment or manual population |
| **Aimfox MCP OAuth** | Added to settings.json. Requires OAuth login on first use — restart session and run `/mcp` to authenticate |
| **Retell webhook test** | Hit "Test" in Retell dashboard to confirm Post Call Analysis receives and processes correctly |
| **Gmail Disapprove path** | If Slack approval denied, workflow stops silently with no log. Could write `disapproved` note to Supabase |
| **Production delay** | All follow-up delays currently 3 min for testing. Change `3*60*1000` → `30*60*1000` in `Build Queue Rows` (Write Event) and `Queue Next Follow-Up?` (Coordinator) before going live |
| **Stale queue rows** | 2 stale test rows in follow_up_queue (mco-test-lead@example.com, May 4). Clear with: `DELETE FROM follow_up_queue WHERE lead_email LIKE '%test%' AND status = 'pending'` |
| **Instantly AI integration (planned)** | When a lead shows interest in Instantly AI cold email sequence, Instantly AI CCs the Flowtics.ai Gmail address. Gmail Reply Agent takes over the thread (has full prior context via CC). MCO follow-up queue activates from there. Need to ensure Instantly AI stops monitoring thread after CC handover. Requires a database of interested lead emails to correctly identify the client vs Instantly AI sender |
