# MCO Client Configuration & Onboarding Guide

> **Purpose:** Every time you onboard a new client, this document tells you exactly what to change, in what order, and how. Nothing starts from scratch — the system is already built. You are only swapping out values.

---

## The Core Principle

The MCO system has two types of settings:

| Type | What it is | How to change |
|---|---|---|
| **Secrets** | API keys, OAuth tokens, passwords | n8n Credentials — one-time setup per client, requires n8n admin |
| **Operational config** | Sender name, delays, campaign IDs, tone | n8n Variables — change once, all workflows pick it up immediately |

You never touch workflow code for a new client. You update Variables and Credentials only.

---

## Part 1 — n8n Variables (Operational Config)

**Where:** n8n → Settings → Variables  
**How to update via API (no UI needed):**
```
PATCH https://n8n-1404.n8n.whiteserverdns.com/api/v1/variables/:id
{ "value": "new value" }
```

These are the variables to create. Every workflow reads from `$vars.*` — change the variable once and every workflow reflects it on the next run.

| Variable Name | Description | Example Value |
|---|---|---|
| `sender_name` | Name shown in outbound emails and LinkedIn messages | `Anik Hanik` |
| `sender_email` | Sending Gmail address | `team@flowticsai.com` |
| `company_name` | Client's company name, used in AI prompts | `Flowtics AI` |
| `booking_link` | Calendly or meeting link injected into AI replies | `https://calendly.com/mahfujurrahman511351/30min` |
| `ai_tone` | Tone instruction injected into all AI system prompts | `professional, concise, consultative` |
| `linkedin_campaign_id` | Aimfox campaign ID for connection requests | `39a3ee2f-8474-4e9a-b498-81d14e319788` |
| `aimfox_account_id` | Aimfox account sending LinkedIn messages | *(from Aimfox dashboard)* |
| `linkedin_delay_hours` | Hours before first LinkedIn follow-up after connection | `24` |
| `voice_retry_delay_hours` | Hours before retry if call not answered | `4` |
| `instantly_reply_to` | Email address Instantly campaigns should Reply-To | `team@flowticsai.com` |

**Note:** n8n Variables are currently instance-wide. If you run two clients on the same n8n instance, prefix variable names: `clientA_sender_name`, `clientB_sender_name`. Alternatively, clone the workflow set per client (see Part 4).

---

## Part 2 — n8n Credentials (Secrets, One-Time Per Client)

**Where:** n8n → Settings → Credentials → Add Credential  
These cannot be changed via the Variables API — they require n8n admin access and are stored encrypted.

| Credential | Type | What to configure | Used by |
|---|---|---|---|
| Gmail | OAuth2 | Connect the client's sending Gmail account | Coordinator, Gmail Reply Agent |
| Anthropic | API Key | Client's Anthropic API key (or share one key across clients) | Coordinator, Gmail Reply Agent |
| Aimfox | HTTP Header Auth | `Authorization: Bearer <aimfox_token>` | Connection Accepted Handler, Coordinator, Reply Agent |
| Supabase | Supabase | Project URL + service role key | All workflows |
| Retell AI | HTTP Header Auth | `Authorization: Bearer <retell_key>` | Call Agent |
| Slack | OAuth | Slack workspace for notifications | Gmail Reply Agent, Aimfox Reply Agent |
| Google Sheets | OAuth2 | Sheet used by Aimfox Reply Agent for conversation state | Aimfox Reply Agent |

**After adding each credential:** open every affected workflow and reassign the credential in the relevant nodes. This is a one-time step per client.

---

## Part 3 — Instantly AI Setup (Per Client)

Instantly AI does not need a dedicated n8n workflow. The Gmail Reply Agent handles everything — as long as Instantly is configured to forward replies to the client's Gmail.

**Steps in Instantly (per client, per campaign):**

