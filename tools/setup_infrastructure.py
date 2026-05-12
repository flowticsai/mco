"""
MCO Infrastructure Setup Script
Runs the one-time setup for n8n workflows, credentials, and Monday.com columns.
Usage: python tools/setup_infrastructure.py
"""
import os
import json
import requests
import sys
import io
from pathlib import Path

# Force UTF-8 output on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ── Load .env ─────────────────────────────────────────────────────────────────
env_path = Path(__file__).parent.parent / ".env"
env = {}
with open(env_path) as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()

SUPABASE_URL     = env.get("SUPABASE_URL", "").rstrip("/").replace("/rest/v1", "")
SUPABASE_KEY     = env.get("SUPABASE_SERVICE_ROLE_KEY", "")
N8N_BASE         = env.get("N8N_BASE_URL", "").rstrip("/")
N8N_API_KEY      = env.get("N8N_API_KEY", "")
MONDAY_TOKEN     = env.get("MONDAY_API_TOKEN", "")
MONDAY_BOARD_ID  = env.get("MONDAY_BOARD_ID", "18399476470")

# ── Helpers ───────────────────────────────────────────────────────────────────
def ok(msg):   print(f"  [OK] {msg}")
def fail(msg): print(f"  [FAIL] {msg}"); sys.exit(1)
def info(msg): print(f"  [..] {msg}")

def n8n_headers():
    return {"X-N8N-API-KEY": N8N_API_KEY, "Content-Type": "application/json"}

def monday_gql(query):
    r = requests.post(
        "https://api.monday.com/v2",
        headers={"Authorization": MONDAY_TOKEN, "Content-Type": "application/json"},
        json={"query": query},
        timeout=15
    )
    r.raise_for_status()
    return r.json()

# ==============================================================================
# STEP 1: Test connections
# ==============================================================================
print("\n--- STEP 1: Testing connections ---")

# Test n8n
try:
    r = requests.get(f"{N8N_BASE}/api/v1/workflows?limit=1", headers=n8n_headers(), timeout=10)
    r.raise_for_status()
    ok(f"n8n API connected ({N8N_BASE})")
except Exception as e:
    fail(f"n8n API failed: {e}")

# Test Monday.com
try:
    result = monday_gql("{ me { name } }")
    name = result.get("data", {}).get("me", {}).get("name", "unknown")
    ok(f"Monday.com API connected (logged in as: {name})")
except Exception as e:
    fail(f"Monday.com API failed: {e}")

# Test Supabase REST
try:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/",
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
        timeout=10
    )
    ok(f"Supabase reachable ({SUPABASE_URL})")
except Exception as e:
    info(f"Supabase REST check inconclusive (normal if tables don't exist yet): {e}")

# ==============================================================================
# STEP 2: Create Supabase credential in n8n
# ==============================================================================
print("\n--- STEP 2: Creating Supabase credential in n8n ---")

# Check if credential already exists; clean up duplicates
supabase_cred_id = None
try:
    r = requests.get(f"{N8N_BASE}/api/v1/credentials?limit=100", headers=n8n_headers(), timeout=10)
    resp = r.json()
    existing = resp.get("data", resp if isinstance(resp, list) else [])
    matches = [c for c in existing if isinstance(c, dict) and c.get("name") == "MCO Supabase"]
    if matches:
        # Keep first, delete duplicates
        supabase_cred_id = matches[0]["id"]
        info(f"Credential 'MCO Supabase' already exists (id: {supabase_cred_id}), skipping creation")
        for dup in matches[1:]:
            try:
                requests.delete(f"{N8N_BASE}/api/v1/credentials/{dup['id']}", headers=n8n_headers(), timeout=10)
                info(f"Removed duplicate credential (id: {dup['id']})")
            except:
                pass
except Exception as e:
    info(f"Could not list credentials: {e}")

