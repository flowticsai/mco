# MCO Debugging & Operations Guide

> **Who this is for:** Anyone investigating why a lead did not receive a message, why a queue row is stuck, or why a workflow failed. Read this before touching anything in n8n or Supabase.

---

## 1. System Dependency Map

Every interaction in MCO follows one of three paths. Knowing which path a lead is on tells you exactly which workflow to check.

```
INBOUND (lead sends a message)
──────────────────────────────
LinkedIn DM  →  Aimfox Reply Agent  →  Write Event  →  (cancel pending queue)
Email        →  Gmail Reply Agent   →  Write Event  →  (cancel pending queue)

PROACTIVE (we send a message unprompted)
────────────────────────────────────────
Supabase follow_up_queue (pending row)
  → Dispatcher (every 15 min)
    → Coordinator
      → email path   → Gmail → Write Event
      → linkedin DM path → Aimfox → Write Event
      → linkedin campaign path → Aimfox campaign → Write Event
      → voice path  → Retell Call Agent → Post Call Analysis → Write Event

CONNECTION ACCEPTANCE
─────────────────────
Aimfox accepted webhook
  → Connection Accepted Handler
    → Send Thanks Message (Aimfox)
    → Write Event (log thanks message)
    → Insert follow_up_queue row (24h delay, linkedin channel)
```

**Critical shared services** (used by every path):
- `POST /mco-write-event` — every workflow calls this to log interactions
- `POST /mco-fetch-context` — every AI reply agent calls this before generating a response
- Supabase `upsert_lead()` RPC — resolves/creates lead identity
- Supabase `insert_conversation_event()` RPC — logs each message

---

## 2. Where to Look When Something Goes Wrong

### Step 1 — Find the n8n execution

**n8n UI:** `https://n8n-1404.n8n.whiteserverdns.com` → open the workflow → click **Executions** tab

Each execution shows:
- Status: `success` / `error` / `running`
- Which node failed (highlighted in red)
- The exact error message
- Input/output data for every node

**Via API** (for quick lookups):
```
GET https://n8n-1404.n8n.whiteserverdns.com/api/v1/executions?workflowId=<ID>&limit=10
```
Add `?includeData=true` to see node-level output.

### Step 2 — Check Supabase for the lead

Open Supabase SQL editor (`https://supabase.com` → project `hkqssbomrcbtfbdowtgj`) and run:

```sql
-- Find the lead
SELECT lead_id, lead_email, linkedin_urn, linkedin_profile_url, phone_e164,
       overall_intent, full_name, company, linkedin_conversation_urn,
       last_active_channel, last_activity_at
FROM leads
WHERE lead_email = 'their@email.com'
   OR linkedin_profile_url LIKE '%their-linkedin-slug%';

-- See all their conversations
SELECT channel, direction, timestamp, content, intent, sender_name
FROM conversations
WHERE lead_id = '<lead_id from above>'
ORDER BY timestamp DESC
LIMIT 20;

-- See their queue rows
SELECT queue_id, target_channel, status, scheduled_for, created_at, follow_up_context
FROM follow_up_queue
WHERE lead_id = '<lead_id>'
ORDER BY created_at DESC;
```

### Step 3 — Match the timeline

Compare the `conversations` timestamps with n8n execution timestamps. If a conversation row is missing for an event you expected, the workflow either never ran or failed silently (`neverError: true`).

---

## 3. Failure Signatures — What They Mean

| Symptom | Where to look | Likely cause |
|---|---|---|
| Lead never got a reply | Aimfox Reply Agent / Gmail Reply Agent executions | Agent didn't trigger, or `intent` check blocked it |
| Queue row stuck as `pending` | follow_up_queue + Dispatcher executions | Dispatcher not running (check schedule), or Coordinator threw |
| Queue row shows `failed` | Coordinator execution | Coordinator payload missing required field; check Coordinator error node |
| Thanks message not sent after connection | Connection Accepted Handler executions | `neverError:true` on Send Thanks Message swallowed Aimfox error; check the HTTP response in that node |
| Duplicate message sent to lead | conversations table — look for 2 rows same content/timestamp | Aimfox webhook retry + missing dedup; `event_id` must be stable |
| Lead's intent demoted (e.g. booking → not_interested) | leads table + RPC | Intent was passed directly in Code node instead of through `upsert_lead()` — should never happen |
| Context block is empty / missing prior channel | Fetch Context execution | Lead was resolved by wrong identifier; `lead_id` may be null |
| Voice call fired but no transcript logged | Post Call Analysis executions | Retell webhook URL misconfigured, or call_analyzed event not firing |
| `lead_id is null` in queue rows | Write Event → Merge Lead Data node | `upsert_lead()` RPC returned an unexpected shape — check Supabase RPC logs |

