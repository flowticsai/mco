# MCO System — Connections Reference
### Every component, URL, credential, and data flow in one place

---

## Webhook URLs (Entry Points)

These are the URLs other systems call to talk to MCO.

| Workflow | URL | Method |
|---|---|---|
| Write Conversation Event | `https://nextus.app.n8n.cloud/webhook/mco-write-event` | POST |
| Fetch Cross-Channel Context | `https://nextus.app.n8n.cloud/webhook/mco-fetch-context` | POST |
| Centralized Follow-Up Coordinator | `https://nextus.app.n8n.cloud/webhook/mco-followup` | POST |
| Aimfox Connection Accepted Handler | `https://nextus.app.n8n.cloud/webhook/mco-aimfox-accepted` | POST |
| FollowUp Queue Dispatcher | *(no webhook — triggered by schedule every 15 min)* | — |

---

## Supabase

**Project URL:** `https://hkqssbomrcbtfbdowtgj.supabase.co`  
**Auth:** Service role key (stored inside every MCO workflow's Config/Setup node)

### Tables

| Table | Purpose | Who writes | Who reads |
|---|---|---|---|
| `leads` | One row per lead. Stores name, company, email, LinkedIn, phone, intent, Monday item ID | Write Event (via `upsert_lead` RPC) | Fetch Context (via `fetch_lead_context` RPC) |
| `conversations` | One row per message, any channel | Write Event (via `insert_conversation_event` RPC) | Fetch Context (via `fetch_lead_context` RPC) |
| `follow_up_queue` | Scheduled follow-ups waiting to fire | Write Event (when `trigger_cross_channel: true`) | Queue Dispatcher (every 15 min) |
| `phone_map` | Maps phone numbers to email addresses | Write Event (when `phone_e164` is present) | Retell integration (future) |

### Postgres RPC Functions

These are called directly by MCO workflows — not raw SQL.

| Function | Called by | What it does |
|---|---|---|
| `upsert_lead` | Write Event | Creates or updates lead record. Applies intent promotion (never demotes: interested stays interested even if a later message says unknown) |
| `insert_conversation_event` | Write Event | Inserts one conversation row. Dedup built in — same `event_id` submitted twice is silently ignored |
| `fetch_lead_context` | Fetch Context | Returns lead record + last N conversation rows ordered by timestamp descending |

---

## The Five MCO Workflows

---

### 1. Write Conversation Event
**n8n ID:** `GUUEUvfjFwLojoA0`  
**Status:** Active

**Triggered by:** Any external workflow calling `POST /mco-write-event`

**Callers:**
- Your Outcraft Reply Agent (reply received + reply sent)
- Your Outcraft Interested Agent (lead marked interested)
- Centralized Follow-Up Coordinator (after sending a follow-up)
- Aimfox Connection Accepted Handler (after sending first LinkedIn message)
- Your Aimfox Reply Agent (reply received + reply sent) *(nodes to be added)*

**What it does, in order:**
1. Validates and normalises the incoming payload
2. Calls Supabase `upsert_lead` → creates or updates the lead record with intent promotion
3. Retrieves the Monday.com item ID for the lead (creates a Monday item if none exists)
4. Calls Supabase `insert_conversation_event` → logs the message (dedup safe)
5. If `phone_e164` present → upserts into `phone_map`
6. Posts an update to the Monday.com item (channel emoji + message content + intent)
7. Updates Monday.com item columns (overall intent, last active channel)
8. If `trigger_cross_channel: true` → inserts a row into `follow_up_queue` for each target channel

**Writes to:**
- Supabase `leads`
- Supabase `conversations`
- Supabase `follow_up_queue` (conditional)
- Supabase `phone_map` (conditional)
- Monday.com board `18399476470`

**Required fields in payload:**

| Field | Required | Notes |
|---|---|---|
| `event_id` | Yes | Unique per message. Use platform's native message/reply ID. Dedup key. |
| `lead_email` | Yes | Canonical identifier. Lowercased automatically. |
| `channel` | Yes | `email`, `linkedin`, `voice`, or `sms` |
| `direction` | Yes | `inbound` or `outbound` |
| `content` | Yes | The message text |
| `timestamp` | Yes | ISO 8601 |
| `intent` | No | `interested`, `not_interested`, `booking`, `referral`, `no_action`, `unknown` |
| `full_name` | No | Used to create/update lead record |
| `company` | No | Used to create/update lead record |
| `linkedin_profile_url` | No | Stored on lead record |
| `phone_e164` | No | Stored in phone_map |
| `trigger_cross_channel` | No | Set `true` to schedule a follow-up on another channel |
| `target_channels` | No | Array e.g. `["email", "linkedin"]` — used when `trigger_cross_channel: true` |
| `follow_up_context` | No | Short note about why the follow-up is being scheduled |

---

### 2. Fetch Cross-Channel Context
**n8n ID:** *(set on first activation)*  
**Status:** Active

**Triggered by:** Any workflow that needs a lead's conversation history before writing a message

**Callers:**
- Centralized Follow-Up Coordinator (before generating follow-up)
- Aimfox Connection Accepted Handler (before generating first LinkedIn message)

**What it does, in order:**
1. Validates payload — requires `lead_email`
2. Calls Supabase `fetch_lead_context` RPC — returns lead record + last N conversations
3. Formats a plain-text context block with timeline, channel icons, intent labels

**Returns:**
```json
{
  "context_block": "=== Cross-Channel Conversation History ===\n...",
  "lead": { "full_name": "...", "company": "...", "overall_intent": "interested", ... },
  "event_count": 7,
  "overall_intent": "interested",
  "lead_email": "lead@company.com"
}
```

**Required fields in payload:**

| Field | Required | Notes |
|---|---|---|
| `lead_email` | Yes | |
| `requesting_channel` | No | If provided, prepends a line like "You are replying on LINKEDIN. Prior cross-channel context below." |
| `max_events` | No | Defaults to 20 |

---

### 3. FollowUp Queue Dispatcher
**n8n ID:** `BQJkE0sa0yRKWDjM`  
**Status:** Inactive — needs manual activation

**Triggered by:** Schedule — every 15 minutes (n8n built-in scheduler)

**What it does, in order:**
1. Reads Supabase `follow_up_queue` — rows where `status = pending` AND `scheduled_for <= now`
2. If no rows → stops (nothing to do)
3. For each pending row:
   - If `target_channel = linkedin` → fetches latest LinkedIn metadata from `conversations` table (to get `aimfox_account_id` and `conversation_urn`)
   - Calls Centralized Follow-Up Coordinator with the queue row data

**Reads from:** Supabase `follow_up_queue`, Supabase `conversations` (LinkedIn metadata only)  
**Calls:** `POST https://nextus.app.n8n.cloud/webhook/mco-followup`

---

### 4. Centralized Follow-Up Coordinator
**n8n ID:** `R9bkR97Xt5fHSN4K`  
**Status:** Built — needs Gmail + Anthropic credentials linked, then activation

**Triggered by:** Queue Dispatcher calling `POST /mco-followup`

**What it does, in order:**
1. Receives queue row (lead email, target channel, trigger channel, context note)
2. Calls Fetch Context → gets full conversation history for the lead
3. Routes to the correct channel branch (Switch node):

**Email branch:**
- Claude (120-word max, professional email format)
- Sends via Gmail from `anik@nextus.ai`
- Logs sent message to Supabase via Write Event
- Marks queue row as `sent` in Supabase

**LinkedIn branch:**
- Claude (80-word max, casual conversational format)
- Checks if LinkedIn profile URL exists (IF node)
- Sends via Aimfox campaign API (adds to MCO Follow-Up campaign with `{{CUSTOM_MESSAGE}}` variable)
- Logs sent message to Supabase via Write Event
- Marks queue row as `sent` in Supabase

**Calls:**
- `POST https://nextus.app.n8n.cloud/webhook/mco-fetch-context`
- Anthropic Claude API (credential: `tFYLbQt9S6IzWYNd`)
- Gmail API (credential: to be linked)
- `POST https://api.aimfox.com/api/v2/accounts/:id/campaigns/REPLACE_WITH_CAMPAIGN_ID/audience/multiple`
- `POST https://nextus.app.n8n.cloud/webhook/mco-write-event`
- `PATCH https://hkqssbomrcbtfbdowtgj.supabase.co/rest/v1/follow_up_queue` (mark sent)

**Pending setup:**
- `AIMFOX_FOLLOWUP_CAMPAIGN_ID` placeholder in Setup node — replace once campaign is created
- Gmail OAuth2 credential — link manually in n8n on the Send Email node
- Anthropic credential — link manually on both Claude Model nodes

---

### 5. Aimfox Connection Accepted Handler
**n8n ID:** `8MxgrCTDN6IZ98iF`  
**Status:** Active

**Triggered by:** Aimfox webhook — fires when a lead accepts a LinkedIn connection request  
**Aimfox webhook ID:** `8ed8df14` (registered, event type: `accepted`)

**What it does, in order:**
1. Receives Aimfox `accepted` event payload
2. Extracts `account_id`, `lead_id`, `lead_urn`, `lead_name`, `linkedin_profile_url`
3. Calls Aimfox custom variables API → retrieves `LEAD_EMAIL` stored when lead was added to campaign
4. If no `LEAD_EMAIL` found → returns 200 (skipped — lead did not originate from email)
5. If `LEAD_EMAIL` found → calls Fetch Context for that email → gets full email conversation history
6. Claude writes a warm, context-aware first LinkedIn message (80 words max, references prior email)
7. Sends message via Aimfox Start Conversation API
8. Logs sent message to Supabase via Write Event

**Calls:**
- `GET https://api.aimfox.com/api/v2/accounts/:account_id/leads/:lead_urn/custom-variables`
- `POST https://nextus.app.n8n.cloud/webhook/mco-fetch-context`
- Anthropic Claude API (credential: `tFYLbQt9S6IzWYNd`)
- `POST https://api.aimfox.com/api/v2/accounts/:account_id/conversations`
- `POST https://nextus.app.n8n.cloud/webhook/mco-write-event`

---

## External Platform Connections

### Monday.com
**Board ID:** `18399476470`  
**Token:** Stored inside Write Event's Setup node  
**Used by:** Write Event only  
**What MCO does:** Creates items for new leads, posts conversation updates, updates intent + channel columns

### Gmail
**Account:** `anik@nextus.ai`  
**Used by:** Centralized Follow-Up Coordinator (email branch)  
**Status:** Credential not yet linked in n8n — must be done manually

### Aimfox
**API Base:** `https://api.aimfox.com/api/v2`  
**Token:** `Bearer 8e65df8c-3fe2-4ecf-bf05-8261ea85464b`  
**Used by:** Connection Accepted Handler, Centralized Follow-Up Coordinator  

Aimfox endpoints MCO calls:

| Endpoint | Used by | Purpose |
|---|---|---|
| `GET /accounts/:id/leads/:urn/custom-variables` | Connection Accepted Handler | Retrieve LEAD_EMAIL stored when lead was added to campaign |
| `POST /accounts/:id/conversations` | Connection Accepted Handler | Send first LinkedIn message to a new connection |
| `POST /accounts/:id/campaigns/:campaign_id/audience/multiple` | Coordinator + Interested workflow | Add email lead to LinkedIn campaign with LEAD_EMAIL custom variable |

### Anthropic (Claude)
**Credential ID in n8n:** `tFYLbQt9S6IzWYNd`  
**Model:** `claude-sonnet-4-6`  
**Used by:** Connection Accepted Handler, Centralized Follow-Up Coordinator  

---

## How Workflows Call Each Other

```
Your Outcraft Reply Agent
  └── POST /mco-write-event  (inbound message)
  └── POST /mco-write-event  (outbound reply)

Your Outcraft Interested Agent
  └── POST /mco-write-event  (lead + intent=interested)
      └── [if linkedin_profile_url] POST Aimfox → add to campaign

Your Aimfox Reply Agent
  └── POST /mco-write-event  (inbound message)
  └── POST /mco-write-event  (outbound reply)

Aimfox (connection accepted event)
  └── POST /mco-aimfox-accepted
      └── GET Aimfox custom variables
      └── POST /mco-fetch-context
      └── Anthropic Claude
      └── POST Aimfox start conversation
      └── POST /mco-write-event

Schedule (every 15 min)
  └── Dispatcher reads Supabase follow_up_queue
      └── POST /mco-followup (Coordinator)
          └── POST /mco-fetch-context
          └── Anthropic Claude
          └── Gmail OR Aimfox campaign
          └── POST /mco-write-event
          └── PATCH Supabase follow_up_queue (mark sent)
```

---

## Credentials Reference

| Service | Value / Location | Used in |
|---|---|---|
| Supabase service key | Inside every MCO workflow's Setup/Config node | All 5 MCO workflows |
| Monday.com token | Inside Write Event's Setup node | Write Event |
| Anthropic (Claude) | n8n credential ID: `tFYLbQt9S6IzWYNd` | Connection Accepted Handler, Coordinator |
| Aimfox API token | `Bearer 8e65df8c-3fe2-4ecf-bf05-8261ea85464b` — hardcoded in nodes | Connection Accepted Handler, Coordinator |
| Gmail OAuth2 | To be linked in n8n on Send Email node | Coordinator |

---

## Pending Connections (Not Yet Wired)

| What | Where | Who does it |
|---|---|---|
| Create Aimfox "MCO LinkedIn Follow-Up" campaign | Aimfox dashboard | You |
| Replace `AIMFOX_FOLLOWUP_CAMPAIGN_ID` in Coordinator Setup node | n8n workflow `R9bkR97Xt5fHSN4K` | Me (once you share the campaign ID) |
| Link Gmail credential on Send Email node | n8n workflow `R9bkR97Xt5fHSN4K` | You |
| Link Anthropic credential on both Claude Model nodes in Coordinator | n8n workflow `R9bkR97Xt5fHSN4K` | You |
| Activate Queue Dispatcher | n8n workflow `BQJkE0sa0yRKWDjM` | You |
| Add 2 Write Event calls to Outcraft Reply Agent | Your existing workflow | You (instructions in integration guide) |
| Add 1 Write Event call + Aimfox campaign add to Outcraft Interested Agent | Your existing workflow | You (instructions in integration guide) |
| Add 2 Write Event calls to Aimfox Nextus AI Reply Agent | Your existing workflow | You (instructions in integration guide) |
| Retell AI integration (voice + SMS) | New MCO workflow | Me (waiting for Retell workflow JSONs) |
| Retool dashboard | New Retool app | Me (after data starts flowing) |
