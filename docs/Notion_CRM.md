# Notion CRM — Reference

## What it is

The Notion CRM is the **new pipeline-visible front end** for MCO, replacing Monday.com (board `18399476470`, scheduled for deletion). Supabase remains the canonical conversation store; Notion is the human-readable view that anyone in the workspace can browse, filter, and act on.

Migration cutover date: **2026-05-16**.

---

## Workspace structure

```
Notion: Flowtics AI's Workspace
└── 📄 MCO CRM                                  ← parent page (top-level)
    ├── 👤 Leads          (database)            ← one row per lead
    └── 💬 Conversations  (database, ↔ Leads)   ← one row per message, any channel
```

Notion IDs (also in `.env`):

| Resource | ID |
|---|---|
| MCO CRM parent page | `362dc227-748f-8071-8ccd-d5388d352b06` |
| Leads database | `362dc227-748f-8184-892a-c6f8f3151b07` |
| Conversations database | `362dc227-748f-817b-b406-cf43be6f4822` |
| Notion bot integration name | `MCO` |

---

## Database schemas

### 👤 Leads

| Property | Type | Notes |
|---|---|---|
| Name | title | full_name (page title) |
| Email | email | canonical lead key |
| Company | rich_text | COALESCE-filled on update |
| Phone | phone_number | E.164 only (validated upstream) |
| Overall intent | select | unknown · no_action · not_interested · referral · already_have_contract · interested · booking — never demotes (mirrors Supabase rule) |
| Last channel | select | email · linkedin · voice · sms |
| First channel | select | same options — set once at create, never updated |
| LinkedIn URL | url | COALESCE-filled |
| LinkedIn URN | rich_text | COALESCE-filled |
| Aimfox campaign | rich_text | COALESCE-filled |
| Sender inbox | email | COALESCE-filled |
| Monday item ID | rich_text | transition-only; will be empty after Monday cutover |
| Last activity | date | updated on every event |
| First seen | date | set once at lead creation |

### 💬 Conversations

| Property | Type | Notes |
|---|---|---|
| Event | title | format: `{Lead Name} · {channel} {direction} · {YYYY-MM-DD HH:MM}` |
| Lead | relation → Leads | **two-way (`dual_property`)** — the Leads DB shows linked conversations back |
| Channel | select | email · linkedin · voice · sms |
| Direction | select | inbound · outbound |
| Timestamp | date | when the event actually occurred (not when written) |
| Intent | select | event-level intent (lead-level intent lives on the Leads row) |
| Content | rich_text | first 1900 chars of the message |
| Content type | select | message · transcript · summary |
| Sender name | rich_text | who said it |
| Event ID | rich_text | UUID — same as Supabase `conversations.event_id` |
| Workflow exec ID | rich_text | n8n execution id (debugging) |

The full message content also lives in the conversation **page body** for readable browsing.

---

## How writes happen

Every channel agent in MCO calls **one webhook**:

```
POST https://n8n-1404.n8n.whiteserverdns.com/webhook/mco-write-event
```

That webhook is `MCO - Write Conversation Event` in n8n. Its dual-write writes to Supabase **+ Notion** (and during the transition, Monday too). The Notion branch is 6 nodes between the Monday path and the `Cross-Channel?` gate:

```
Notion: Query Lead              → POST databases/{leads}/query, filter Email=
   ↓
Notion: Extract Lead ID         → Code node. Parses query, builds COALESCE'd update_props
   ↓
IF: Notion Lead Found?
   │
   ├─[yes]→ Notion: Update Lead       → PATCH pages/{id}, body = {properties: update_props}
   │                                     Always updates: Overall intent / Last channel / Last activity
   │                                     Fills only-if-empty: Company / Phone / LinkedIn URL / URN / Aimfox campaign / Sender inbox
   │
   └─[no] → Notion: Create Lead       → POST pages, full lead record from event
   ↓
Notion: Create Conversation     → POST pages, with Lead relation + message-as-page-body
```

All Notion HTTP nodes use `Authorization: Bearer ntn_…` from the inline header (no shared n8n credential) and `Notion-Version: 2022-06-28`. They use `neverError: true` so a Notion outage degrades gracefully without failing the Supabase write.

