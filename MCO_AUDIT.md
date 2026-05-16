# MCO System Audit
**Date:** 2026-05-12  
**Auditor:** Claude (automated audit via n8n API + Supabase REST)

> **2026-05-16 addendum:** Several things have changed since this audit:
>
> 1. **Notion CRM live.** A new `MCO CRM` Notion workspace was created and `MCO - Write Conversation Event` now dual-writes to Notion (6 new nodes between the Monday branch and the `Cross-Channel?` gate). Every channel agent that funnels through `/mco-write-event` reaches Notion automatically. Full reference: [docs/Notion_CRM.md](docs/Notion_CRM.md).
> 2. **Workflow activation drifted from this audit.** As of 2026-05-16: Write Event = ON, Fetch Context = ON, Flowtics outbound = ON; all others (Coordinator, Dispatcher, Aimfox Reply Agent, Gmail Reply Agent, Connection Accepted) currently **OFF**. Re-activate when ready to resume the loop.
> 3. **`Flowtics AI Outbound agent-MCO` is built** (this audit said the Retell handler was pending). It exists, is active, runs the voice channel via Retell. Not yet documented in workflows/.
> 4. **Two pre-existing bugs were found** while building the Notion dual-write but **deliberately not fixed** because they only affect Monday and Monday is going away:
>    - `Merge Lead Data` drops the canonical `overall_intent` from the Supabase RPC (array-vs-object mismatch). Effect: the webhook response and Monday updates report event-level intent instead of canonical lead intent.
>    - `Create Monday Item` creates a new Monday item per event for the same lead instead of deduping by email.
> 5. **Monday removal is a single-script operation.** `python tools/kill_monday.py --apply` removes the 5 Monday nodes from Write Event and the 3 standalone `mondayCom` nodes from Aimfox Reply Agent, with auto-backup.

---

## What MCO Is

MCO (Multi-Channel Outreach) is an automation layer that unifies LinkedIn, email, voice, and SMS interactions for every lead into a single shared memory store (Supabase). Any AI agent in the system — whether it's replying on LinkedIn or sending a follow-up email — reads from and writes to the same store, so it always knows what happened on every other channel.

The system has three layers:
1. **Write path** — every interaction gets recorded in Supabase (conversations table)
2. **Read path** — AI agents fetch cross-channel context before replying
3. **Dispatch path** — when a lead shows interest, a follow-up gets queued and sent on the right channel automatically

---

## Infrastructure

### Supabase Database
**Project:** `hkqssbomrcbtfbdowtgj.supabase.co`  
**Current data:** 3 leads · 5 conversations · 2 follow-up queue items (both pending, test data)

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `leads` | One row per lead. Single source of truth for identity + intent | lead_email (PK), full_name, company, linkedin_urn, linkedin_profile_url, phone_e164, first_channel, last_active_channel, overall_intent, monday_item_id, aimfox_campaign, email_sender_inbox |
| `conversations` | Every inbound/outbound interaction across all channels | event_id (PK, dedup key), lead_email, timestamp, channel, direction, content, content_type, sender_name, intent |
| `follow_up_queue` | Queued cross-channel follow-ups waiting to be sent | queue_id (PK), lead_email, trigger_channel, target_channel, scheduled_for, status (pending/sent), follow_up_context |
| `phone_map` | Maps phone numbers to lead emails (for Retell/SMS) | phone_e164 (PK), lead_email, source, confidence |

**Intent promotion logic** is handled by a Postgres RPC function `upsert_lead()` — intent never demotes atomically. Order: unknown → no_action → not_interested → referral → interested → booking.

---

## n8n Workflows Built

### 1. MCO - Write Conversation Event
**ID:** GUUEUvfjFwLojoA0 | **Status:** ACTIVE ✓  
**Webhook:** `POST /mco-write-event`  
**Nodes:** 19  

**What it does:** The single write path for the entire system. Every channel calls this when an interaction happens.

**Flow:**
```
Webhook → Setup & Validate → Upsert Lead → Merge Lead Data → Insert Conversation
  → Check Insert Result → Was Duplicate?
      [duplicate] → Return Already Written
      [new] → Has Phone? → Upsert PhoneMap (if phone present)
            → Format Monday Update → Create Monday Item → Extract Monday ID
            → Has Monday Item? → Post Monday Update
            → Cross-Channel? → Build Queue Rows → Insert FollowUpQueue (if trigger_cross_channel=true)
            → Return OK
```

