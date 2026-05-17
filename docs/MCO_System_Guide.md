# MCO System — Plain English Guide
### What it is, how it works, and why we built it this way
**Last updated:** 2026-05-17 (synced to live system)

---

## The Problem We're Solving

Without MCO, every channel works in isolation. A lead might accept a LinkedIn connection request, reply to a follow-up email three days later, then receive an AI voice call that re-introduces the product they already said they're interested in. Each agent starts from scratch every time.

MCO is the connective tissue. It gives every channel a shared memory, so every agent — whether it's generating a LinkedIn DM, an email reply, or briefing a voice agent before a call — always knows the full cross-channel history of that lead before it responds.

---

## The Core Rule

**The system should always be the last to send.**

- If the lead replies to something → the relevant Reply Agent handles it (reactive path)
- If the lead hasn't replied and time has passed → the Coordinator fires a follow-up (proactive path)

These two paths never overlap. When a lead responds, Write Conversation Event automatically cancels any pending follow-ups so the Coordinator doesn't send a duplicate.

---

## The Three Paths

```
WRITE PATH      Every interaction → POST /mco-write-event → Supabase + Notion
READ PATH       Before any AI reply → POST /mco-fetch-context → full cross-channel context
DISPATCH PATH   Lead goes quiet → Dispatcher (every 15 min) → Coordinator → send
```

---

## The Workflows

### Core Infrastructure (always running)

**Write Conversation Event**  
The single write path for the entire system. Every message sent or received — on any channel — is recorded here. It updates Supabase (lead record + conversation history), updates Notion CRM, and optionally queues a cross-channel follow-up.

**Fetch Cross-Channel Context**  
Called before any AI generates a reply. Returns a formatted summary of everything that's ever happened with a lead — every email, LinkedIn message, voice call — so the AI can reply like someone who's been paying attention.

**FollowUp Queue Dispatcher**  
Checks Supabase every 15 minutes for follow-ups that are due. When it finds one, it hands it to the Coordinator. It also fetches Aimfox account metadata for LinkedIn follow-ups so the Coordinator has what it needs.

**Centralized Follow-Up Coordinator**  
The proactive outreach engine. When the Dispatcher triggers it, it fetches full context, generates an AI message, and routes to the right channel:
- **Email** → Claude generates message → Gmail sends from `team@flowticsai.com`
- **LinkedIn** → checks if a conversation thread exists (has `conversation_urn`):
  - Yes (connected) → Claude generates message → Aimfox replies inside the existing thread
  - No (not connected) → Adds lead to Aimfox campaign (sends connection request) → marks queue skipped → stops
- **Voice** → triggers Call Agent webhook → Call Agent handles Retell call

---

### LinkedIn Pipeline

**Aimfox Connection Accepted Handler**  
When a LinkedIn connection is accepted, Aimfox fires this webhook. It:
1. Sends a fixed "great to connect" message immediately (no AI — warm and personal by design)
2. Logs the message + stores `conversation_urn` on the lead record (critical — this is how the Coordinator knows it can DM this lead)
3. Queues a 24-hour follow-up so the Coordinator can continue the conversation the next day

**Aimfox Nextus AI Reply Agent — MCO**  
Handles inbound LinkedIn replies. Uses Google Sheets to decide whether to reply (are we the last to send?). Uses Claude/Gemini to generate responses. When a lead shows genuine interest, writes events to MCO/Supabase. This is the reactive path — it only fires when the lead sends a message.

**MCO - Aimfox Responded**  
When Aimfox detects that a lead has responded to a campaign message, this marks an Aimfox label (internal Aimfox state tracking). By design, it does not write to Supabase — the Reply Agent handles the actual response and any Supabase writes.

---

### Email Pipeline

**MCO - Gmail Reply Agent**  
When an email arrives at `team@flowticsai.com`, this workflow fires. It:
1. Fetches full cross-channel context (knows about LinkedIn messages, prior calls, everything)
2. Logs the inbound email to Supabase
3. Classifies intent
4. Generates an AI reply using all available context
5. Sends the reply from `team@flowticsai.com`
6. Logs the outbound reply to Supabase

---

### Voice Pipeline

**Flowtics AI Call Agent — MCO**  
Runs every 4 hours on schedule AND accepts on-demand webhook triggers. For each pending voice follow-up:
1. Fetches the lead record and full conversation context
2. Uses GPT-4o-mini to summarise the context into a ≤180-word brief for the voice agent
3. Fires a Retell outbound call from `+15722124790` (agent: Maya — Flowtics AI)
4. Passes `queue_id` in the call metadata so Post Call Analysis can track it

