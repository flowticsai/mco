# Aimfox Nextus AI Reply Agent — MCO

**n8n ID:** `SPN1NLyHH1LcfViD`  
**Entry:** Aimfox webhook — fires on `new_reply` / `reply` events  
**Node count:** 38 | **Status:** Active  
**Triggered by:** Aimfox when a lead sends a LinkedIn message

## Objective

React to inbound LinkedIn messages. Checks whether the conversation is open (Google Sheets gate), classifies the lead's intent, generates an AI reply using the full LinkedIn thread **plus MCO cross-channel context**, sends the reply, and writes the event to Supabase.

## Gate — Google Sheets Conversation State

Before any AI processing, the agent looks up the conversation in Google Sheets (`Get row(s) in sheet1`). If the sheet row is missing or the conversation is marked closed, the agent skips silently. This is intentional: not every LinkedIn message needs a response.

**Critical:** if the Google Sheets credential expires, all LinkedIn replies are silently dropped.

## Node Sequence

1. **Webhook** — receives Aimfox `new_reply` / `reply` event
2. **Code2** — drops webhooks older than 48 hours (Aimfox retry guard). If `event.created_at` is more than 48h ago, nothing downstream runs.
3. **Code** — parses the raw Aimfox payload: extracts `sender.first_name`, `sender.last_name`, `sender.public_identifier`, `account_id`, `conversation_urn`, and the message body.
4. **Get row(s) in sheet1** — looks up the conversation in Google Sheets by conversation URN. If not found → stop.
5. **If5** — checks if the sheet row indicates the conversation is open.
6. **If6** — secondary gate check.
7. **Thread** — `GET Aimfox /accounts/:id/conversations/:urn` — fetches the full LinkedIn thread history.
8. **If13** — checks if the lead's reply qualifies for AI processing (e.g., is not an automated Aimfox system message).
   - **YES** → `MCO: Fetch Context` → `AI Agent1`
   - **NO** → `Text Classifier`

### AI Reply Path (If13 = YES)

9. **MCO: Fetch Context** — `POST /mco-fetch-context`
   - Body: `{ linkedin_profile_url: 'https://www.linkedin.com/in/' + sender.public_identifier, requesting_channel: 'linkedin' }`
   - Returns full lead record + last 20 cross-channel conversations as `context_block`
   - `neverError: true` — if this fails, AI Agent1 still runs with `(No cross-channel history found.)`
10. **AI Agent1** — Anthropic model, generates a LinkedIn reply.
    - Prompt includes: lead name, full LinkedIn thread (`Code.Message`), and MCO cross-channel history (`MCO: Fetch Context.context_block`)
    - System prompt: Nextus ITAD persona, 3–4 sentence LinkedIn DM style, guides toward a call
11. **Send Reply** — `POST Aimfox /accounts/:id/conversations/:urn` — sends the generated reply
12. **MCO: Write Returning** or **MCO: Write First Interested** — Code node, builds the Write Event payload
13. → Write Event (`POST /mco-write-event`)

### Classifier Path (If13 = NO)

9. **Text Classifier** — classifies the message intent
10. **Switch** — routes by intent
11. **Decision Maker Agent** — handles edge cases (booking, referral, not interested)
12. **If17 / If22** — further routing
13. → appropriate MCO write path

## MCO Write Nodes

| Node | When it fires | What it does |
|---|---|---|
| `MCO: Build Seed B1` | First contact on this lead | Writes seed event to Supabase |
| `MCO: Write Returning` | Lead has prior history | Writes inbound message + intent to Supabase |
| `MCO: Write First Interested` | Lead first shows interest | Writes event + queues cross-channel follow-up |

Each calls `POST /mco-write-event`. `MCO: Write First Interested` sets `trigger_cross_channel: true` + `target_channels: ["email"]` to queue an email follow-up.

## Cross-Channel Context (Added 2026-05-17)

The `MCO: Fetch Context` node was added between `If13` and `AI Agent1` so the AI sees the lead's full history across all channels (email, LinkedIn, voice) before generating a reply — not just the LinkedIn thread.

The `AI Agent1` prompt structure:
```
Lead is: {first_name} {last_name}

=== LinkedIn Thread ===
{Code.Message — full thread from Aimfox}

=== Cross-Channel History (all channels) ===
{MCO: Fetch Context.context_block || '(No cross-channel history found.)'}
```

## Key Design Notes

- **48h webhook guard (Code2):** Aimfox retries webhooks up to 6 times. Code2 drops retries older than 48h to prevent stale processing.
- **Google Sheets gate:** conversation state lives in Sheets, not Supabase. If the Sheets credential expires, replies stop silently.
- **`neverError: true` on MCO: Fetch Context:** if Supabase context fetch fails, the AI still runs. The reply quality degrades (no cross-channel context) but no error is thrown.
- **`neverError` on Send Reply:** if Aimfox returns an error, the node shows success. Always check output JSON for `{ "status": "fail" }`.
- **LinkedIn-only leads:** `lead_email` may be null. MCO: Fetch Context uses `linkedin_profile_url` as the identifier.

## Error Handling

| Failure | Effect |
|---|---|
| Google Sheets credential expired | All LinkedIn replies silently skipped |
| Aimfox token expired | Send Reply fails silently (neverError). Check output JSON. |
| MCO: Fetch Context unavailable | AI Agent1 runs without cross-channel context. `context_block` fallback: `'(No cross-channel history found.)'` |
| Lead not in Supabase | context_block is empty. AI generates from LinkedIn thread only. |
| Code2 drops event (>48h old) | Nothing runs downstream. Expected behaviour for Aimfox retries. |