if not supabase_cred_id:
    credential_payload = {
        "name": "MCO Supabase",
        "type": "supabaseApi",
        "data": {
            "host": SUPABASE_URL,
            "serviceRole": SUPABASE_KEY
        }
    }
    try:
        r = requests.post(
            f"{N8N_BASE}/api/v1/credentials",
            headers=n8n_headers(),
            json=credential_payload,
            timeout=15
        )
        r.raise_for_status()
        result = r.json()
        supabase_cred_id = result.get("id") or result.get("data", {}).get("id")
        ok(f"Created 'MCO Supabase' credential in n8n (id: {supabase_cred_id})")
    except Exception as e:
        info(f"Could not create credential via API (may need to do manually): {e}")
        info("You can create it manually: n8n → Credentials → New → Supabase → name 'MCO Supabase'")
        supabase_cred_id = "MCO_SUPABASE_CREDENTIAL_ID"  # placeholder

# ==============================================================================
# STEP 3: Import n8n workflows
# ==============================================================================
print("\n--- STEP 3: Importing n8n workflows ---")

workflows_dir = Path(__file__).parent.parent / "n8n_workflows"
workflow_files = [
    "MCO_Write_Conversation_Event.json",
    "MCO_Fetch_Cross_Channel_Context.json",
]

imported_workflow_ids = {}

# Get existing workflows to check for duplicates
try:
    r = requests.get(f"{N8N_BASE}/api/v1/workflows?limit=100", headers=n8n_headers(), timeout=10)
    existing_workflows = {w["name"]: w["id"] for w in r.json().get("data", [])}
except:
    existing_workflows = {}

for wf_file in workflow_files:
    wf_path = workflows_dir / wf_file
    if not wf_path.exists():
        fail(f"Workflow file not found: {wf_path}")

    with open(wf_path) as f:
        wf_data = json.load(f)

    wf_name = wf_data["name"]

    # Swap placeholder credential ID with real one
    wf_str = json.dumps(wf_data)
    wf_str = wf_str.replace("MCO_SUPABASE_CREDENTIAL_ID", str(supabase_cred_id))
    wf_data = json.loads(wf_str)

    # n8n API only accepts these fields on creation; strip everything else
    wf_payload = {
        "name": wf_data["name"],
        "nodes": wf_data["nodes"],
        "connections": wf_data["connections"],
        "settings": wf_data.get("settings", {}),
        "staticData": wf_data.get("staticData"),
    }
    wf_data = wf_payload

    if wf_name in existing_workflows:
        wid = existing_workflows[wf_name]
        info(f"'{wf_name}' already exists (id: {wid}), updating...")
        try:
            # Update existing
            r = requests.put(
                f"{N8N_BASE}/api/v1/workflows/{wid}",
                headers=n8n_headers(),
                json=wf_data,
                timeout=15
            )
            r.raise_for_status()
            imported_workflow_ids[wf_name] = wid
            ok(f"Updated '{wf_name}' (id: {wid})")
        except Exception as e:
            info(f"Could not update '{wf_name}': {e}")
    else:
        try:
            r = requests.post(
                f"{N8N_BASE}/api/v1/workflows",
                headers=n8n_headers(),
                json=wf_data,
                timeout=15
            )
            r.raise_for_status()
            result = r.json()
            wid = result.get("id") or result.get("data", {}).get("id")
            imported_workflow_ids[wf_name] = wid
            ok(f"Imported '{wf_name}' (id: {wid})")
        except Exception as e:
            info(f"Could not import '{wf_name}': {e}")
            try:
                info(f"n8n error detail: {r.text[:500]}")
            except:
                pass
            info(f"Import manually: n8n > Workflows > Import from File > {wf_file}")