---

## 4. Per-Workflow: Inputs Required, Where It Fails

### Write Conversation Event (`5qOo5YzrPnW8Uj9g`)
**Required:** `event_id` (UUID), `channel`, `direction`, `content`, `timestamp`, at least one of: `lead_email`, `linkedin_profile_url`, `linkedin_urn`, `phone_e164`

**Common failure points:**
- `event_id` is not a valid UUID → Supabase rejects the insert silently (neverError)
- No lead identifier → `upsert_lead()` throws, workflow returns 400
- `Merge Lead Data` node: `upsert_lead()` returns plain object; if code uses `Array.isArray()` check, `lead_id` is always null → `follow_up_queue` inserts fail silently

**How to verify a write succeeded:**
```sql
SELECT * FROM conversations WHERE event_id = '<your_event_id>';
```

---

### Fetch Cross-Channel Context (`UJoDCfkmD3NJHktk`)
**Required:** at least one lead identifier

**Common failure points:**
- Lead not found → returns `{ lead: null, event_count: 0 }` (not an error)
- `linkedin_conversation_urn` missing from lead record → Coordinator takes campaign path instead of DM path

**How to verify:**
```sql
SELECT linkedin_conversation_urn FROM leads WHERE lead_email = '...';
```

---

### FollowUp Queue Dispatcher (`3ju6z4oJcWJqskBN`)
**Runs:** every 15 minutes on schedule

**What it does:** fetches `pending` rows from `follow_up_queue` where `scheduled_for <= NOW()`, calls Coordinator for each.

**Common failure points:**
- No pending rows (check `status` column — may be `sent`, `failed`, or `skipped`)
- `follow_up_context` JSON is malformed → `Split Rows` Code node throws
- Coordinator webhook unreachable → queue row stays pending indefinitely

**Check queue health:**
```sql
-- Overdue rows (should be 0)
SELECT COUNT(*) as overdue
FROM follow_up_queue
WHERE status = 'pending' AND scheduled_for < NOW() - INTERVAL '30 minutes';

-- Status breakdown
SELECT status, COUNT(*) FROM follow_up_queue GROUP BY status;
```

---

### Centralized Follow-Up Coordinator (`KXKcCYRnK4V8v9k7`)
**Required:** `queue_id`, `lead_id` or `lead_email`, `target_channel`

**Three paths:**
1. `email` → Anthropic generates reply → Gmail sends → Write Event logs
2. `linkedin` with `conversation_urn` → Aimfox DM in existing thread
3. `linkedin` without `conversation_urn` → Aimfox campaign (adds to campaign `6e2feb86-...`)
4. `voice` → Retell Call Agent webhook

**Common failure points:**
- `conversation_urn` is null on lead record → takes campaign path even for existing connections
- Aimfox token wrong → 401 on DM send, silently swallowed
- Campaign in DONE state → Aimfox returns 400 on audience add
- Anthropic API error → email body is empty or workflow fails

---

### Connection Accepted Handler (`WTbIAJCZGtppAT91`)
**Entry:** Aimfox `accepted` webhook

**Payload structure (real Aimfox format):**
```json
{
  "id": "<stable-UUID-for-dedup>",
  "event_type": "accepted",
  "event": {
    "account": { "id": 774180197 },
    "target": { "id": <aimfox_lead_id>, "urn": "<linkedin_urn>", "first_name": "...", "public_identifier": "..." }
  }
}
```

