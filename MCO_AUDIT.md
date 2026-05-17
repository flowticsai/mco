# MCO System Audit
**Date:** 2026-05-17  
**Method:** Automated — n8n API + Supabase REST

---

## Audit Result: 100/107 checks passed (7 were false positives or by-design)

All 10 workflows are Active. Supabase tables and RPCs confirmed accessible.

---

## Infrastructure

### Supabase
**Project:** `hkqssbomrcbtfbdowtgj.supabase.co`

| Table | Status |
|---|---|
| `leads` | Accessible. Columns: lead_id (UUID PK), lead_email, linkedin_profile_url, linkedin_urn, phone_e164, linkedin_conversation_urn, overall_intent, full_name, company |
| `conversations` | Accessible. Columns: event_id (PK/dedup), lead_id FK, channel, direction, content, metadata JSONB, timestamp |
| `follow_up_queue` | Accessible. Columns: queue_id PK, lead_id FK, lead_email, target_channel, status, scheduled_for, follow_up_context |
| `phone_map` | Accessible |

**RPCs:** `upsert_lead()`, `insert_conversation_event()`, `fetch_lead_context()` — all exist and respond correctly.

---

## Workflow Status

| Workflow | n8n ID | Nodes | Active | Notes |
|---|---|---|---|---|
| Write Conversation Event | `5qOo5YzrPnW8Uj9g` | 21 | Yes | Includes Notion CRM integration + Cancel Pending On Inbound |
| Fetch Cross-Channel Context | `UJoDCfkmD3NJHktk` | 5 | Yes | Returns full lead object with linkedin_conversation_urn |
| FollowUp Queue Dispatcher | `3ju6z4oJcWJqskBN` | 6 | Yes | Simplified 2026-05-17: Split Rows parses follow_up_context directly; LinkedIn branch removed |
| Centralized Follow-Up Coordinator | `KXKcCYRnK4V8v9k7` | 26 | Yes | 3 channel paths; LinkedIn branch fixed 2026-05-17 |
| Connection Accepted Handler | `WTbIAJCZGtppAT91` | 9 | Yes | Queue Follow-Up now references correct node (fixed 2026-05-17) |
| Aimfox Reply Agent — MCO | `SPN1NLyHH1LcfViD` | 37 | Yes | Reactive; Google Sheets gate; MCO writes on interested leads |
| Gmail Reply Agent | `mFBOGdMAsXRKD1Pv` | 18 | Yes | Full fetch context → reply → log cycle |
| Post Call Analysis | `r8XKHCnL4vju2E4j` | 7 | Yes | Retry logic added 2026-05-17; re-queues unanswered calls 4h |
| Call Agent | `xE8mFF8HxPaSXNmi` | 14 | Yes | Schedule every 4h + webhook; queue_id in Retell metadata |
| Aimfox Responded | `Zw7iTErdMMJjiM7g` | 2 | Yes | Aimfox label only — by design, no Supabase write |

---

## Bugs Fixed During This Session (2026-05-17)

