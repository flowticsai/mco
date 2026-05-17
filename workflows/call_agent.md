# Flowtics AI Call Agent — MCO

**n8n ID:** `xE8mFF8HxPaSXNmi`  
**Entry:** `POST /webhook/3adf4681-721b-452e-94b3-5618887a15c4` + Schedule every 4h  
**Node count:** 14 | **Status:** Active  
**Triggered by:** Coordinator (voice path) via webhook, or schedule every 4h

## Objective

For each pending `voice` row in `follow_up_queue`, fetch the lead record, summarise prior conversation context using GPT-4o-mini, then place an outbound Retell call. The `queue_id` is embedded in Retell metadata so Post Call Analysis can mark the row and re-queue if the call is not answered.

## Two Entry Paths

| Trigger | How | Data source |
|---|---|---|
| Schedule (every 4h) | n8n cron → Supabase fetch → loop | Fetches all pending `voice` rows from Supabase |
| Webhook (on-demand) | Coordinator POSTs to webhook URL | Receives single lead payload in webhook body |

Both paths converge at **Unified Input**, which normalises the data shape so all downstream nodes work identically.

## Required Inputs (webhook path)

| Field | Notes |
|---|---|
| `queue_id` | UUID of the `follow_up_queue` row |
| `lead_email` | Used to fetch lead record and as Retell dynamic variable |
| `lead_id` | Supabase UUID (optional but preferred) |
| `phone_e164` | E.164 format. Required to place the call |
| `target_channel` | Should be `voice` |
| `trigger_channel` | Where this lead came from (e.g. `email`, `linkedin`) |
| `follow_up_context` | Optional context string passed from queue row |

## Node Sequence

1. **Webhook / Schedule** — entry point
2. **Normalize Webhook Input** *(webhook path only)* — Code node
   - Reads `lead_email`, `lead_id`, `phone_e164`, `queue_id`, `target_channel`, `trigger_channel`, `follow_up_context` from body
3. **Fetch Pending Voice Leads** *(schedule path only)* — Supabase REST, fetches pending voice rows
4. **Loop One Lead at a Time** *(schedule path only)* — SplitInBatches, processes one lead per iteration
5. **Unified Input** — Code node, merges both paths into a single shape:
   - `lead_email`, `lead_id`, `phone_e164`, `queue_id`, `target_channel`, `trigger_channel`, `follow_up_context`, `triggered_by_coordinator`
6. **Fetch Lead Record** — Supabase REST, fetches lead by `lead_email`
   - Returns: `full_name`, `company`, `phone_e164`, `overall_intent`
7. **POST /mco-fetch-context** — fetches last 20 conversations for the lead
8. **Build OpenAI Request** — Code node, assembles a prompt from context for GPT-4o-mini
9. **Summarize Prior Conversation** — OpenAI `gpt-4o-mini`, ≤180-word prior conversation brief
10. **Prepare Retell Variables** — Set node, builds Retell dynamic variables:
    - `previous_conversation_summary` — from OpenAI response
    - `lead_email` — from Unified Input
    - `first_name` — from `Build OpenAI Request` lead object
    - `company_name` — from `Build OpenAI Request` lead object
    - `phone_e164` — from `Fetch Lead Record` (fallback: Unified Input)
    - `booking_link` — `https://calendly.com/mahfujurrahman511351/30min`
11. **Build Retell Request** — Code node, assembles Retell API payload:
    - `agent_id: "agent_ff863b1414049444c174360809"` (Maya - Flowtics AI)
    - `from_number: "+15722124790"`
    - `to_number: phone_e164`
    - `metadata: { queue_id, lead_email, source: "n8n_flowtics_followup" }` — `queue_id` is echoed back in Retell webhook for Post Call Analysis
12. **Retell: Create Phone Call** — POST to Retell API
13. **Skip Mark Queue?** — IF node. Checks `triggered_by_coordinator === true`.
    - YES (webhook path) → skip. The Coordinator already marked the queue row `sent` before triggering this workflow.
    - NO (schedule path) → Mark Queue Sent
14. **Mark Queue Sent** *(schedule path only)* — PATCH `follow_up_queue` row to `status: "sent"`
15. **Loop One Lead at a Time** *(schedule path)* — continue to next lead

## What Happens After the Call

Retell fires `call_analyzed` to Post Call Analysis (`r8XKHCnL4vju2E4j`) when the call ends. Post Call Analysis:
- Logs the call transcript/summary to Supabase via Write Event
- If not answered (`dial_no_answer`, `voicemail`, `dial_failed`, `busy`) → re-queues a new voice row 4 hours later
- If answered → no re-queue; the original queue row is already `sent`

## Key Design Notes

- **`queue_id` in Retell metadata** — this is how Post Call Analysis knows which queue row to re-queue after an unanswered call. Never remove it from `Build Retell Request`.
- **`phone_e164` flow** — must pass through `Normalize Webhook Input` → `Unified Input` → `Prepare Retell Variables`. If any node drops it, the call fails silently. Reference: `$('Fetch Lead Record').first().json.phone_e164 || $('Unified Input').item.json.phone_e164`.
- **`triggered_by_coordinator` flag** — when the Coordinator triggers this workflow via webhook, it passes `triggered_by_coordinator: true`. The `Skip Mark Queue?` IF node uses this to skip the `Mark Queue Sent` step on the webhook path, since the Coordinator already marked the row `sent`. On the schedule path this flag is absent (`false`), so `Mark Queue Sent` runs normally.
- **No dedup on calls** — if the workflow runs twice for the same queue row (Dispatcher race), two calls go out. The queue row is marked `sent` at the end, so the second run fires before the PATCH. Low probability given 4h schedule interval and fast execution.
- **neverError NOT set** — Retell call failures will show as errors in n8n executions. Check executions tab if a lead is not getting called.

## Error Handling

- Missing `phone_e164` → Retell returns 422, execution errors (visible in n8n)
- Retell API key invalid → 401 error, execution stops at step 12
- Lead not found in Supabase → `Fetch Lead Record` returns empty; `full_name` and `company` fall back to empty strings
- OpenAI failure → `previous_conversation_summary` falls back to `'No prior conversation on record.'`
