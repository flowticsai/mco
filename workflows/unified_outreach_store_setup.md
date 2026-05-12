# Unified Outreach Store Setup

## Objective
One-time setup guide for the MCO Unified Outreach infrastructure: Supabase database, n8n credential, Monday.com new columns, and Retool dashboard.

---

## Step 1: Create Supabase Project

1. Go to [supabase.com](https://supabase.com) → New Project
2. Name: `MCO Outreach`
3. Choose region closest to you (EU West or US East)
4. Set a strong database password (save it — you won't need it often but keep it)
5. Wait ~2 minutes for project to provision

**Collect these values (you'll need them for n8n):**
- Project URL: `https://[your-project-ref].supabase.co`
- Service Role Key: Settings → API → `service_role` key (not the anon key)

---

## Step 2: Run the SQL Setup Script

1. In your Supabase project → SQL Editor → New Query
2. Open `tools/supabase_setup.sql` from this project directory
3. Paste the entire contents into the SQL Editor
4. Click **Run**
5. Confirm the final SELECT returns 4 rows: `conversations`, `follow_up_queue`, `leads`, `phone_map`

---

## Step 3: Add Supabase Credential to n8n

1. In n8n (`nextus.app.n8n.cloud`) → Settings → Credentials → Add Credential
2. Search for **Supabase**
3. Fill in:
   - **Host**: your Project URL (e.g. `https://abcdefgh.supabase.co`)
   - **Service Role Secret**: the `service_role` key from Step 1
4. Name it: `MCO Supabase`
5. Save and test

---

## Step 4: Add New Columns to Monday.com Board 18399476470

In Monday.com, open board `18399476470` and add these columns:

| Column Name | Column Type | Options |
|-------------|-------------|---------|
| `last_active_channel` | Text | — |
| `overall_intent` | Status | unknown, no_action, not_interested, referral, already_have_contract, interested, booking |
| `first_channel` | Text | — |
| `phone_e164` | Text | — |

Note: Do not remove or rename any existing columns.

---

## Step 5: Import n8n Sub-Workflows

Import both files from `n8n_workflows/` into n8n:

1. n8n → Workflows → Import from file
2. Import `MCO_Write_Conversation_Event.json`
3. Import `MCO_Fetch_Cross_Channel_Context.json`
4. For each imported workflow:
   - Open it and update the Supabase credential to `MCO Supabase`
   - Update the Monday.com credential to `XPrpJrLPDAOzNWgi`
   - Activate the workflow
   - Copy the webhook URL from the Webhook node (Production URL)
   - Save the webhook URL — you'll wire it into Aimfox and Outcraft workflows

---

## Step 6: Build Retool Dashboard

1. Go to [retool.com](https://retool.com) → Create account (free tier: up to 5 users)
2. New Resource → Supabase → Connect using your Project URL + service_role key
3. Create new App: "MCO Outreach Dashboard"
4. Layout — two columns:
   - **Left (30%):** Table component
     - Data source: `SELECT lead_email, full_name, company, overall_intent, last_active_channel, last_activity_at FROM leads ORDER BY last_activity_at DESC`
     - Add filter controls: intent dropdown, channel dropdown, text search on full_name/company
   - **Right (70%):** Container with:
     - Header: Text showing `{{leads_table.selectedRow.data.full_name}}` + intent badge
     - Timeline: List component
       - Data: `SELECT channel, direction, timestamp, content, sender_name, intent FROM conversations WHERE lead_email = '{{leads_table.selectedRow.data.lead_email}}' ORDER BY timestamp ASC`
       - Template per item:
         ```
         {channel_emoji} {CHANNEL} · {direction} · {timestamp}
         From: {sender_name}
         {content}
         Intent: {intent}
         ```
       - Color coding: email=blue, linkedin=purple, voice=green, sms=orange
5. Set auto-refresh to 60 seconds on the conversations list

---

## Verification Checklist

- [ ] Supabase SQL editor shows 4 tables
- [ ] n8n Supabase credential test passes
- [ ] Write Conversation Event workflow is active and has a webhook URL
- [ ] Fetch Cross-Channel Context workflow is active and has a webhook URL
- [ ] Monday.com board has 4 new columns
- [ ] Retool dashboard shows the leads table and timeline panel
- [ ] Test POST to Write Conversation Event → row appears in Supabase + Monday update posted + Retool shows the event