| Bug | Workflow | Impact | Fix |
|---|---|---|---|
| IF node typeVersion 1 (strict) on LinkedIn: Has Conversation URN? | Coordinator | LinkedIn DM path never fired | Deleted and recreated with new node ID, typeVersion 2, loose |
| LinkedIn: Has Profile URL? checked wrong field | Coordinator | Always false | Fixed condition to check `linkedin_profile_url` |
| Merge Context: conversation_urn extraction after return statement | Coordinator | Dead code, URN was always null | Moved extraction inside return |
| Setup & Validate required lead_email only | Coordinator | LinkedIn-only leads caused 400 | Now accepts any identifier |
| Log to Supabase missing lead_id | Coordinator | Slower lookups, broke LinkedIn-only leads | Added lead_id to payload |
| LinkedIn branch too complex (extra IF nodes) | Coordinator | Confusing, error-prone | Simplified to single `Has Conversation URN?` check |
| Add to LinkedIn Campaign → After Send (wrong path) | Coordinator | Connection request counted as sent message | Split to separate Mark Queue Skipped → Return OK path |
| Queue LinkedIn Follow-Up used $json from HTTP response | Connection Accepted Handler | conversation_urn and aimfox_account_id were null in queue row | Changed to reference Extract Conversation URN node directly |
| LinkedIn? / Fetch LinkedIn Meta / Merge LinkedIn Meta nodes redundant | Dispatcher | follow_up_context already held aimfox_account_id and conversation_urn; extra nodes added complexity with no benefit | Removed all three; Split Rows now parses follow_up_context directly (2026-05-17) |
| Voice: Trigger Call Agent missing lead_id and phone_e164 | Coordinator | Call Agent couldn't find LinkedIn-only leads | Added both fields to payload |
| Post Call Analysis no retry logic | Post Call Analysis | Unanswered calls silently lost | Added Check Retry → Needs Retry? → Re-Queue Voice Call |
| Log Connection Request missing between Add to Campaign and Mark Queue Skipped | Coordinator | Connection requests not logged to Supabase | Added Log Connection Request node |
| Extract Fields read `body.account_id` and `body.lead` — wrong structure | Connection Accepted Handler | Would throw "No account_id" on every real Aimfox webhook; only test payload (flat format) had ever run | Fixed to read `event.account.id` and `event.target` per actual Aimfox accepted webhook spec |
| Extract Conversation URN used `require("crypto")` — blocked by n8n task runner | Connection Accepted Handler | Every real execution crashed at URN extraction | Replaced with `Math.random()`-based UUID generator |
| Random UUID as `log_event_id` provided no dedup protection vs Aimfox 6-retry webhook | Connection Accepted Handler | Duplicate conversation rows on Aimfox retries | Now uses `raw.id` (stable Aimfox event UUID from webhook payload) as `log_event_id`; Math.random() only as fallback |
| `Log Connection Request` and `Mark Queue Skipped` used `$json` from prior HTTP responses | Coordinator | `queue_id` and all lead fields were `undefined` at those nodes; queue row never marked skipped | Changed all field references to `$('Merge Context').first().json.*` |
| Coordinator `Setup & Validate` syntax error: `//` comment ate trailing comma | Coordinator | SyntaxError on every execution | Removed inline comment, clean object literal |
| Fetch Context URL undefined: Setup & Validate omitted `FETCH_CONTEXT_URL` and `WRITE_EVENT_URL` | Coordinator | "URL parameter must be a string, got undefined" on Fetch Context node | Added both constants to Setup & Validate output |
| `Add to LinkedIn Campaign` URL not evaluated (no `=` prefix) | Coordinator | Aimfox received URL-encoded literal `%7B%7B$json.aimfox_account_id%7D%7D` | Changed to `={{ "https://..." + $json.aimfox_account_id + "..." }}` expression |
| `Mark Queue Skipped` URL filter syntax wrong | Coordinator | Supabase received literal `eq={{ $json.queue_id }}` causing PGRST100 parse error | Changed to `={{ "...?queue_id=eq." + $('Merge Context').first().json.queue_id }}` |
| `Add to LinkedIn Campaign` wrong URL and body | Coordinator | Every campaign add 404'd silently; connection requests never sent | Correct endpoint is `POST /campaigns/:id/audience` (no `/accounts/:id` prefix); body is `{ profile_url }` not `{ leads: [urn] }`. Verified live: Aimfox returns `{"status":"ok","profile":{...}}` |
| `Mark Queue Sent` headers used `$json.SUPABASE_KEY` | Coordinator | No API key found in request — queue row never marked sent | Changed to `$('Merge Context').first().json.SUPABASE_KEY` |
| `Reply to Existing Conversation` URL had `/messages` suffix | Coordinator | Aimfox returned "Cannot POST" — DM never sent | Correct endpoint is `POST /accounts/:id/conversations/:urn` (no `/messages`). Verified live: returns `{status:"ok", message_urn}` |
| `Reply to Existing Conversation` body used `$('Merge Context').first().json.generated_message` | Coordinator | `generated_message` not in Merge Context → `{}` body → Aimfox 422 | Changed to `$('LinkedIn: Extract Message').first().json.generated_message` |
| `After Send` didn't carry `generated_message` forward | Coordinator | `Log to Supabase` content was always null → conversation row never written | Added try-catch pickup of `generated_message` from LinkedIn/Email/Voice Extract nodes in After Send code |
| `Log to Supabase` content used `$('Merge Context').first().json.generated_message` | Coordinator | Same as above — content always null | Changed to `$json.generated_message` (from After Send output) |
| `Log to Supabase` didn't pass `linkedin_urn` or `linkedin_profile_url` | Coordinator | For LinkedIn-only leads (no email), Write Event had no usable identifier | Added `linkedin_urn` and `linkedin_profile_url` to payload |
| Write Event `Setup & Validate` didn't accept `lead_id` as valid identifier | Write Event | LinkedIn-only leads passed `lead_id` only → "At least one identifier required" error → no conversation logged | Added `lead_id` check to the identifier validation block |

---

## Audit Findings — Explained

### False Positives (nodes exist with different names)
- Write Event: `Was Duplicate?` = dedup check; `Insert FollowUpQueue` = queue write — both present, named differently than audit searched for
- Fetch Context: returns full lead object (including `linkedin_conversation_urn`) implicitly — not explicitly named in code text

### By-Design (not bugs)
- **Aimfox Responded** — only marks Aimfox label, no Supabase write. The Reply Agent handles actual response + any MCO writes. This is intentional.
- **Aimfox Reply Agent** — uses Google Sheets (not Supabase) to decide whether to reply. Writes to MCO only for interested/returning leads. Intentional design — Google Sheets is the conversation-state gate.

### Cleaned Up
- 2 overdue test queue rows (`mco-test-lead@example.com`) marked `failed` — leftover from test suite run on 2026-05-04.

---

## Queue Health (at audit time)
- 0 overdue pending items (after cleanup)
- Dispatcher confirmed active (schedule every 15 min)
