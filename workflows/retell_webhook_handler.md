# Post Call Analysis — MCO

**n8n ID:** `r8XKHCnL4vju2E4j`  
**Entry:** `POST /webhook/9cdd28e8-7cfd-4765-a623-cda2d1b9f7a7`  
**Triggered by:** Retell AI `call_analyzed` event after every outbound call completes

## Objective

Receive the Retell post-call webhook, log the call transcript/summary to Supabase, and — if the call was not answered — automatically re-queue a new voice follow-up for 4 hours later so the system retries automatically.

## Node Sequence

1. **Webhook1** — receives Retell `call_analyzed` payload
2. **Filter** — only processes `event_type = "call_analyzed"`, drops all others
3. **Build MCO Write Payload** — Code node, extracts from Retell payload:
   - `lead_email` from `call.retell_llm_dynamic_variables.lead_email`
   - `phone_e164` from `call.to_number` (validated E.164 format)
   - `event_id` — deterministic UUID derived from `call.call_id`
   - `content` from `call_analysis.call_summary` or `call.transcript`
   - `content_type: "summary"`
   - `intent` from `call_analysis.custom_analysis_data.qualified_status` (true → `interested`, else `unknown`)
   - `disconnection_reason` from `call.disconnection_reason`
   - `queue_id` from `call.metadata.queue_id` (set by Call Agent when creating the Retell call)
   - `was_answered` — false if disconnection_reason is `dial_no_answer`, `voicemail`, `dial_failed`, or `busy`
4. **POST /mco-write-event** — logs call summary to Supabase (channel=voice, direction=outbound)
5. **Check Retry** — Code node, reads from Build MCO Write Payload output:
   - Sets `needs_retry = !payload.was_answered`
   - Passes `queue_id`, `lead_email`, `lead_id` (from Write Event response), `phone_e164`
6. **Needs Retry?** — IF node (typeVersion 2, loose)
   - Output 0 (true = not answered) → Re-Queue Voice Call
   - Output 1 (false = answered) → execution ends
7. **Re-Queue Voice Call** — POST to Supabase `/rest/v1/follow_up_queue`:
   - `target_channel: "voice"`, `status: "pending"`
   - `scheduled_for: NOW() + 4h`
   - `follow_up_context: "Retry: call not answered ({disconnection_reason})"`

## "Not Answered" Disconnection Reasons

The following `disconnection_reason` values trigger a retry:
- `dial_no_answer` — lead did not pick up
- `voicemail` — call went to voicemail
- `dial_failed` — technical failure connecting
- `busy` — line was busy

All other disconnection reasons (e.g., `user_hangup`, `agent_hangup`) are treated as "answered" — no retry.

## How queue_id Flows Through

1. Coordinator triggers Call Agent with `queue_id`
2. Call Agent passes `queue_id` in `retell_body.metadata.queue_id` when creating the Retell call
3. Retell echoes `call.metadata` back in the `call_analyzed` webhook
4. Post Call Analysis reads `call.metadata.queue_id` and stores it in `Check Retry` output
5. Re-Queue Voice Call can reference `queue_id` for tracking (the original queue item is already marked `sent` by the Coordinator — the re-queue creates a new row)

## Notes
- `lead_email` MUST be passed in Retell dynamic variables when creating the call — it's required to identify the lead
- The original queue item is marked `sent` by the Coordinator at the time the call is triggered (not when it's answered). The Re-Queue creates a new separate queue row.
- There is no retry cap — the system will keep re-queueing until the call is answered. Add retry count tracking to `follow_up_context` if you want to limit attempts.
- Error handling: `neverError: true` on the Write Event call means failures are logged but don't halt execution.