**Post Call Analysis — MCO**  
Retell fires this after every call. It:
1. Logs the call transcript/summary to Supabase via Write Event
2. Checks if the call was answered (`disconnection_reason`)
3. If not answered (voicemail, no answer, busy) → automatically re-queues a new voice follow-up 4 hours later

---

### Data & Utilities

**Aimfox Data Fetching MCO**  
Fetches lead data from Aimfox and writes it to Google Sheets. Separate from the outreach pipeline — used for reporting and enrichment.

---

## The Data Store (Supabase)

**leads** — One row per lead. The canonical identity record. A lead is identified by any of: UUID `lead_id`, `lead_email`, `linkedin_profile_url`, `linkedin_urn`, or `phone_e164`. Stores `linkedin_conversation_urn` — the Coordinator reads this to know whether it can DM or must send a connection request first.

**conversations** — One row per message, any channel. This is what Fetch Context reads to build the history summary. Deduplication on `event_id` means it's safe to call Write Event multiple times for the same message.

**follow_up_queue** — The waiting list. Each row has a `target_channel`, `scheduled_for`, and `status` (pending / sent / skipped / failed). The Dispatcher reads this every 15 minutes.

**phone_map** — Maps phone numbers to leads. Used when Retell fires a call and we need to find the lead by their phone number.

---

## The Two Lead Journeys

### Journey A: Lead comes from LinkedIn (Aimfox)

```
Lead accepts connection request
  -> Connection Accepted Handler
      -> Send fixed thanks message immediately
      -> Store conversation_urn on lead record
      -> Queue 24h follow-up

24 hours later -> Dispatcher fires -> Coordinator
  -> Has conversation_urn? YES
  -> Claude generates LinkedIn DM
  -> Aimfox replies inside existing thread
  -> Logged to Supabase

Lead replies to that DM
  -> Aimfox Reply Agent fires (reactive)
  -> Google Sheets: should we reply? Yes (lead replied)
  -> Claude/Gemini generates response
  -> Aimfox sends reply
  -> [if interested] Written to Supabase
  -> Write Event cancels any pending follow-ups
```

### Journey B: Lead comes from Email

```
Lead replies to cold email
  -> Gmail Reply Agent fires
  -> Fetch Context (cross-channel history)
  -> Log inbound to Supabase
  -> Claude generates reply
  -> Gmail sends from team@flowticsai.com
  -> Log outbound to Supabase

If lead shows interest -> trigger_cross_channel=true
  -> follow_up_queue row inserted (target=linkedin)
  -> Dispatcher fires -> Coordinator

Lead has no conversation_urn yet (not connected on LinkedIn)
  -> Coordinator: Add to Aimfox campaign (sends connection request)
  -> Logs connection request to Supabase
  -> Queue marked skipped

Lead accepts connection request
  -> Connection Accepted Handler fires
  -> (same as Journey A from here)
```

### Journey C: Voice Follow-Up

```
Lead is in follow_up_queue with target_channel=voice
  -> Dispatcher fires -> Coordinator
  -> Coordinator triggers Call Agent webhook
  -> Call Agent: fetch context, summarize, fire Retell call
  -> Queue marked sent

Call not answered
  -> Retell fires Post Call Analysis
  -> Logs to Supabase (no answer)
  -> Re-queues new voice follow-up in 4h
  -> Retries until answered
```

---

## What Notion Tracks

Every time Write Event fires, it also updates Notion CRM:
- Finds the lead's Notion page by email
- Updates intent, last active channel, name, company if changed
- Creates a new Conversation entry in the lead's page with message content + channel

This keeps the pipeline visible in Notion without any manual updates.

---

## What's Live

| Capability | Status |
|---|---|
| LinkedIn connection accepted → thanks message → 24h follow-up | Live |
| Lead replies on LinkedIn → AI responds (reactive) | Live |
| Email reply received → AI responds with full cross-channel context | Live |
| Scheduled email follow-up → Claude → Gmail | Live |
| Scheduled LinkedIn DM follow-up → Claude → Aimfox | Live |
| Scheduled voice follow-up → Retell AI → Maya | Live |
| Unanswered calls → auto-retry in 4h | Live |
| All interactions logged to Supabase + Notion | Live |
| Retool dashboard | Not built |
