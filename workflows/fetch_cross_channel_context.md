# Fetch Cross-Channel Context

## Objective
Return a formatted conversation history block for a given lead, covering all channels. Called by AI agent nodes (Decision Maker, Reply Agent, etc.) before they generate a response, so they are aware of what has already been discussed with the lead on other channels.

## When to Use
Call this immediately before any AI node that generates a reply to a lead. Wire the returned `context_block` into the AI system prompt as an additional context section.

## Required Inputs

| Field | Required | Notes |
|-------|----------|-------|
| lead_email | YES | Canonical lead email (or placeholder) |
| requesting_channel | no | The channel currently generating a reply (for context labeling) |
| max_events | no | Max conversation events to return. Default: 20 |

## Node Sequence

1. **Webhook/Execute Workflow Entry** — POST `/mco-fetch-context`
2. **Validate** — Code node
   - Require: lead_email
   - Default max_events to 20 if not provided
   - Normalize lead_email to lowercase
3. **Query Lead** — Supabase SELECT
   - `SELECT * FROM leads WHERE lead_email = '{{lead_email}}'`
   - If no row found → return empty context (lead is unknown, first interaction)
4. **Query Conversations** — Supabase SELECT
   - `SELECT * FROM conversations WHERE lead_email = '{{lead_email}}' ORDER BY timestamp DESC LIMIT {{max_events}}`
5. **Format Context Block** — Code node
   - Reverse result order (DESC query → ASC display)
   - Build text block (see format below)
   - Truncate each content entry to 300 chars in the context block
6. **Return Response** — 200 OK with context payload

## Output Format

```json
{
  "context_block": "=== Cross-Channel Conversation History ===\nLead: John Smith (Acme Corp)\nOverall Intent: interested (updated: 2026-05-03)\n\n[2026-05-03 09:15 UTC] 📧 EMAIL — Inbound\nLead replied saying they're interested in a demo next week.\nIntent: interested\n\n[2026-05-02 16:42 UTC] 💼 LINKEDIN — Outbound\nSent connection accepted follow-up message.\nIntent: no_action\n",
  "lead": {
    "lead_email": "john@acme.com",
    "full_name": "John Smith",
    "company": "Acme Corp",
    "overall_intent": "interested",
    "last_active_channel": "email"
  },
  "event_count": 2,
  "overall_intent": "interested"
}
```

## How to Inject into AI Prompt

In the n8n AI Agent node system prompt, append:

```
---
CROSS-CHANNEL CONTEXT (read before replying):
{{$json.context_block}}
---
Use this context to avoid repeating information, reference prior conversations naturally, and be consistent with commitments made on other channels.
```

## Edge Cases
- **Lead not found** → return `{ context_block: "(No prior conversation history found for this lead.)", event_count: 0, overall_intent: "unknown" }`
- **No conversations yet** → return `{ context_block: "(Lead exists but no conversation history recorded yet.)", event_count: 0 }`
- **context_block too long** → max_events=20 with 300-char truncation keeps output under ~8,000 chars, safe for any model's context

## Notes
- This is a read-only workflow. It never writes to Supabase.
- Call this even if you're unsure whether prior context exists — the empty response is handled gracefully.
- The requesting_channel field is optional but useful for labeling the context block ("You are currently replying on LINKEDIN. Prior context below:")