**Key behaviours:**
- Deduplication on `event_id` — safe to retry, never double-writes
- Upserts lead record on every call (creates if new, updates if returning)
- Posts update to Monday.com board 18399476470 for every interaction
- If `trigger_cross_channel: true` + `target_channels: ["email"]` → inserts into follow_up_queue, which triggers email follow-up within 15 minutes

**Called by:** Aimfox Reply Agent (Write Returning, Write First Interested), Gmail Reply Agent (Write Inbound, Write Outbound), Connection Accepted Handler, Centralized Coordinator

---

### 2. MCO - Fetch Cross-Channel Context
**ID:** JXpvRl8WTAqigVfi | **Status:** ACTIVE ✓  
**Webhook:** `POST /mco-fetch-context`  
**Nodes:** 5  

**What it does:** Returns a formatted text block of everything that's happened with a lead across all channels. AI agents call this before generating any reply.

**Flow:**
```
Webhook → Setup & Validate → Fetch Context RPC → Format Context Block → Return Context
```

**Returns:**
```json
{
  "context_block": "=== Cross-Channel Conversation History ===\nLead: John Smith...",
  "lead": { "full_name": "...", "overall_intent": "interested", ... },
  "event_count": 7,
  "overall_intent": "interested"
}
```

**Called by:** Gmail Reply Agent, Connection Accepted Handler, Centralized Coordinator

---

### 3. MCO - FollowUp Queue Dispatcher
**ID:** BQJkE0sa0yRKWDjM | **Status:** ACTIVE ✓  
**Trigger:** Every 15 minutes (cron)  
**Nodes:** 9  

**What it does:** Polls follow_up_queue for pending items, enriches them with LinkedIn metadata if needed, then calls the Coordinator to send the actual message.

**Flow:**
```
Every 15 Minutes → Config → Fetch Pending Queue → Split Rows → LinkedIn?
  [LinkedIn] → Fetch LinkedIn Meta → Merge LinkedIn Meta → Call Coordinator → Log Result
  [Email/other] → Call Coordinator → Log Result
```

**Note:** The 2 pending rows currently in follow_up_queue are test data from May 4. If they haven't been processed yet, they're stale. Worth clearing them manually in Supabase.

---

### 4. MCO - Centralized Follow-Up Coordinator
**ID:** R9bkR97Xt5fHSN4K | **Status:** ACTIVE ✓  
**Webhook:** `POST` (called by Dispatcher)  
**Nodes:** 19  

**What it does:** Receives a queued follow-up item, fetches context, generates a message using Claude, then sends it on the right channel (Gmail or LinkedIn campaign).

**Flow:**
```
Webhook → Setup & Validate → Fetch Context → Merge Context → Route by Channel
  [Email]    → Email: Generate Message (Claude) → Email: Extract Message → Send Email (Gmail) → After Send
  [LinkedIn] → LinkedIn: Generate Message (Claude) → LinkedIn: Extract Message
               → LinkedIn: Has Profile URL? 
                   [yes] → Add to LinkedIn Campaign → After Send
                   [no]  → LinkedIn: Skip (No URL) → After Send
After Send → Log to Supabase → Mark Queue Sent → Return OK
```

**Key behaviours:**
- Generates personalised messages using cross-channel context
- For email: sends via Gmail using `md.imranhanik@gmail.com`
- For LinkedIn: adds to an Aimfox campaign (requires profile URL)
- Marks queue item as `sent` after successful dispatch
- Logs the outbound message back to Supabase via Write Conversation Event

---

### 5. MCO - Aimfox Connection Accepted Handler
**ID:** 8MxgrCTDN6IZ98iF | **Status:** ACTIVE ✓  
**Webhook:** Aimfox connection accepted event  
**Nodes:** 15  

**What it does:** Fires when a LinkedIn connection is accepted. Reads the lead's email from Aimfox custom variables, fetches MCO context, generates a personalised opening message, sends it via LinkedIn, and logs to Supabase.

**Flow:**
```
Webhook → Extract Fields → Get Lead Custom Variables → Extract LEAD_EMAIL → Has Email?
  [no email] → Skip → Return OK (Skipped)
  [has email] → Fetch Context → Merge Context → Generate Message (Claude)
              → Extract Message → Send via Start Conversation → Log to Supabase → Return OK
```

**Key behaviours:**
- Only proceeds if lead has a LEAD_EMAIL custom variable in Aimfox
- Uses MCO context to personalise the opening message (avoids repeating what was said on email)
- Logs outbound LinkedIn message to Supabase

