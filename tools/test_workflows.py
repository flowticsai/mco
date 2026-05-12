"""
MCO Workflow Test Script
Run: python tools/test_workflows.py
"""
import json, time, uuid, requests, sys, io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

env_path = Path(__file__).parent.parent / ".env"
env = {}
with open(env_path) as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()

WRITE_URL = env.get("MCO_WRITE_EVENT_WEBHOOK")
FETCH_URL = env.get("MCO_FETCH_CONTEXT_WEBHOOK")
SUPA_URL  = env.get("SUPABASE_URL", "").rstrip("/")
SUPA_KEY  = env.get("SUPABASE_SERVICE_ROLE_KEY", "")
SB_HEADERS = {
    "apikey":        SUPA_KEY,
    "Authorization": f"Bearer {SUPA_KEY}",
    "Content-Type":  "application/json"
}
TEST_EMAIL = "mco-test-lead@example.com"

# Fresh UUIDs each run — required because conversations.event_id is UUID PRIMARY KEY
EVT_A = str(uuid.uuid4())   # linkedin inbound (basic write)
EVT_B = str(uuid.uuid4())   # email outbound
EVT_C = str(uuid.uuid4())   # cross-channel trigger (booking)
EVT_D = str(uuid.uuid4())   # intent demotion attempt

passed = failed = 0

def check(label, ok, detail=""):
    global passed, failed
    if ok:
        print(f"  [PASS] {label}")
        passed += 1
    else:
        print(f"  [FAIL] {label}" + (f"  => {detail}" if detail else ""))
        failed += 1

def sb_get(table, params):
    r = requests.get(f"{SUPA_URL}/{table}", headers={**SB_HEADERS, "Accept": "application/json"}, params=params, timeout=10)
    return r.json() if isinstance(r.json(), list) else []

# ── Clean up previous test data ───────────────────────────────────────────────
print("\n--- Cleanup: removing previous test lead ---")
requests.delete(f"{SUPA_URL}/follow_up_queue", headers=SB_HEADERS,
    params={"lead_email": f"eq.{TEST_EMAIL}"}, timeout=10)
requests.delete(f"{SUPA_URL}/conversations", headers=SB_HEADERS,
    params={"lead_email": f"eq.{TEST_EMAIL}"}, timeout=10)
requests.delete(f"{SUPA_URL}/leads", headers=SB_HEADERS,
    params={"lead_email": f"eq.{TEST_EMAIL}"}, timeout=10)
print("  Done")

# ── Test 1: Write basic event ──────────────────────────────────────────────────
print(f"\n--- Test 1: Write basic event (event_id={EVT_A[:8]}...) ---")
r = requests.post(WRITE_URL, json={
    "event_id":   EVT_A,
    "lead_email": TEST_EMAIL,
    "channel":    "linkedin",
    "direction":  "inbound",
    "content":    "Hi, saw your message. Looks interesting!",
    "timestamp":  "2026-05-04T10:00:00Z",
    "full_name":  "MCO Test Lead",
    "company":    "Test Corp",
    "intent":     "interested"
}, timeout=30)
data = r.json() if r.headers.get("content-type","").startswith("application/json") else {}
check("HTTP 200",              r.status_code == 200, r.status_code)
check("status = ok",           data.get("status") == "ok", data)
check("monday_item_id present",bool(data.get("monday_item_id")), data)

# Verify conversation row was actually written
time.sleep(1)
rows = sb_get("conversations", {"event_id": f"eq.{EVT_A}"})
check("conversation row in Supabase", len(rows) == 1, f"got {len(rows)} rows")

# ── Test 2: Duplicate event is idempotent ──────────────────────────────────────
print(f"\n--- Test 2: Same event_id again — must return already_written ---")
r2 = requests.post(WRITE_URL, json={
    "event_id":   EVT_A,        # same UUID as Test 1
    "lead_email": TEST_EMAIL,
    "channel":    "linkedin",
    "direction":  "inbound",
    "content":    "Hi, saw your message. Looks interesting!",
    "timestamp":  "2026-05-04T10:00:00Z",
    "intent":     "interested"
}, timeout=30)
data2 = r2.json() if r2.headers.get("content-type","").startswith("application/json") else {}
check("HTTP 200",                r2.status_code == 200, r2.status_code)
check("status = already_written", data2.get("status") == "already_written", data2)

# ── Test 3: Second event on different channel ──────────────────────────────────
print(f"\n--- Test 3: Second event on email channel ---")
r3 = requests.post(WRITE_URL, json={
    "event_id":   EVT_B,
    "lead_email": TEST_EMAIL,
    "channel":    "email",
    "direction":  "outbound",
    "content":    "Following up from our LinkedIn chat. Free for a quick call?",
    "timestamp":  "2026-05-04T11:00:00Z",
    "intent":     "no_action"
}, timeout=30)
data3 = r3.json() if r3.headers.get("content-type","").startswith("application/json") else {}
check("HTTP 200",    r3.status_code == 200, r3.status_code)
check("status = ok", data3.get("status") == "ok", data3)

