# MCO — Gmail Reply Agent

**n8n ID:** `mFBOGdMAsXRKD1Pv`  
**Entry:** Gmail trigger — fires on new emails to `team@flowticsai.com`  
**Node count:** 18 | **Status:** Active  
**Triggered by:** Any inbound email to the Gmail inbox

## Objective

React to inbound email replies from leads who are already in our outbound email campaign. Checks that the sender is one of our leads (gate), fetches cross-channel context, classifies intent, generates a focused email reply, sends it via Gmail, and logs both the inbound and outbound events to Supabase.

## How Emails Arrive

Instantly sends outbound emails to leads and CCs `team@flowticsai.com` on every send. This means two types of email hit the Gmail trigger:

1. **CC copies of our own outbound** — From: our Instantly sender, To: lead, Cc: `team@flowticsai.com`. These must be dropped.
2. **Lead replies** — From: lead's email, To: `team@flowticsai.com`. These are what we want to process.

The two-filter gate handles this:

## Gate — Only Our Campaign Leads

**Filter 1 — `Extract & Filter` (Code node):**
- Normalises Gmail trigger header casing — Gmail outputs `From`, `To`, `Subject` (capitalised). Code reads both forms with fallback: `email.from || email.From`.
- Outputs `from_email`, `to_email`, `from_name` — does NOT decide `lead_email` yet (that is determined by the Supabase lookup)
- Drops emails where FROM could not be parsed
- Drops emails from `noreply` / `no-reply` senders
- Drops emails that are not replies (requires `Re:` subject prefix or `inReplyTo` / `in-reply-to` header)
- Drops emails with empty body

**Filter 2 — `Supabase: Check Outbound` → `Filter: Our Thread Only`:**
- Queries `leads` table with OR filter: `leads?or=(lead_email.eq.{from_email},lead_email.eq.{to_email})&select=lead_id,lead_email,full_name&limit=1`
- Checks both FROM and TO against Supabase leads — whichever matches is the lead
- This handles two distinct scenarios:
  - **Scenario 1 (Instantly outbound CC):** From=Instantly sender, To=lead, Cc=team@flowticsai.com → TO matches Supabase → lead is TO
  - **Scenario 2 (Lead replies to our email):** From=lead, To=team@flowticsai.com → FROM matches Supabase → lead is FROM
- `Filter: Our Thread Only` sets `lead_email` and `lead_name` from the Supabase match result
- All downstream nodes (Fetch Context, Reply Agent, Write Inbound, Write Outbound, Send Gmail Reply) use this resolved `lead_email`

## Node Sequence

1. **Gmail Trigger** — fires on new email to `team@flowticsai.com`
2. **Extract & Filter** — Code node, parses sender email/name, subject, body (≤3000 chars). Drops noreply and non-replies.
3. **Supabase: Check Outbound** — `GET Supabase conversations`, checks for prior outbound email to this sender. `neverError: true`.
4. **Filter: Our Thread Only** — Code node. If Supabase returns empty array → `return []` (drops). If rows exist → passes `Extract & Filter` output downstream.
5. **MCO: Fetch Context** — `POST /mco-fetch-context` with `lead_email`, `requesting_channel: 'email'`
   - Returns full lead record + last 20 cross-channel conversations as `context_block`
6. **Merge Context** — Code node, merges parsed email fields with `context_block`, `overall_intent`, `lead_name` from Supabase lead record.
7. **MCO: Write Inbound** — `POST /mco-write-event`, logs the inbound email to Supabase (channel: `email`, direction: `inbound`)
8. **Text Classifier** — Classifies the email intent using Anthropic.
   - Output 0: should reply → `Reply Agent`
   - Output 1: no action → `No Operation, do nothing`
9. **Reply Agent** — Anthropic `claude-sonnet-4-20250514`, generates email reply body.
   - Prompt: lead name + subject + email body + cross-channel history
   - System message: use context to understand intent, keep reply focused on this email thread, do NOT reference other channels
   - Max 120 words, no formatting
10. **Edit Fields** — prepares reply data
11. **Send a message** — Slack notification (human review / approval step)
12. **Switch** — routes based on approval outcome
13. **Send Gmail Reply** — Gmail node, sends reply in the same thread
14. **MCO: Write Outbound** — `POST /mco-write-event`, logs the sent reply to Supabase (channel: `email`, direction: `outbound`)

## Cross-Channel Context Usage

The AI receives the full MCO history (LinkedIn, email, voice) but is instructed to:
- Use it to understand the lead's situation and intent level
- **Not** mention other channels in the email reply
- Keep the reply focused on the email thread

This means if a lead called us last week but is now replying to an email, the AI knows they're warm — but the reply reads as a natural continuation of the email thread, not a reference to the call.

## Key Design Notes

- **Gate is Supabase leads table:** any email address (FROM or TO) that exists in the `leads` table passes through. The lead must have been added to Supabase by the Instantly workflow or any other upstream process before Gmail Reply Agent will act on it.
- **`neverError: true` on Supabase check:** if Supabase is down, the filter node receives an error response. `Filter: Our Thread Only` checks for a valid `lead_email` on the result — a non-object or missing `lead_email` returns `[]`, so the agent fails safe.
- **n8n single-item array unwrapping:** n8n's HTTP node unwraps single-item Supabase arrays `[{...}]` into plain objects `{...}`. `Filter: Our Thread Only` handles both: `Array.isArray(rows) ? rows[0] : (rows?.lead_email ? rows : null)`.
- **Instantly AI leads:** Set Reply-To = `team@flowticsai.com` in every Instantly campaign. Gmail trigger picks up those replies automatically.
- **Gmail header casing bug (fixed 2026-05-18):** The n8n Gmail trigger outputs header names with capital letters (`From`, `Subject`) not lowercase. The original `Extract & Filter` code used `email.from` and `email.subject` (lowercase), which were always `undefined`. This caused every email — including real lead replies — to be silently dropped at the `!leadEmail` check. Fixed by reading `email.from || email.From` and `email.subject || email.Subject`. Without this fix the workflow never processed any lead reply.

## Error Handling

| Failure | Effect |
|---|---|
| Gmail OAuth expired | Trigger stops firing; no emails processed |
| Supabase check fails | `Filter: Our Thread Only` returns `[]` — email dropped (fail safe) |
| MCO: Fetch Context fails | `context_block` falls back to `'(No prior conversation history.)'` |
| Anthropic API error | `Reply Agent` fails; no reply sent |
| Slack approval step denied | `Switch` routes away from `Send Gmail Reply` |