---

### 6. Aimfox Nextus AI Reply Agent — MCO
**ID:** mAGUFlmJZ0gwge3s | **Status:** ACTIVE ✓  
**Trigger:** Aimfox webhook (new LinkedIn reply received)  
**Nodes:** 59  

**What it does:** The main LinkedIn reply agent. Receives every LinkedIn reply, classifies intent, generates AI response, sends it, and writes to Supabase in two different ways depending on whether the lead is new or returning.

**MCO integration points (3 nodes added to existing agent):**

```
Thread → MCO: Build Seed B1 (always runs — formats full historical thread for potential seed write)

AI Agent1 → Send Reply → MCO: Write Returning
  Logic: Check Supabase leads table
    Lead EXISTS → write inbound (client's reply) + outbound (our reply) → done
    Lead NOT in Supabase → do nothing (not our tracked lead yet)

Mark Interested → MCO: Write First Interested
  Logic: Check Supabase leads table
    Lead NOT in Supabase → write full historical thread (from Build Seed B1) 
                         + current inbound (intent=interested, trigger_cross_channel=true, target_channels=['email'])
                         + our outbound reply
                         → this triggers email follow-up within 15 minutes
    Lead EXISTS → do nothing (returning lead, already written by Write Returning)
```

**Intent classification → downstream actions:**
- `Mark Interested` → Write First Interested → email follow-up queued
- `Not Interested` → no MCO write
- `Referral` → Slack notification + AI Agent5 (referral handling)
- `Booking` → Slack notification
- `Already Have Contract` → handled separately

**Removed nodes (cleaned up this session):**
- ~~MCO: Write Seed B1~~ — was writing seed for ALL first-time leads regardless of interest
- ~~MCO: Write Outbound B1~~ — orphaned, no longer needed
- ~~MCO: Write Interested~~ — replaced by Write First Interested
- ~~MCO: Check Seeded B1~~ — replaced by direct Supabase check inside the Code nodes
- ~~MCO: IF Seeded? B1~~ — same, removed

---

### 7. MCO - Gmail Reply Agent
**ID:** 7pTHnRue85rqTvXk | **Status:** INACTIVE ✗ (needs activation)  
**Trigger:** Gmail — new email received  
**Nodes:** 18  

**What it does:** Replies to emails that are replies to follow-ups the Coordinator sent. Checks Supabase to confirm the email is part of a thread we started, fetches context, classifies intent, generates a reply using Claude, sends for Slack approval, then sends the Gmail reply and logs it.

**Flow:**
```
Gmail Trigger → Extract & Filter → Supabase: Check Outbound → Filter: Our Thread Only
  [not our thread] → stops silently (no output)
  [our thread confirmed] → MCO: Fetch Context → Merge Context → MCO: Write Inbound
                         → Text Classifier
                             [needs reply] → Reply Agent (Claude + Knowledgebase)
                                           → Edit Fields → Slack sendAndWait (approval)
                                           → Switch
                                               [Approve] → Send Gmail Reply → MCO: Write Outbound
                                               [Disapprove] → stops silently
                             [no reply needed] → No Operation
```

**Key behaviours:**
- Only fires for emails that are replies to threads we started (verified via Supabase outbound check)
- Uses `threadId` for proper Gmail threading (replies appear in same thread)
- Requires human approval in Slack before sending
- Writes both inbound and outbound to Supabase

**Blocker:** Gmail credential needs to be linked in n8n and the workflow activated.

---

## Workflow Interconnection Map

```
LinkedIn Reply (Aimfox webhook)
    │
    └─► Aimfox Nextus AI Reply Agent — MCO
            │
            ├─ [every reply] MCO: Write Returning ──────────────────► Write Conversation Event
            │                                                               │
            └─ [interested]  MCO: Write First Interested ──────────────────┤
                              (trigger_cross_channel=true)                  │
                                                                            ▼
                                                                     Supabase (leads + conversations + follow_up_queue)
                                                                            │
                                                                     FollowUp Queue Dispatcher (every 15 min)
                                                                            │
                                                                     Centralized Follow-Up Coordinator
                                                                            │
                                                                     Gmail (email follow-up sent)
                                                                            │
Lead replies to email ◄──────────────────────────────────────────────────────
    │
    └─► Gmail Reply Agent (INACTIVE)
            │
            ├─ Fetch Cross-Channel Context ◄────────────────────────── Supabase
            │
            └─ Write Conversation Event ─────────────────────────────► Supabase

LinkedIn Connection Accepted (Aimfox webhook)
    │
    └─► Connection Accepted Handler
            │
            ├─ Fetch Cross-Channel Context
            └─ Write Conversation Event ─────────────────────────────► Supabase
```

