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
