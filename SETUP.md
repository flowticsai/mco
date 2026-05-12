# MCO Unified Outreach — Setup Guide

Follow these steps in order. Each phase builds on the previous one.

---

## Phase 1: Supabase (15 min)

### 1.1 Create the project
1. Go to [supabase.com](https://supabase.com) → Sign in → New Project
2. Name: `MCO Outreach` | Region: closest to you
3. Set a strong DB password and save it
4. Wait ~2 minutes for provisioning

### 1.2 Run the SQL setup
1. In your project → **SQL Editor** → New Query
2. Open `tools/supabase_setup.sql` from this folder
3. Paste the entire file → **Run**
4. Confirm the last SELECT returns 4 rows: `conversations`, `follow_up_queue`, `leads`, `phone_map`

### 1.3 Collect credentials
From your Supabase project → **Settings** → **API**:
- **Project URL**: `https://[ref].supabase.co`
- **service_role key**: (secret key, NOT the anon key)

---

## Phase 2: n8n Credential Setup (5 min)

1. Go to `nextus.app.n8n.cloud` → **Settings** → **Credentials** → Add Credential
2. Search: **Supabase**
3. Fill in:
   - Host: your Project URL
   - Service Role Secret: service_role key
4. Name it: `MCO Supabase` → Save & Test

---

## Phase 3: Import n8n Sub-Workflows (10 min)

Import both files from the `n8n_workflows/` folder:

### MCO - Write Conversation Event
1. n8n → Workflows → **⋮** menu → Import from File
2. Select `n8n_workflows/MCO_Write_Conversation_Event.json`
3. Open the imported workflow
4. For every **Supabase** node: click it → change credential to `MCO Supabase`
5. For every **Monday.com** node: confirm credential is `XPrpJrLPDAOzNWgi`
6. **Activate** the workflow (toggle top-right)
7. Copy the **Production Webhook URL** from the Webhook node — save this as `WRITE_EVENT_WEBHOOK_URL`

### MCO - Fetch Cross-Channel Context
1. Import `n8n_workflows/MCO_Fetch_Cross_Channel_Context.json`
2. Update Supabase credential to `MCO Supabase` in both Supabase nodes
3. **Activate** the workflow
4. Copy the **Production Webhook URL** — save this as `FETCH_CONTEXT_WEBHOOK_URL`

---

## Phase 4: Monday.com — Add New Columns (5 min)

Open board `18399476470` and add these columns (do not touch existing columns):

| Name | Type | Options |
|------|------|---------|
| `last_active_channel` | Text | — |
| `overall_intent` | Status | unknown · no_action · not_interested · referral · already_have_contract · interested · booking |
| `first_channel` | Text | — |
| `phone_e164` | Text | — |

---

## Phase 5: Retool Dashboard (20 min)

1. Go to [retool.com](https://retool.com) → Create free account
2. **Resources** → Add New → **Supabase**
   - Name: `MCO Supabase`
   - Host: your Project URL
   - API Key: service_role key
3. **Create App** → Blank App → name it `MCO Outreach Dashboard`

### Left panel (30% width) — Leads Table
- Add a **Table** component
- Data source — custom query:
  ```sql
  SELECT lead_email, full_name, company, overall_intent,
         last_active_channel, last_activity_at
  FROM leads
  ORDER BY last_activity_at DESC NULLS LAST
  ```
- Add a **Select** filter for `overall_intent`
- Add a **Text Input** for search on `full_name` / `company`

### Right panel (70% width) — Conversation Timeline
- Add a **Container**
- Inside: a **Text** header showing `{{ leads_table.selectedRow.data.full_name }}`
- Add a **Listview** component:
  - Data — custom query:
    ```sql
    SELECT channel, direction, timestamp, content, sender_name, intent
    FROM conversations
    WHERE lead_email = {{ leads_table.selectedRow.data.lead_email }}
    ORDER BY timestamp ASC
    ```
  - Item template:
    - Title: `{{ item.channel.toUpperCase() }} · {{ item.direction }} · {{ new Date(item.timestamp).toUTCString() }}`
    - Body: `{{ item.content }}`
    - Footer: `Intent: {{ item.intent }}`
  - Color-code background by channel (use Conditional Styles):
    - email → light blue
    - linkedin → light purple
    - voice → light green
    - sms → light orange
- Set **Refresh interval**: 60 seconds on the conversations listview

---

## Phase 6: Test End-to-End

Test the Write Event sub-workflow with a sample POST:

```bash
curl -X POST {WRITE_EVENT_WEBHOOK_URL} \
  -H "Content-Type: application/json" \
  -d '{
    "event_id": "11111111-1111-1111-1111-111111111111",
    "lead_email": "test@example.com",
    "timestamp": "2026-05-04T10:00:00Z",
    "channel": "linkedin",
    "direction": "inbound",
    "content": "Hi, I saw your message and I am interested in learning more about your service.",
    "content_type": "message",
    "sender_name": "Test Lead",
    "intent": "interested",
    "full_name": "Test Lead",
    "company": "Test Corp"
  }'
```

**Expected results:**
- [ ] Response: `{ "status": "ok", "monday_item_id": "...", "overall_intent": "interested" }`
- [ ] Row in Supabase `conversations` table
- [ ] Row in Supabase `leads` table (overall_intent = interested)
- [ ] New Monday.com item created on board 18399476470
- [ ] Monday.com item has an Update posted with 💼 LINKEDIN emoji
- [ ] Retool dashboard shows the lead in the list and the event in the timeline

Test the Fetch Context sub-workflow:

```bash
curl -X POST {FETCH_CONTEXT_WEBHOOK_URL} \
  -H "Content-Type: application/json" \
  -d '{
    "lead_email": "test@example.com",
    "requesting_channel": "email"
  }'
```

**Expected results:**
- [ ] Response contains `context_block` with the LinkedIn event formatted with 💼 emoji
- [ ] `overall_intent` = "interested"
- [ ] `event_count` = 1

---

## Phase 7: Wire Into Existing Workflows

After both sub-workflows are tested and working:

### In Aimfox Nextus AI Reply Agent:
1. Before the **Decision Maker Agent** node:
   - Add **HTTP Request** → POST to `FETCH_CONTEXT_WEBHOOK_URL`
   - Body: `{ "lead_email": "{{lead_email}}", "requesting_channel": "linkedin" }`
   - Append the returned `context_block` to the Decision Maker Agent system prompt
2. After **Send Reply** and **Send Followup** nodes:
   - Add **HTTP Request** → POST to `WRITE_EVENT_WEBHOOK_URL`
   - Map all fields (channel: "linkedin", direction: "outbound", etc.)
3. After **Text Classifier** node (inbound messages):
   - Add **HTTP Request** → POST to `WRITE_EVENT_WEBHOOK_URL`
   - Map direction: "inbound", intent from classifier output

### In Outcraft Reply Agent (Email):
1. Before the **Reply Agent (Claude)** node:
   - Add **HTTP Request** → POST to `FETCH_CONTEXT_WEBHOOK_URL`
   - Body: `{ "lead_email": "{{from_email}}", "requesting_channel": "email" }`
2. After **Send Mail** node:
   - Add **HTTP Request** → POST to `WRITE_EVENT_WEBHOOK_URL`
   - Map: channel: "email", direction: "outbound"
3. On webhook entry (inbound email):
   - Add early **HTTP Request** → POST to `WRITE_EVENT_WEBHOOK_URL`
   - Map: direction: "inbound"

---

## Next Steps (after Retell AI JSONs are shared)
- Build `MCO - Retell Webhook Handler` workflow
- Build `MCO - Clay Enrichment Receiver` workflow
- Build `MCO - FollowUp Queue Dispatcher` workflow (Schedule trigger, every 15 min)