1. Open the campaign → Settings → Sending Settings
2. Set **Reply-To** = `{{ sender_email }}` (the client's Gmail address, e.g. `team@flowticsai.com`)
3. Save

That is the entire Instantly integration. When a lead replies:
- Instantly forwards the reply to the client's Gmail
- Gmail Reply Agent fires
- It reads the full Gmail thread (for context) and generates a reply in the same thread
- Event is logged to Supabase

**Optional — log the full thread to Supabase on first interest:**  
When a lead replies with intent = `interested`, the Gmail Reply Agent should read the full Gmail thread and log each prior message (from Instantly's outbound sequence) to Supabase using `POST /mco-write-event` with `event_id = gmail_message_id` (dedup-safe). This gives LinkedIn and Voice agents full context of what was said before they ever spoke to the lead. This step is not yet built but is low complexity — one `Get Thread` node in Gmail Reply Agent.

---

## Part 4 — Multiple Clients on the Same n8n Instance

Two approaches depending on scale:

### Option A: Shared workflows with namespaced Variables (up to ~3 clients)
- All clients share the same workflow set
- Variables are namespaced: `clientA_sender_name`, `clientB_sender_name`
- Workflows read the right variable using a `client_id` passed in the webhook payload or queue row
- **Requires:** `client_id` column in `follow_up_queue` and `leads` tables; Config node reads the right prefixed variable
- Best when clients are similar and you want one place to manage everything

### Option B: Cloned workflow set per client (simplest, recommended to start)
- Duplicate all workflows in n8n for each new client
- Update the **Config node** in each cloned workflow with that client's hardcoded values
- Each client's workflows are independent — no risk of one client's change affecting another
- **Config node is one node per workflow** — 10 workflows × 1 Config node = 10 edits total per new client
- Best when clients have significantly different setups or you want clean separation

**Recommended starting point:** Option B. When you hit 3+ clients, migrate to Option A with a `client_config` Supabase table.

---

## Part 5 — Dashboard (Retool)

Retool connects to Supabase natively. The planned dashboard has two uses: **operations** (viewing leads and conversations) and **config** (changing Variables without opening n8n).

### Tab 1: Leads (already planned)
- Table: `SELECT lead_email, full_name, company, overall_intent, last_active_channel, last_activity_at FROM leads ORDER BY last_activity_at DESC`
- Filters: intent dropdown, channel dropdown, name/company search
- Click a lead → conversation timeline panel on the right
- Timeline: `SELECT channel, direction, timestamp, content, sender_name, intent FROM conversations WHERE lead_id = '...' ORDER BY timestamp ASC`

### Tab 2: Queue Health
- Table: `SELECT queue_id, lead_email, target_channel, status, scheduled_for, created_at FROM follow_up_queue ORDER BY scheduled_for DESC LIMIT 100`
- Quick view: how many pending, sent, skipped, failed — use a summary row at the top
- Useful for spotting stuck or overdue queue items

### Tab 3: Client Config (n8n Variables via API)
A simple form with one field per Variable. On save, calls the n8n Variables API:
```
PATCH /api/v1/variables/:id   { "value": "..." }
```

Fields to expose in the form:

| Field label | n8n Variable |
|---|---|
| Sender name | `sender_name` |
| Sender email | `sender_email` |
| Booking link | `booking_link` |
| AI tone | `ai_tone` |
| LinkedIn follow-up delay (hours) | `linkedin_delay_hours` |
| Voice retry delay (hours) | `voice_retry_delay_hours` |
| LinkedIn campaign ID | `linkedin_campaign_id` |

**What NOT to expose in the dashboard:** API keys, OAuth tokens, Supabase keys — these stay in n8n Credentials only.

---

## Part 6 — New Client Onboarding Checklist

Work through this in order. Estimated time: 45–60 minutes per client.

### Step 1 — Credentials (n8n, ~20 min)
- [ ] Add Gmail credential for client's sending address
- [ ] Add Aimfox credential (API token from client's Aimfox account)
- [ ] Add Retell credential (if client is using voice)
- [ ] Confirm Anthropic credential (share existing or add new)
- [ ] Add Supabase credential (share existing project or create new one for the client)

### Step 2 — Supabase (5 min, only if new project)
- [ ] Run `tools/supabase_setup.sql` in Supabase SQL editor
- [ ] Confirm 4 tables created: `leads`, `conversations`, `follow_up_queue`, `phone_map`

### Step 3 — n8n Variables (5 min)
- [ ] Set `sender_name`
- [ ] Set `sender_email`
- [ ] Set `company_name`
- [ ] Set `booking_link`
- [ ] Set `ai_tone`
- [ ] Set `linkedin_campaign_id` (from client's Aimfox)
- [ ] Set `aimfox_account_id` (from client's Aimfox)
- [ ] Set `linkedin_delay_hours` (default: `24`)
- [ ] Set `voice_retry_delay_hours` (default: `4`)
- [ ] Set `instantly_reply_to` (client's Gmail address)

### Step 4 — Workflows (15 min)
- [ ] If using Option B (cloned workflows): duplicate all active workflows in n8n, rename with client prefix
- [ ] In each workflow's Config node: update credential references to the new client's credentials
- [ ] Activate all cloned workflows
- [ ] Test: send a POST to `/mco-write-event` with a test lead → confirm row appears in Supabase

### Step 5 — Instantly (5 min, per campaign)
- [ ] Open each Instantly campaign → set Reply-To = client's Gmail address
- [ ] Confirm first test reply lands in Gmail and Gmail Reply Agent fires

### Step 6 — Aimfox (5 min)
- [ ] Confirm Aimfox webhook (accepted event) points to `/webhook/mco-aimfox-accepted`
- [ ] Confirm Aimfox reply webhook points to the Aimfox Reply Agent webhook URL
- [ ] Confirm campaign ID in n8n Variable `linkedin_campaign_id` matches the live campaign

### Step 7 — Retell (5 min, if using voice)
- [ ] Confirm Retell agent ID and phone number are set in Call Agent workflow
- [ ] Update `booking_link` Variable to client's booking URL
- [ ] Test: trigger Call Agent webhook with a test lead phone number

### Step 8 — Verify end-to-end
- [ ] Run `python tools/test_workflows.py` (update `.env` with new client's webhook URLs first)
- [ ] Check Supabase: leads and conversations tables have test data
- [ ] Check follow_up_queue: test cross-channel trigger created a pending row
- [ ] Confirm queue row gets dispatched within 15 minutes (Dispatcher fires)

---

## What Never Changes Between Clients

- The Supabase schema (same tables, same RPCs)
- The Write Event and Fetch Context workflow logic
- The Coordinator's routing logic (email / LinkedIn DM / LinkedIn campaign / voice)
- The Post Call Analysis retry logic
- The Dispatcher's queue polling logic
- The dedup logic (`event_id`)
- The intent promotion order (`unknown → no_action → not_interested → referral → interested → booking`)

These are the core of the system. Client-specific values are only ever in Variables, Credentials, and Config nodes.
