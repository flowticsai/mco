# Centralized Follow-Up Coordinator

**n8n ID:** `KXKcCYRnK4V8v9k7`  
**Entry:** `POST /webhook/mco-followup`  
**Node count:** 26 | **Status:** Active  
**Triggered by:** FollowUp Queue Dispatcher (every 15 min)

## Objective
Send a follow-up message to a lead on a specified channel. Fetches cross-channel context, generates a personalised AI reply, then routes to the correct sending method based on channel and lead state.

## Required Inputs

| Field | Required | Notes |
|---|---|---|
| `queue_id` | YES | UUID from `follow_up_queue` — used to mark the item sent/skipped |
| `target_channel` | YES | `email` \| `linkedin` \| `voice` |
| `lead_email` | one of these | Lowercase canonical email |
| `lead_id` | one of these | UUID from leads table |
| `linkedin_profile_url` | one of these | Required for LinkedIn campaign path |
| `linkedin_urn` | no | Aimfox lead URN |
| `aimfox_account_id` | no | Required for LinkedIn send paths |
| `conversation_urn` | no | If provided, overrides Supabase lookup for LinkedIn DM path |
| `lead_name` | no | Display name for personalisation |
| `phone_e164` | no | Required for voice path |
| `trigger_reason` | no | Logged to Supabase metadata. Default: `follow_up` |

## Node Sequence

1. **Webhook** — receives payload from Dispatcher
2. **Setup & Validate** — Code node
   - Require at least one identifier (lead_email, lead_id, or linkedin_profile_url)
   - Require: target_channel, queue_id
   - Pass all LinkedIn identifiers forward
3. **Fetch Context** — `POST /mco-fetch-context`
   - Passes lead_id (preferred) or lead_email
   - Returns lead record (including `linkedin_conversation_urn`) + conversation history
4. **Merge Context** — Code node
   - Merges Setup output with Context output
   - Extracts `conversation_urn` from lead record (`lead.linkedin_conversation_urn`) with fallback to Setup payload value
   - Extracts `lead_id` from lead record
   - Sets `lead_name` from lead record or setup fallback
5. **Route by Channel** — Switch node
   - Output 0: `email`
   - Output 1: `linkedin`
   - Output 2: `voice`

### Email path
6. **Email: Claude Model** — Anthropic credential (`Anthropic account 2`)
7. **Email: Generate Message** — LLM chain with full context block in system prompt
8. **Email: Extract Message** — Code node, extracts clean text from AI response
9. **Send Email (Gmail)** — Gmail node, from `team@flowticsai.com` (credential `Gmail account` ID `IC6TPjXMVxTyn2R9`)
10. After Send → Log → Mark Queue Sent → Return OK

### LinkedIn path
6. **LinkedIn: Has Conversation URN?** — IF node (typeVersion 2, loose)
   - Checks `$json.conversation_urn isNotEmpty`

   **YES — existing conversation (connected lead, has DM thread):**
   7. **LinkedIn: Claude Model** — Anthropic
   8. **LinkedIn: Generate Message** — LLM chain (null-safe: `trigger_channel` falls back to `'PROACTIVE'` if null)
   9. **LinkedIn: Extract Message** — Code node, outputs `generated_message`
   10. **Reply to Existing Conversation** — `POST Aimfox /accounts/:aimfox_account_id/conversations/:conversation_urn`
       - Body: `{ message: $('LinkedIn: Extract Message').first().json.generated_message }` — NO `/messages` suffix
       - `neverError: true` — check output JSON for `status: "fail"` if DM seems not delivered
   11. After Send → Log → Mark Queue Sent → Return OK

   **NO — no connection yet (email-sourced lead without LinkedIn connection):**
   7. **Add to LinkedIn Campaign** — `POST Aimfox /campaigns/6e2feb86-b9c6-4c18-87fa-c5fe5e41682f/audience`
      - Body: `{ profile_url: linkedin_profile_url }` — no `/accounts/:id` prefix; uses normal API key
      - Sends Aimfox connection request on behalf of the account
   8. **Log Connection Request** — `POST /mco-write-event`
      - channel: `linkedin`, direction: `outbound`
      - content: `"Connection request sent via Aimfox campaign"`
      - intent: `no_action`
      - metadata: `{ campaign_id, aimfox_account_id, queue_id, triggered_by: "coordinator_linkedin_no_urn" }`
   9. **Mark Queue Skipped** — `PATCH Supabase follow_up_queue`, status: `skipped`
   10. **Return OK (Campaign)** — 200 response, does NOT go through After Send

### Voice path
6. **Voice: Trigger Call Agent** — `POST /webhook/3adf4681-721b-452e-94b3-5618887a15c4`
   - Passes: `lead_email`, `lead_id`, `phone_e164`, `queue_id`, `target_channel: "voice"`, `trigger_channel`, `follow_up_context`, `triggered_by_coordinator: true`
   - The `triggered_by_coordinator` flag tells the Call Agent to skip its own `Mark Queue Sent` step — the Coordinator owns that responsibility on the webhook path
7. **Voice: Prepare Result** — Code node
   - Stamps `generated_message: "Voice call follow-up initiated via Call Agent"`, `send_status: "sent"`
8. After Send → Log → Mark Queue Sent → Return OK

### Shared tail (email + LinkedIn DM + voice)
- **After Send** — Code node. Spreads the send API response, adds `send_status: 'sent'`, and picks up `generated_message` via try-catch from whichever Extract Message node ran (LinkedIn → Email → Voice). This is how `generated_message` survives past the HTTP response that overwrites `$json`.
- **Log to Supabase** — `POST /mco-write-event`. Uses `$json.generated_message` (from After Send) as `content`. Also passes `linkedin_urn` + `linkedin_profile_url` from Merge Context so Write Event can resolve LinkedIn-only leads (no email).
- **Mark Queue Sent** — `PATCH Supabase follow_up_queue`, status: `sent`. All header fields (`apikey`, `Authorization`) reference `$('Merge Context').first().json.SUPABASE_KEY` — NOT `$json.SUPABASE_KEY` which is overwritten by the HTTP response.
- **Return OK** — 200 response

## Key Design Decisions

**Why `Has Conversation URN?` is the only LinkedIn branch:**
- LinkedIn-sourced leads always have `linkedin_conversation_urn` in Supabase (written by the Connection Accepted Handler).
- Email-sourced leads have no LinkedIn connection yet → add to campaign → stop.
- Once the connection is accepted, the Handler writes `conversation_urn` to Supabase AND queues a new `follow_up_queue` row (24h later). Next time the Coordinator fires, it finds the URN and takes the DM path.

**Why "Add to Campaign" does NOT go to After Send:**
- Adding to a campaign is a connection request, not a sent message.
- Marking it as `sent` would falsely indicate a conversation happened.
- The queue item is marked `skipped`. A new queue entry is created by the Connection Accepted Handler when the connection is actually accepted.

**Why voice is marked `sent` immediately:**
- The Coordinator only triggers the call; it doesn't know if it was answered.
- Post Call Analysis handles the outcome: if not answered, it re-queues a new voice follow-up 4h later.
- The original queue item is `sent` (call was triggered); the retry is a fresh queue row.

## Error Handling
- Missing identifier → 400
- Aimfox API failure → neverError=true, still logs and marks queue (with error in metadata)
- Supabase log failure → non-fatal, Mark Queue Sent still runs
