# Aimfox Connection Accepted Handler

**n8n ID:** `WTbIAJCZGtppAT91`  
**Entry:** `POST /webhook/mco-aimfox-accepted`  
**Node count:** 9 | **Status:** Active  
**Triggered by:** Aimfox webhook — fires when a LinkedIn connection request is accepted

## Objective
When a LinkedIn connection is accepted: send a fixed "thanks for connecting" opening message, log the event to Supabase (including `conversation_urn`), and queue a follow-up dispatch 24 hours later so the Coordinator can continue the conversation via LinkedIn DM.

## Trigger Payload (from Aimfox)

| Field | Source | Notes |
|---|---|---|
| `account_id` | Aimfox payload | The Aimfox account that sent the connection request |
| `lead_id` | Aimfox payload | Aimfox's internal lead ID (used as `recipients` in DM API) |
| `lead_urn` | Aimfox payload | LinkedIn URN of the lead |
| `lead_name` | Aimfox custom variables | Extracted from `Get Lead Custom Variables` RPC |
| `lead_email` | Aimfox custom variables | May be null for LinkedIn-only leads |
| `linkedin_profile_url` | Aimfox payload | Full LinkedIn URL |

## Node Sequence

1. **Webhook** — receives Aimfox `accepted` event
2. **Extract Fields** — Code node, parses real Aimfox `accepted` payload: reads `account_id` from `event.account.id`, `lead_id` + `lead_urn` + `lead_name` from `event.target`, constructs `linkedin_profile_url` from `target.public_identifier` if `profile_url` is absent. Falls back to legacy flat format for test payloads.
3. **Get Lead Custom Variables** — `GET Aimfox /accounts/:id/leads/:lead_urn/custom-variables`
4. **Extract LEAD_EMAIL** — Code node, reads LEAD_EMAIL and lead_name from custom variables response
5. **Send Thanks Message** — `POST Aimfox /accounts/:id/conversations`
   - Body: `{ message: "Hi {lead_name}, great to connect! Looking forward to learning more about what you're working on.", recipients: [lead_id] }`
   - Fixed text — no AI generation. Fires immediately.
6. **Extract Conversation URN** — Code node
   - Merges all fields from Extract LEAD_EMAIL back into the output
   - Reads `conversation_urn` from Aimfox response (tries: `conversation_urn`, `urn`, `data.conversation_urn`, `conversation.urn`)
   - Sets `thanks_sent: true`
   - Generates `log_event_id` (UUID) using `Math.random()` — do NOT use `require('crypto')`, it is blocked by the n8n task runner
7. **Log Connection Accepted** — `POST /mco-write-event`
   - channel: `linkedin`, direction: `outbound`
   - content: the thanks message text
   - sender_name: `Anik Hanik`
   - intent: `no_action`
   - `linkedin_conversation_urn` — stored on lead record via upsert_lead in Write Event
   - metadata: `{ aimfox_account_id, lead_id (Aimfox), lead_urn, conversation_urn, triggered_by: "connection_accepted" }`
8. **Queue LinkedIn Follow-Up** — `POST Supabase /rest/v1/follow_up_queue`
   - `lead_id`: from Write Event response (Supabase UUID)
   - `lead_email`: from Write Event response
   - `target_channel: "linkedin"`, `status: "pending"`
   - `scheduled_for: NOW() + 24h`
   - `follow_up_context` (JSON string): references `$('Extract Conversation URN').first().json` for:
     - `conversation_urn` — the LinkedIn thread URN
     - `aimfox_account_id` — account_id from the Aimfox webhook
     - `linkedin_urn` — lead_urn from the webhook
     - `linkedin_profile_url` — from the webhook
9. **Return OK** — 200 response

## What This Enables

After this handler runs, Supabase has:
- A `conversations` row for the thanks message (channel=linkedin, direction=outbound)
- The lead's `linkedin_conversation_urn` set on the `leads` record
- A `follow_up_queue` row scheduled 24h later

When the Dispatcher fires and picks up that queue row, it calls the Coordinator. The Coordinator fetches context, finds `conversation_urn` on the lead record, and takes the **LinkedIn DM path** — replying inside the existing conversation thread.

## Notes
- `lead_email` may be null — the handler works for LinkedIn-only leads. The Supabase lead_id (returned by Write Event) is used for the queue row.
- The thanks message is fixed text — not AI-generated. This is intentional for consistency.
- `conversation_urn` is extracted from the Aimfox Send Thanks Message response (step 5). The `Queue LinkedIn Follow-Up` node (step 8) reads this from the `Extract Conversation URN` node directly — NOT from `$json` which at that point is the Write Event HTTP response.
- `aimfox_account_id` similarly must be read from `$('Extract Conversation URN').first().json.account_id`, not from `$json`.
- The 24h delay is set in `Queue LinkedIn Follow-Up`. Adjust `Date.now() + 24*60*60*1000` if a different interval is needed.
- The Dispatcher's `Fetch LinkedIn Meta` also looks up `aimfox_account_id` from the conversations table as a backup, using the metadata stored in step 7.
