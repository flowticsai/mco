"""
Monday → Notion cutover: remove Monday from MCO workflows.

USAGE:
  python tools/kill_monday.py            # dry-run (default): prints plan, no changes
  python tools/kill_monday.py --apply    # actually performs the cutover

WHAT IT DOES:
  1. Backs up both target workflows to .tmp/kill_monday_backup_<ts>_*.json
  2. MCO - Write Conversation Event:
     - Removes 5 Monday nodes (Format/Create/Extract/Has/Post Monday Update)
     - Rewires: Upsert PhoneMap → Notion: Query Lead (was → Format Monday Update)
                Has Phone? [no] → Notion: Query Lead (was → Format Monday Update)
  3. Aimfox Nextus AI Reply Agent — MCO:
     - Removes every node with type 'n8n-nodes-base.mondayCom'
     - Rewires each removed node's predecessors → its successors (skipping over Monday)

SAFETY:
  - Dry-run by default. Backups always taken even on --apply.
  - Idempotent: if Monday is already gone from a workflow, it's a no-op for that one.
  - On any PUT failure, attempts to restore from backup.

REVERT:
  python tools/kill_monday.py --restore .tmp/kill_monday_backup_<ts>_<workflow>.json
"""
import json, sys, io, argparse, datetime, requests
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

ROOT = Path(__file__).parent.parent
TMP  = ROOT / '.tmp'
TMP.mkdir(exist_ok=True)

env = {}
for line in open(ROOT / '.env', encoding='utf-8'):
    s = line.strip()
    if s and not s.startswith('#') and '=' in s:
        k, v = s.split('=', 1); env[k.strip()] = v.strip()

N8N  = env['N8N_BASE_URL'].rstrip('/')
H    = {'X-N8N-API-KEY': env['N8N_API_KEY'], 'Content-Type': 'application/json'}
TS   = datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%S')

ALLOWED_PUT_KEYS = {'name', 'nodes', 'connections', 'settings'}
ALLOWED_SETTINGS_KEYS = {'executionOrder','saveDataErrorExecution','saveDataSuccessExecution',
                       'saveManualExecutions','callerPolicy','errorWorkflow','timezone'}

WRITE_EVENT_MONDAY_NODES = [
    'Format Monday Update',
    'Create Monday Item',
    'Extract Monday ID',
    'Has Monday Item?',
    'Post Monday Update',
]

def strip_for_put(w):
    w = {k: v for k, v in w.items() if k in ALLOWED_PUT_KEYS}
    if 'settings' in w and isinstance(w['settings'], dict):
        w['settings'] = {k: v for k, v in w['settings'].items() if k in ALLOWED_SETTINGS_KEYS}
    return w


def get_workflow_by_name_part(name_part):
    r = requests.get(f'{N8N}/api/v1/workflows?limit=100', headers=H, timeout=15)
    matches = [w for w in r.json()['data'] if name_part in w['name']]
    if not matches:
        raise RuntimeError(f'No workflow matching "{name_part}"')
    return matches[0]['id'], matches[0]['name']


def fetch(wid):
    return requests.get(f'{N8N}/api/v1/workflows/{wid}', headers=H, timeout=15).json()


def backup(wf, tag):
    p = TMP / f'kill_monday_backup_{TS}_{tag}.json'
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(wf, f, indent=2, ensure_ascii=False)
    return p


def find_predecessors(connections, target_name):
    """Return list of (src_name, output_idx) that connect into target_name."""
    out = []
    for src, conns in connections.items():
        for output_idx, links in enumerate(conns.get('main', [])):
            for link in links:
                if link.get('node') == target_name:
                    out.append((src, output_idx))
    return out


def remove_node_and_rewire(wf, node_name):
    """Remove a node + connect its predecessors directly to its successors.
    Dedupes the resulting links so we don't end up with src -> succ twice
    when multiple removed nodes inherit the same successor."""
    nodes = wf['nodes']
    conns = wf['connections']
    if not any(n['name'] == node_name for n in nodes):
        return False  # already removed
    # Find successors (this node's outgoing connections — flatten across outputs)
    succs = []
    for links in conns.get(node_name, {}).get('main', []):
        for link in links:
            succs.append(link)  # preserve the link object
    # Find predecessors
    preds = find_predecessors(conns, node_name)
    # Rewire: each predecessor (src, output_idx) -> append each successor to src.main[output_idx]
    for src, output_idx in preds:
        src_conns = conns.setdefault(src, {}).setdefault('main', [])
        while len(src_conns) <= output_idx:
            src_conns.append([])
        # Remove the link that pointed at node_name
        src_conns[output_idx] = [l for l in src_conns[output_idx] if l.get('node') != node_name]
        # Add the successors — but dedupe by (node, type, index)
        existing_keys = {(l.get('node'), l.get('type','main'), l.get('index',0)) for l in src_conns[output_idx]}
        for succ in succs:
            k = (succ.get('node'), succ.get('type','main'), succ.get('index',0))
            if k in existing_keys:
                continue
            existing_keys.add(k)
            src_conns[output_idx].append(succ)
    # Drop the node and its outgoing connections
    wf['nodes'] = [n for n in nodes if n['name'] != node_name]
    conns.pop(node_name, None)
    return True