### Intent source — important detail

`Notion: Extract Lead ID` reads the canonical intent from `$('Upsert Lead').item.json.overall_intent`, **not** from the event payload or from `Merge Lead Data`. This is because `Merge Lead Data` has a pre-existing bug that drops the RPC result. The Supabase `upsert_lead` RPC enforces the promotion-only rule (interested can't be demoted to no_action), and the Notion side mirrors that by reading from the RPC output directly.

---

## Why every channel agent is already covered

You only need to wire **one workflow** (Write Event) to Notion because every other MCO workflow funnels through that webhook:

| Workflow | How it reaches Notion |
|---|---|
| `Aimfox Nextus AI Reply Agent — MCO` | Code nodes `MCO: Write Returning` + `MCO: Write First Interested` `fetch()` to `/mco-write-event` |
| `MCO - Aimfox Connection Accepted Handler` | HTTP node `Log to Supabase` posts to `/mco-write-event` |
| `MCO - Centralized Follow-Up Coordinator` | HTTP node `Log to Supabase` posts to `/mco-write-event` |
| `MCO - Gmail Reply Agent` | HTTP nodes `MCO: Write Inbound` + `MCO: Write Outbound` post to `/mco-write-event` |
| `Flowtics AI Outbound agent-MCO` | HTTP node `POST /mco-write-event` |

The standalone `mondayCom` nodes inside `Aimfox Nextus AI Reply Agent — MCO` (3 of them) are pre-MCO legacy. They write extra updates to Monday but **don't** touch Supabase or Notion. The Monday-kill script (`tools/kill_monday.py`) removes them at cutover.

---

## Monday cutover

When you're ready to retire Monday:

```bash
python tools/kill_monday.py           # dry-run, shows the plan
python tools/kill_monday.py --apply   # makes the changes
```

This removes the 5 Monday nodes from Write Event and the 3 `mondayCom` nodes from Aimfox Reply Agent, rewiring neighbors safely. Backups land in `.tmp/kill_monday_backup_*_*.json` automatically. Reverting:

```bash
python tools/kill_monday.py --restore .tmp/kill_monday_backup_<ts>_write_event.json
```

After cutover, the `Monday item ID` column on the Leads DB is dead — you can hide it in views or remove the property via the Notion UI.

---

## Operational notes

- **Adding a property** in the future: do it via the Notion UI on the database. Then any new event whose Setup & Validate output exposes that field can be wired into `Notion: Create Lead` / `Notion: Extract Lead ID` (in the workflow). No DB recreate needed.
- **Adding a new intent** would mean: add the option to the select in Notion **and** add it to Supabase's `intent_rank()` function **and** add it to Setup & Validate's `validIntents` array. Three places.
- **Views** can be created via the Notion MCP server's `notion-create-view` tool (the older Notion *API* doesn't expose view creation, but the MCP does). As of 2026-05-16 the database has 4 views: `Default view` (table), `Pipeline (Kanban)` (board grouped by Overall intent), `Timeline` (First seen → Last activity), `Active leads` (filtered: Overall intent ∈ {interested, booking}).
- **Token rotation**: when the integration token rotates, update `NOTION_TOKEN` in `.env` **and** the inline bearer in the 6 Notion HTTP nodes inside Write Event. Easier path: re-run `.tmp/patch_*.py` scripts with the new token.

---

## Files involved

| File | Role |
|---|---|
| `.env` | `NOTION_TOKEN`, three `NOTION_*_ID` vars |
| `n8n_workflows/MCO_Write_Conversation_Event.json` | local snapshot of the dual-write workflow |
| `tools/kill_monday.py` | cutover script — removes Monday nodes when triggered |
| `.tmp/create_notion_crm.py` | one-shot DB creation script (already ran 2026-05-16) |
| `.tmp/patch_write_event_notion.py` | one-shot dual-write patch (already ran 2026-05-16) |
| `.tmp/write_event_backup_pre_notion.json` | pre-Notion backup of Write Event workflow |

See [MCO_Connections_Reference.md](MCO_Connections_Reference.md) for cross-system wiring and [MCO_System_Guide.md](MCO_System_Guide.md) for the plain-English overview.
