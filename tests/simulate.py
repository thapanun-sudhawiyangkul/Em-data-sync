"""Replay day 23 -> day 24 -> day 24 again -> edge cases against an in-memory backend."""
import sys, shutil, argparse, logging, tempfile, datetime as dt
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sync_passport as sp

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="embroi_sim_"))


class MemoryBackend:
    def __init__(self):
        self.rows, self.state, self.runs, self.sent = {}, {}, [], 0

    def get_state(self, m):
        s = self.state.get(m)
        return None if s is None else {k: sp.to_json(v) for k, v in s.items()}   # strings, like the REST API

    def count(self, m):
        return sum(1 for k in self.rows if k[0] == m)

    def upsert_rows(self, m, rows):
        self.sent += len(rows)
        for r in rows:
            self.rows[(m, r["recordno"], r["datestart"])] = dict(r)

    def close_open_except(self, m, recordno, datestart):
        for k, r in self.rows.items():
            if k[0] == m and r.get("is_open") and (k[1], k[2]) != (recordno, datestart):
                r["is_open"] = False

    def save_state(self, m, s):
        self.state[m] = dict(s)

    def record_run(self, run):
        self.runs.append(dict(run))


cfg = sp.load_config(ROOT / "config.json")
cfg["staging_dir"] = str(TMP / "staging")
cfg["copy_retries"], cfg["copy_retry_seconds"] = 1, 0
sp.setup_logging(TMP / "logs", False)
logging.getLogger("sync_passport").handlers[:] = [h for h in sp.log.handlers if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)]

be = MemoryBackend()
# Seed: Supabase already holds machine 1260 at its day-23 state, with no watermark and no is_open.
for r in sp.read_rows(ROOT / "Data" / "23" / "TFGN-620--1260.MDF"):
    be.rows[("TFGN-620--1260", r["recordno"], r["datestart"])] = {**r, "is_open": False}


def run(label, data_dir, **kw):
    a = argparse.Namespace(data_dir=str(data_dir), machine=None, dry_run=False, full=False, loop=False, verbose=False)
    for k, v in kw.items():
        setattr(a, k, v)
    be.sent, n0 = 0, len(be.runs)
    print(f"\n##### {label}")
    ok = sp.run_once(cfg, a, backend=be)
    for r in be.runs[n0:]:
        print(f"   {r['machine_name']:16} {r['status']:9} mode={r['mode']} sent={r['rows_sent']} "
              f"inserted={r['inserted']} refreshed={r['updated']} msg={r['message']}")
    print("   total rows in DB:", {m: be.count(m) for m in sorted({k[0] for k in be.rows})},
          "| open rows:", sorted((k[0], k[1]) for k, r in be.rows.items() if r.get("is_open")))
    return ok


def verify(day):
    """DB must equal the file content exactly (all columns) for every machine."""
    for p in sorted((ROOT / "Data" / day).glob("*.MDF")):
        rows = sp.read_rows(p)
        bad = [r["recordno"] for r in rows
               if {c: be.rows.get((p.stem, r["recordno"], r["datestart"]), {}).get(c) for c in sp.COL_NAMES}
               != {c: r[c] for c in sp.COL_NAMES}]
        extra = be.count(p.stem) - len(rows)
        print(f"   verify {p.stem} vs day {day}: mismatching rows={len(bad)} extra rows in DB={extra}")


run("1. first run on day 23 (1260 already in DB without watermark, two new machines)", ROOT / "Data" / "23")
verify("23")
run("2. same folder again (files unchanged)", ROOT / "Data" / "23")
run("3. day 24 files (the real incremental case)", ROOT / "Data" / "24")
verify("24")
run("4. day 24 again", ROOT / "Data" / "24")

# 5. NAS delivers an OLDER file again (day 23 after day 24 was uploaded) -> must be skipped
run("5. older file shows up after a newer one was uploaded", ROOT / "Data" / "23")
verify("24")

# 6. half-copied file: truncate a day-24 file in a temp folder
half = TMP / "half"; half.mkdir()
src = ROOT / "Data" / "24" / "TFGN-915--5008.MDF"
data = src.read_bytes()
(half / src.name).write_bytes(data[: len(data) // 2 + 1234])          # not a multiple of 8 KB
run("6a. half-copied file, ragged size", half)
(half / src.name).write_bytes(data[: (len(data) // 2 // 8192) * 8192])  # cut on a page boundary
run("6b. half-copied file, cut exactly on a page boundary", half)
verify("24")

# 7. machine log reset: build a fake 'after reset' state by pretending the DB watermark is from an older generation
be.state["TFGN-620--2301"]["last_recordno"] = 9000
be.state["TFGN-620--2301"]["last_datestart"] = dt.datetime(2026, 1, 5, 8, 0, 0)
be.state["TFGN-620--2301"]["file_hash"] = "old"
be.rows[("TFGN-620--2301", 9000, dt.datetime(2026, 1, 5, 8, 0, 0))] = {"recordno": 9000, "datestart": dt.datetime(2026, 1, 5, 8, 0, 0), "is_open": True}
run("7. machine 2301 was reset (watermark record 9000 from January not in file)", ROOT / "Data" / "24", machine="TFGN-620--2301")
print("   old-generation row kept:", ("TFGN-620--2301", 9000, dt.datetime(2026, 1, 5, 8, 0, 0)) in be.rows,
      "| its is_open now:", be.rows[("TFGN-620--2301", 9000, dt.datetime(2026, 1, 5, 8, 0, 0))]["is_open"])

# 8. a non-machine file in the folder
junk = TMP / "junk"; junk.mkdir()
(junk / "NOTES.MDF").write_bytes(b"\0" * 8192 * 4)
shutil.copy2(ROOT / "Data" / "24" / "TFGN-620--1260.MDF", junk / "TFGN-620--1260.MDF")
run("8. folder with a junk .MDF next to a good one", junk)

shutil.rmtree(TMP, ignore_errors=True)
