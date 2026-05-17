# Fetch Cross-Channel Context

**n8n ID:** `UJoDCfkmD3NJHktk`  
**Entry:** `POST /webhook/mco-fetch-context`  
**Node count:** 5 | **Status:** Active

## Objective
Return a formatted conversation history block for a given lead, covering all channels. Called by AI agent nodes (Coordinator, Gmail Reply Agent, Call Agent) before they generate a response, so they always have the full cross-channel history.

## When to Use
Call this immediately before any AI node that generates a reply to a lead. Wire the returned `context_block` into the AI system prompt.

## Required Inputs — at least one identifier

| Field | Required | Notes |
|---|---|---|
| `lead_id` | one of these | UUID — fastest lookup, preferred when available |
| `lead_email` | one of these | Lowercase canonical email |
| `linkedin_profile_url` | one of these | Full LinkedIn URL |
| `phone_e164` | one of these | E.164 format |
| `requesting_channel` | no | Current channel (prepends context header e.g. "You are replying on LINKEDIN") |
| `max_events` | no | Max conversation events to return. Default: 20 |

## Node Sequence

1. **Webhook** — `POST /mco-fetch-context`
2. **Setup & Validate** — Code node
   - Require at least one identifier
   - Normalise lead_email to lowercase if present
3. **Fetch Context RPC** — Supabase `fetch_lead_context()` RPC
   - Resolves lead by whichever identifier is supplied (lead_id → email → linkedin_profile_url → phone)
   - Returns full lead record + last N conversations ordered by timestamp DESC
4. **Format Context Block** — Code node
   - Reverses result order (DESC query → ASC display for readability)
   - Prepends channel-awareness header if `requesting_channel` provided
   - Formats each conversation entry with timestamp, channel icon, direction, content
5. **Return Context** — 200 OK

## Output

```json
{
  "context_block": "=== Cross-Channel Conversation History ===\nLead: John Smith (Acme Corp)\nOverall Intent: interested (updated: 2026-05-03)\n\n[2026-05-03 09:15 UTC] [EMAIL] EMAIL - Inbound\nLead replied saying they're interested in a demo next week.\nIntent: interested\n\n[2026-05-02 16:42 UTC] [LINKEDIN] LINKEDIN - Outbound\nConnection request sent via Aimfox campaign.\nIntent: no_action\n",
  "lead": {
    "lead_id": "uuid-here",
    "lead_email": "john@acme.com",
    "full_name": "John Smith",
    "company": "Acme Corp",
    "overall_intent": "interested",
    "last_active_channel": "email",
    "linkedin_conversation_urn": "urn:li:msg:abc123",
    "linkedin_profile_url": "https://linkedin.com/in/johndoe"
  },
  "event_count": 2,
  "overall_intent": "interested",
  "lead_email": "john@acme.com",
  "lead_id": "uuid-here"
}
```

## How to Inject into AI Prompt

In the n8n AI Agent node system prompt, append:

```
---
CROSS-CHANNEL CONTEXT (read before replying):
{{$json.context_block}}
---
Use this context to avoid repeating information, reference prior conversations naturally,
and be consistent with commitments made on other channels.
```

## Edge Cases
- **Lead not found** → `{ context_block: "(No prior conversation history found for this lead.)", lead: null, event_count: 0, overall_intent: "unknown" }`
- **Lead exists, no conversations** → returns lead record with empty conversation list and note
- **context_block too long** → max_events=20 keeps output manageable

## Notes
- Read-only — never writes to Supabase.
- The `lead` object in the response always includes `linkedin_conversation_urn` — the Coordinator reads this to decide whether to DM (has URN) or add to campaign (no URN).
- Always call this even if you're unsure prior context exists — the empty response is handled gracefully.
- `lead_id` (UUID) is returned in the response — downstream nodes should pass this to Write Event for faster lead resolution.