# ── Test 4: Cross-channel trigger ─────────────────────────────────────────────
print(f"\n--- Test 4: Cross-channel trigger (booking intent, target=[email,sms]) ---")
r4 = requests.post(WRITE_URL, json={
    "event_id":              EVT_C,
    "lead_email":            TEST_EMAIL,
    "channel":               "linkedin",
    "direction":             "inbound",
    "content":               "Yes I want to book a demo next week!",
    "timestamp":             "2026-05-04T12:00:00Z",
    "intent":                "booking",
    "trigger_cross_channel": True,
    "target_channels":       ["email", "sms"],
    "follow_up_context":     "Lead wants to book a demo"
}, timeout=30)
data4 = r4.json() if r4.headers.get("content-type","").startswith("application/json") else {}
check("HTTP 200",    r4.status_code == 200, r4.status_code)
check("status = ok", data4.get("status") == "ok", data4)

time.sleep(2)
queue_rows = sb_get("follow_up_queue", {"lead_email": f"eq.{TEST_EMAIL}", "order": "created_at.desc", "limit": "5"})
check("follow_up_queue has 2 rows", len(queue_rows) >= 2, f"got {len(queue_rows)} rows")
if queue_rows:
    channels = [row.get("target_channel") for row in queue_rows]
    check("email queued", "email" in channels, channels)
    check("sms queued",   "sms"   in channels, channels)
    statuses = [row.get("status") for row in queue_rows]
    check("status = pending", all(s == "pending" for s in statuses), statuses)

# ── Test 5: Intent demotion prevention ────────────────────────────────────────
print(f"\n--- Test 5: Write 'not_interested' after 'booking' — must not demote ---")
r5 = requests.post(WRITE_URL, json={
    "event_id":   EVT_D,
    "lead_email": TEST_EMAIL,
    "channel":    "email",
    "direction":  "inbound",
    "content":    "Actually never mind.",
    "timestamp":  "2026-05-04T13:00:00Z",
    "intent":     "not_interested"
}, timeout=30)
check("HTTP 200", r5.status_code == 200, r5.status_code)
time.sleep(1)
leads = sb_get("leads", {"lead_email": f"eq.{TEST_EMAIL}", "limit": "1"})
current_intent = leads[0].get("overall_intent") if leads else "unknown"
check("overall_intent stayed 'booking'", current_intent == "booking", f"got '{current_intent}'")

# ── Test 6: Fetch cross-channel context ───────────────────────────────────────
print(f"\n--- Test 6: Fetch cross-channel context ---")
fr = requests.post(FETCH_URL, json={
    "lead_email":          TEST_EMAIL,
    "requesting_channel":  "email",
    "max_events":          20
}, timeout=30)
fdata = fr.json() if fr.headers.get("content-type","").startswith("application/json") else {}
check("HTTP 200",                   fr.status_code == 200, fr.status_code)
check("context_block present",      bool(fdata.get("context_block")), fdata)
check("event_count >= 3",           fdata.get("event_count", 0) >= 3, fdata.get("event_count"))
check("overall_intent = booking",   fdata.get("overall_intent") == "booking", fdata.get("overall_intent"))
cb = fdata.get("context_block", "")
check("[LINKEDIN] in context",      "[LINKEDIN]" in cb)
check("[EMAIL] in context",         "[EMAIL]"    in cb)
check("channel header in context",  "EMAIL" in cb[:200])
if cb:
    print("\n  --- Context Block Preview (first 15 lines) ---")
    for line in cb.split("\n")[:15]:
        print("  " + line)
    print("  ...")

# ── Test 7: Unknown lead returns gracefully ────────────────────────────────────
print(f"\n--- Test 7: Fetch context for unknown lead ---")
ur = requests.post(FETCH_URL, json={"lead_email": "nobody-test@nowhere.com"}, timeout=30)
udata = ur.json() if ur.headers.get("content-type","").startswith("application/json") else {}
check("HTTP 200",         ur.status_code == 200, ur.status_code)
check("lead is null",     udata.get("lead") is None, udata.get("lead"))
check("event_count = 0",  udata.get("event_count") == 0, udata.get("event_count"))

# ── Summary ────────────────────────────────────────────────────────────────────
print(f"\n{'='*45}")
print(f"  Results: {passed} passed, {failed} failed")
if failed == 0:
    print("  All tests passed!")
else:
    print("  Some tests failed — check output above.")
print('='*45)