---

## What's Working End-to-End

| Scenario | Status |
|----------|--------|
| Lead replies on LinkedIn → AI replies → logged to Supabase | ✓ LIVE |
| Lead marked Interested on LinkedIn → email follow-up queued | ✓ LIVE |
| Dispatcher picks up queue every 15 min → Coordinator sends email | ✓ LIVE |
| Connection accepted → personalised opening message sent | ✓ LIVE |
| Lead replies to our email → AI replies → logged to Supabase | ✗ NOT ACTIVE (Gmail agent inactive) |
| Voice/SMS call ends → logged to Supabase | ✗ NOT BUILT (Retell handler pending) |

---

## What's Not Built Yet

### 1. Gmail Reply Agent Activation
**What's needed:** Open workflow `7pTHnRue85rqTvXk` in n8n, link a Gmail credential to the Gmail Trigger node and Send Gmail Reply node, then activate.  
**Impact:** Without this, email replies from leads go unread and unanswered by the system. The email follow-up chain is one-directional right now (we send, they reply, nothing happens).

### 2. Retell Webhook Handler
**What's needed:** Retell AI JSON workflow files from user (to get webhook URLs, signing secrets, custom analysis fields). Workflow doc exists at `workflows/retell_webhook_handler.md`.  
**What it would do:** Receive post-call webhooks from Retell, resolve phone → email via phone_map, write call transcripts/summaries to Supabase, trigger cross-channel follow-ups if call intent = interested/booking.  
**Impact:** Voice calls are completely outside the MCO loop right now. No call data lands in Supabase.

### 3. Retool Dashboard
**What's needed:** Retool account + Supabase connection.  
**What it would show:** Left panel = leads table (filterable by intent/channel). Right panel = full cross-channel conversation timeline for selected lead. Auto-refreshes every 60 seconds.  
**Impact:** Currently no UI to see what the system is doing. You'd have to query Supabase directly to check on leads.

---

## Known Issues / Things to Watch

| Issue | Detail |
|-------|--------|
| Stale test data in follow_up_queue | 2 rows with status=pending from May 4 test runs. Dispatcher will keep trying to process them. Clear them in Supabase SQL editor: `DELETE FROM follow_up_queue WHERE lead_email IN ('mco-test-lead@example.com')` |
| phone_map table empty | No voice/SMS activity yet. Will populate once Retell handler is built. |
| Gmail Reply Agent has no Disapprove path logging | If Slack approval is denied, it stops silently. No record that a reply was rejected. Not critical but worth noting. |
| Build Seed B1 runs for all leads | Now runs for every LinkedIn reply (returning leads included), but output is discarded for returning leads. Negligible performance cost. |
| Supabase only has test data | 3 leads and 5 conversations, all from manual testing on May 4. No real production data yet. |

---

## Files on Disk

```
workflows/
  write_conversation_event.md        ✓ Complete SOP
  fetch_cross_channel_context.md     ✓ Complete SOP
  unified_outreach_store_setup.md    ✓ Setup guide (infrastructure already set up)
  retell_webhook_handler.md          ✗ Pending — workflow not built yet

tools/
  setup_infrastructure.py            ✓ One-time setup script
  test_workflows.py                  ✓ E2E test suite for Write + Fetch workflows

.tmp/
  Various Python scripts used to build/patch n8n workflows via API
  (disposable — all changes are live in n8n)
```

---

## Next Steps (Priority Order)

1. **Activate Gmail Reply Agent** — link Gmail credential in n8n, activate workflow `7pTHnRue85rqTvXk`. 30 minutes of work, closes the email reply loop entirely.

2. **Clear stale test queue rows** — `DELETE FROM follow_up_queue WHERE status='pending' AND created_at < '2026-05-05'` in Supabase SQL editor.

3. **Retell Webhook Handler** — share Retell JSON files so the voice/SMS channel can be built. Workflow doc is ready, just needs the Retell-specific payload structure to build the n8n workflow.

4. **Retool Dashboard** — visual interface to monitor leads and conversations. Supabase is ready, just needs a Retool account and the two queries wired up.