def plan_write_event(wf):
    """Plan changes to Write Event. Returns (changes_summary, modified_wf)."""
    summary = []
    # Special rewiring needed:
    #   Has Phone? [main:0]   -> ... → Format Monday Update.  Want: -> Notion: Query Lead
    #   Has Phone? [main:1]   -> Upsert PhoneMap → Format Monday Update.
    #   Upsert PhoneMap [main:0] -> Format Monday Update. Want: -> Notion: Query Lead
    # Since we remove Format Monday Update (and friends) and rewire predecessors to successors,
    # and Post Monday Update's successor IS Notion: Query Lead, the standard rewire works.
    for nm in WRITE_EVENT_MONDAY_NODES:
        if any(n['name'] == nm for n in wf['nodes']):
            summary.append(f'  - Remove "{nm}" and rewire neighbors')
        else:
            summary.append(f'  - "{nm}" already absent')
    # Apply
    for nm in WRITE_EVENT_MONDAY_NODES:
        remove_node_and_rewire(wf, nm)
    return summary, wf


def plan_aimfox(wf):
    summary = []
    monday_nodes = [n['name'] for n in wf['nodes'] if n.get('type') == 'n8n-nodes-base.mondayCom']
    if not monday_nodes:
        summary.append('  - No mondayCom nodes found (already cleaned)')
    for nm in monday_nodes:
        summary.append(f'  - Remove mondayCom node "{nm}"')
        remove_node_and_rewire(wf, nm)
    return summary, wf


def push(wid, wf):
    requests.post(f'{N8N}/api/v1/workflows/{wid}/deactivate', headers=H, timeout=15)
    wf2 = strip_for_put(wf)
    r = requests.put(f'{N8N}/api/v1/workflows/{wid}', headers=H, json=wf2, timeout=30)
    if r.status_code >= 400:
        return False, r.text[:500]
    requests.post(f'{N8N}/api/v1/workflows/{wid}/activate', headers=H, timeout=15)
    return True, 'OK'


def restore(path):
    bak = json.load(open(path, encoding='utf-8'))
    name = bak.get('name', '')
    wid, _ = get_workflow_by_name_part(name)
    ok, msg = push(wid, bak)
    print(f'Restore {name}: {msg}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='Actually apply the cutover')
    ap.add_argument('--restore', help='Path to a kill_monday_backup_*.json to restore')
    args = ap.parse_args()

    if args.restore:
        restore(args.restore)
        return

    print('=' * 60)
    print('MONDAY → NOTION CUTOVER')
    print('Mode:', 'APPLY (live changes)' if args.apply else 'DRY-RUN (no changes)')
    print('=' * 60)

    for name_part, tag in [('Write Conversation', 'write_event'),
                           ('Aimfox Nextus AI Reply', 'aimfox_reply')]:
        wid, name = get_workflow_by_name_part(name_part)
        nm = name.encode('ascii', 'replace').decode('ascii')
        print(f'\n[{nm}]  id={wid}')
        wf = fetch(wid)
        if args.apply:
            backup_path = backup(wf, tag)
            print(f'  Backup: {backup_path}')
        # Plan + apply on the in-memory copy
        if 'Write Conversation' in name:
            summary, wf2 = plan_write_event(wf)
        else:
            summary, wf2 = plan_aimfox(wf)
        for line in summary:
            print(line)
        if not args.apply:
            continue
        ok, msg = push(wid, wf2)
        print(f'  PUT: {msg}')
        if not ok:
            print('  RESTORING FROM BACKUP...')
            bak = json.load(open(backup_path, encoding='utf-8'))
            push(wid, bak)
            print('  Restored.')
            sys.exit(1)

    if not args.apply:
        print('\n(dry-run complete. Re-run with --apply to execute.)')
    else:
        print('\nDone. Monday is gone from MCO.')


if __name__ == '__main__':
    main()
