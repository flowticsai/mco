# Retell Webhook Handler

## Objective
Receive post-call/post-SMS webhooks from Retell AI, resolve the caller's phone number to a canonical lead email, and write the interaction to the unified conversation store.

## Status
PENDING — waiting for user to share Retell AI ("Inadin") workflow JSON files.

## When to Use
Triggered automatically by Retell AI after every call ends or SMS exchange completes. Do not call manually.

## Expected Retell Webhook Payload (standard fields)
```json
{
  "event": "call_ended",
  "call_id": "call_abc123",
  "from_number": "+15551234567",
  "to_number": "+15559876543",
  "direction": "inbound",
  "duration_ms": 187000,
  "transcript": "Agent: Hello, this is Nextus AI...\nLead: Hi, I'm interested in...",
  "call_analysis": {
    "call_summary": "Lead expressed strong interest in a demo.",
    "user_sentiment": "Positive",
    "call_successful": true,
    "custom_analysis_data": {
      "intent": "interested"
    }
  },
  "start_timestamp": 1746354600000,
  "end_timestamp": 1746354787000
}
```

## Planned Node Sequence

1. **Webhook Entry** — POST `/mco-retell-webhook`
2. **Verify HMAC Signature** — Code node
   - Header: `x-retell-signature`
   - Secret: Retell webhook signing secret (stored in n8n credential or environment)
   - Reject 401 if signature invalid
3. **Extract Fields** — Code node
   - call_id, from_number, to_number, direction, transcript, duration_ms
   - intent from `call_analysis.custom_analysis_data.intent`
   - summary from `call_analysis.call_summary`
   - channel: determine `voice` vs `sms` from call type
4. **Resolve Phone → Email** — Supabase SELECT
   - `SELECT lead_email FROM phone_map WHERE phone_e164 = '{{from_number}}'`
   - If found → use lead_email
   - If not found → trigger Clay enrichment (see below) and store placeholder temporarily
5. **Call Write Conversation Event** — Execute Workflow or HTTP POST
   - channel: `voice` or `sms`
   - direction: from Retell payload
   - content: transcript (or summary if transcript exceeds 2000 chars)
   - content_type: `transcript` or `summary`
   - metadata: `{ call_id, duration_ms, from_number, to_number }`
   - If intent = interested/booking → trigger_cross_channel: true, target_channels: ["email","linkedin"]
6. **Return 200** to Retell

## Phone → Email Resolution (when not in phone_map)

If `from_number` is not in `phone_map`:
1. Store a temporary record in `phone_map` with source=retell, confidence=low, lead_email=NULL
2. POST to Clay enrichment webhook: `https://api.clay.com/v3/sources/webhook/...` (URL TBD — get from user)
   - Payload: `{ phone: from_number, call_id: call_id }`
3. Clay webhook will resolve and call back → handled by `clay_enrichment_receiver` workflow
4. Conversation event still gets written immediately with a placeholder email like `phone_{from_number}@retell.placeholder` so no data is lost

## Notes
- This workflow cannot be fully built until Retell AI workflow JSONs are shared — they contain the webhook URLs, signing secrets, and any custom call analysis fields.
- If Retell sends SMS transcripts differently from voice, a Switch node on `call_type` will handle routing.