**Common failure points:**
- Aimfox token expired → `Send Thanks Message` returns 401 (neverError swallows it)
- `Get Lead Custom Variables` returns no `LEAD_EMAIL` → `lead_email` is null (OK — handler works for LinkedIn-only leads)
- `conversation_urn` null after Send Thanks (Aimfox 500/400) → lead's `linkedin_conversation_urn` stays null → Coordinator will use campaign path instead of DM path

**Check if thanks was sent:**
```sql
SELECT content, timestamp, metadata
FROM conversations
WHERE lead_id = '<lead_id>' AND direction = 'outbound' AND channel = 'linkedin'
ORDER BY timestamp DESC LIMIT 5;
```

---

### Aimfox Reply Agent (`SPN1NLyHH1LcfViD`)
**Entry:** Aimfox `new_reply` or `reply` webhook

**Gate:** Google Sheets conversation state — only replies if the sheet says this conversation is open. If the sheet row is missing or closed, the agent skips silently.

**Common failure points:**
- Google Sheets credential expired → agent skips all replies
- Lead not found in Supabase → context block is empty, AI reply has no context
- Anthropic API error → no reply sent

---

### Gmail Reply Agent (`mFBOGdMAsXRKD1Pv`)
**Entry:** Gmail trigger (new email to `team@flowticsai.com`)

**Common failure points:**
- Gmail OAuth token expired → trigger stops firing (check n8n credential status)
- Instantly AI campaign Reply-To not set to Gmail address → replies never arrive
- Thread not found in Gmail → context fetch fails

---

### Call Agent (`xE8mFF8HxPaSXNmi`)
**Runs:** every 4h + on-demand webhook

**Common failure points:**
- No `voice` rows in `follow_up_queue` → nothing to call (expected if no leads are at voice stage)
- `phone_e164` missing from lead → call cannot be placed
- Retell API key invalid → call fails silently

**Check voice queue:**
```sql
SELECT q.queue_id, l.phone_e164, l.full_name, q.scheduled_for, q.status
FROM follow_up_queue q JOIN leads l ON q.lead_id = l.lead_id
WHERE q.target_channel = 'voice' AND q.status = 'pending';
```

---

### Post Call Analysis (`r8XKHCnL4vju2E4j`)
**Entry:** Retell `call_analyzed` webhook

**What it does:** logs call transcript + intent to Supabase; if call not answered, re-queues voice follow-up 4h later.

**Common failure points:**
- `queue_id` missing from Retell metadata → cannot mark queue row as sent
- `disconnection_reason` not in the unanswered set → answered call that wasn't logged (check Retell webhook payload)

---

## 5. Supabase Health Check Queries

Run these to get a full picture of system state:

```sql
-- Overall queue health
SELECT status, COUNT(*) as count FROM follow_up_queue GROUP BY status ORDER BY count DESC;

-- Leads by intent
SELECT overall_intent, COUNT(*) FROM leads GROUP BY overall_intent ORDER BY COUNT(*) DESC;

-- Most recent activity (last 24h)
SELECT l.full_name, l.lead_email, c.channel, c.direction, c.timestamp, c.intent
FROM conversations c JOIN leads l ON c.lead_id = l.lead_id
WHERE c.timestamp > NOW() - INTERVAL '24 hours'
ORDER BY c.timestamp DESC LIMIT 20;

-- Leads missing linkedin_conversation_urn (Coordinator will use campaign path)
SELECT lead_email, full_name, overall_intent
FROM leads
WHERE linkedin_urn IS NOT NULL AND linkedin_conversation_urn IS NULL;

-- Overdue queue rows (stuck)
SELECT q.queue_id, l.lead_email, q.target_channel, q.scheduled_for, q.status
FROM follow_up_queue q JOIN leads l ON q.lead_id = l.lead_id
WHERE q.status = 'pending' AND q.scheduled_for < NOW() - INTERVAL '30 minutes'
ORDER BY q.scheduled_for ASC;
```

---

## 6. n8n Workflow IDs (Quick Reference)

