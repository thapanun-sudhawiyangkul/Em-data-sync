import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sync_passport as sp
from supabase import create_client

ROOT = Path(__file__).resolve().parents[1]
c = json.load(open(ROOT / "config.json"))["supabase"]
sb = create_client(c["url"], c["key"])

for p in sorted((ROOT / "Data" / "24").glob("*.MDF")):
    m = p.stem
    db, start = [], 0
    while True:
        r = (sb.table("passport_log").select("*").eq("machine_name", m)
             .order("recordno").order("datestart").range(start, start + 999).execute())
        db += r.data
        if len(r.data) < 1000:
            break
        start += 1000
    file_rows = {(r["recordno"], sp.to_json(r["datestart"])): {k: sp.to_json(r[k]) for k in sp.COL_NAMES}
                 for r in sp.read_rows(p)}
    db_rows = {(r["recordno"], r["datestart"]): {k: r[k] for k in sp.COL_NAMES} for r in db}
    mismatch = [k for k in file_rows if db_rows.get(k) != file_rows[k]]
    extra = set(db_rows) - set(file_rows)
    opens = [(r["recordno"], r["dateend"]) for r in db if r["is_open"]]
    touched = [(r["recordno"], r["dateend"], r["strokes"]) for r in db if r["updated_at"]]
    print(f"{m}: file={len(file_rows)} db={len(db_rows)} mismatching={len(mismatch)} extra_in_db={len(extra)}")
    print(f"    is_open rows: {opens}")
    print(f"    rows whose values changed since first upload: {touched}")

print("\nstate:")
for r in sb.table("passport_sync_state").select("machine_name,last_recordno,last_datestart,row_count,file_size,last_sync_at").order("machine_name").execute().data:
    print("   ", r)
print("\nmachine status view:")
for r in sb.table("passport_machine_status").select("*").order("machine_name").execute().data:
    print("   ", r)
print("\nrun log (this session):")
for r in sb.table("passport_sync_run").select("run_id,machine_name,status,mode,source_rows,rows_sent,inserted,updated").order("run_id", desc=True).limit(12).execute().data[::-1]:
    print("   ", r)
print("\ndaily view, 2026-09-23:")
for r in sb.table("passport_daily_design").select("*").eq("day", "2026-09-23").order("machine_name").execute().data:
    print("   ", r)