# Activate imported workflows
print("\n  Activating workflows...")
for wf_name, wid in imported_workflow_ids.items():
    try:
        r = requests.post(
            f"{N8N_BASE}/api/v1/workflows/{wid}/activate",
            headers=n8n_headers(),
            timeout=10
        )
        r.raise_for_status()
        ok(f"Activated '{wf_name}'")
    except Exception as e:
        info(f"Could not auto-activate '{wf_name}' — activate manually in n8n: {e}")

# Fetch webhook URLs
print("\n  Fetching webhook URLs...")
for wf_name, wid in imported_workflow_ids.items():
    try:
        r = requests.get(f"{N8N_BASE}/api/v1/workflows/{wid}", headers=n8n_headers(), timeout=10)
        wf = r.json()
        nodes = wf.get("nodes", [])
        for node in nodes:
            if node.get("type") == "n8n-nodes-base.webhook":
                path = node.get("parameters", {}).get("path", "")
                webhook_url = f"{N8N_BASE}/webhook/{path}"
                ok(f"Webhook URL for '{wf_name}': {webhook_url}")
    except Exception as e:
        info(f"Could not fetch webhook URL for '{wf_name}': {e}")

# ==============================================================================
# STEP 4: Add columns to Monday.com board
# ==============================================================================
print("\n--- STEP 4: Adding columns to Monday.com board ---")

board_id = int(MONDAY_BOARD_ID)

# First check what columns already exist
try:
    result = monday_gql(f"""{{
        boards(ids: [{board_id}]) {{
            columns {{ id title type }}
        }}
    }}""")
    existing_cols = {
        col["title"]: col["id"]
        for col in result.get("data", {}).get("boards", [{}])[0].get("columns", [])
    }
    info(f"Board has {len(existing_cols)} existing columns")
except Exception as e:
    fail(f"Could not fetch Monday.com board columns: {e}")

# Columns to add
new_columns = [
    {"title": "last_active_channel", "column_type": "text"},
    {"title": "first_channel",       "column_type": "text"},
    {"title": "phone_e164",           "column_type": "text"},
    {"title": "overall_intent",       "column_type": "status"},  # status = Status column
]

for col in new_columns:
    if col["title"] in existing_cols:
        ok(f"Column '{col['title']}' already exists, skipping")
        continue
    try:
        mutation = f"""mutation {{
            create_column(
                board_id: {board_id},
                title: "{col['title']}",
                column_type: {col['column_type']}
            ) {{ id title }}
        }}"""
        result = monday_gql(mutation)
        errors = result.get("errors")
        if errors:
            info(f"Column '{col['title']}': {errors[0].get('message', errors)}")
        else:
            created = result.get("data", {}).get("create_column", {})
            ok(f"Created column '{col['title']}' (id: {created.get('id')})")
    except Exception as e:
        info(f"Could not create column '{col['title']}': {e}")

# Set labels for overall_intent status column
try:
    overall_intent_id = existing_cols.get("overall_intent") or "overall_intent"
    intent_labels = {
        "1": "unknown",
        "2": "no_action",
        "3": "not_interested",
        "4": "referral",
        "5": "already_have_contract",
        "6": "interested",
        "7": "booking"
    }
    labels_json = json.dumps(json.dumps({"labels": intent_labels}))
    mutation = f"""mutation {{
        change_column_metadata(
            board_id: {board_id},
            column_id: "overall_intent",
            column_property: labels,
            value: {labels_json}
        ) {{ id }}
    }}"""
    monday_gql(mutation)
    ok("Set intent labels on 'overall_intent' status column")
except Exception as e:
    info(f"Could not set status labels (set manually in Monday.com): {e}")

# ==============================================================================
# DONE
# ==============================================================================
print("""
=================================================
  Setup complete!

  ONE MANUAL STEP REMAINING -- Supabase SQL:
  1. Go to supabase.com/dashboard/project/hkqssbomrcbtfbdowtgj/sql/new
  2. Open tools/supabase_setup.sql from this folder
  3. Paste the entire file -> click RUN
  4. Confirm it shows 4 table names at the bottom
=================================================
""")