| Workflow | ID | Entry point |
|---|---|---|
| Write Event | `5qOo5YzrPnW8Uj9g` | `POST /webhook/mco-write-event` |
| Fetch Context | `UJoDCfkmD3NJHktk` | `POST /webhook/mco-fetch-context` |
| Dispatcher | `3ju6z4oJcWJqskBN` | Schedule (every 15 min) |
| Coordinator | `KXKcCYRnK4V8v9k7` | `POST /webhook/mco-followup` |
| Connection Accepted Handler | `WTbIAJCZGtppAT91` | `POST /webhook/mco-aimfox-accepted` |
| Aimfox Reply Agent | `SPN1NLyHH1LcfViD` | Aimfox `new_reply` webhook |
| Gmail Reply Agent | `mFBOGdMAsXRKD1Pv` | Gmail trigger |
| Call Agent | `xE8mFF8HxPaSXNmi` | `POST /webhook/3adf4681-...` |
| Post Call Analysis | `r8XKHCnL4vju2E4j` | `POST /webhook/9cdd28e8-...` |
| Aimfox Responded | `Zw7iTErdMMJjiM7g` | Aimfox `responded` webhook |

---

## 7. neverError Nodes — Silent Failure List

These nodes have `neverError: true`. They will show `success` in n8n even when the underlying API call failed. Always inspect their **output JSON** in the execution, not just the status colour.

| Workflow | Node | What it calls | What a silent failure looks like |
|---|---|---|---|
| Connection Accepted Handler | Send Thanks Message | Aimfox POST /conversations | Output: `{ "status": "fail", "error": { "code": 401 } }` |
| Connection Accepted Handler | Get Lead Custom Variables | Aimfox GET /leads/:urn/custom-variables | Output: `{ "status": "fail" }` |
| Coordinator | Reply to Existing Conversation | Aimfox POST /conversations/:urn | Output: `{ "error": "..." }` |
| Coordinator | Add to LinkedIn Campaign | Aimfox POST /campaigns/:id/audience | Output: `{ "error": "..." }` |
| Write Event | Insert Conversation | Supabase RPC | Output: empty array `[]` if dedup blocked |

**Rule:** after any `neverError` node, the next node should check the output. If it doesn't, errors propagate silently.

---

## 8. How to Re-trigger a Failed Event

If a workflow failed and you need to replay it:

**Option A — Re-send the webhook** (safest, dedup prevents duplicates):
```bash
curl -X POST https://n8n-1404.n8n.whiteserverdns.com/webhook/mco-write-event \
  -H "Content-Type: application/json" \
  -d '{ ...original payload... }'
```
The `event_id` dedup means safe to replay — if the row already exists, returns `{ "status": "already_written" }`.

**Option B — Re-run from n8n UI:**
Open the execution → click **Retry** (top right). Note: this re-runs the entire workflow with the same input, including any API calls.

**Option C — Fix the queue row and let Dispatcher pick it up:**
```sql
-- Push scheduled_for to now so Dispatcher picks it up on next run
UPDATE follow_up_queue
SET scheduled_for = NOW(), status = 'pending'
WHERE queue_id = '<queue_id>';
```

---

## 9. Known Constraints and Edge Cases

- **Aimfox `neverError` nodes:** silent failures are the #1 debugging challenge. Always check the output JSON, not the node colour.
- **`conversation_urn` null:** if the thanks message send fails (Aimfox 500), `conversation_urn` stays null on the lead. Coordinator will add them to a campaign instead of DMing — this is degraded but functional.
- **LinkedIn-only leads:** `lead_email` can be null throughout the system. All workflows now handle this; never throw on missing email.
- **Aimfox campaign state:** campaign `6e2feb86-b9c6-4c18-87fa-c5fe5e41682f` accepts audience adds in ACTIVE, PAUSED, DONE, and CREATED states. Unsupported types are GROUP MESSAGE and EVENT MESSAGE only.
- **Instantly AI:** no separate workflow. Set Reply-To = `team@flowticsai.com` in each campaign. Gmail Reply Agent handles all inbound from Instantly.
- **Dispatcher fires every 15 min:** a pending queue row will be picked up within 15 minutes of `scheduled_for`. If it's been >30 min and the row is still pending, the Dispatcher or Coordinator has a problem.
