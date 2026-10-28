"""
sync_passport.py - upload embroidery machine logs to Supabase, incrementally.

Every *.MDF in the data folder is one machine (machine name = file name).
For each machine the script remembers the last record it uploaded and, on the
next run, sends only the unfinished last record again plus everything newer.

Usage:
    py sync_passport.py                     one sync run (use with Task Scheduler)
    py sync_passport.py --dry-run           read files, show what would be sent, upload nothing
    py sync_passport.py --data-dir PATH     use another folder than config.json says
    py sync_passport.py --machine NAME      only this machine
    py sync_passport.py --full              ignore the watermark, re-send every row
    py sync_passport.py --loop              run forever, every interval_minutes
    py sync_passport.py --check             test folder and Supabase settings, upload nothing
    py sync_passport.py -v                  verbose log
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import logging
import logging.handlers
import os
import shutil
import sys
import time
from pathlib import Path

from mdf_reader import MdfReader

# Folder of the program: next to the .exe when packaged, next to the .py otherwise.
HERE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
# Relative paths in config.json are relative to the folder that holds config.json.
BASE = HERE


def default_config_path() -> Path:
    for folder in (HERE, HERE.parent):
        if (folder / "config.json").exists():
            return folder / "config.json"
    return HERE / "config.json"
SOURCE_TABLE = "passport"
PAGE_SIZE = 8192

# columns of the machine's passport table, in upload order
COL_NAMES = [
    "recordno", "state", "boxsrnr", "reasonid", "datestart", "dateend", "designno",
    "operator", "headno", "needleno", "headsrun", "strokes", "stitches", "numberx",
    "aheadmask1", "aheadmask2", "thrdbrku1", "thrdbrkl1", "thrdbrku2", "thrdbrkl2",
    "errcode", "reservedi", "reservedg",
]

log = logging.getLogger("sync_passport")


class SkipMachine(Exception):
    """The file cannot be used this run (half copied, suspicious); try again next run."""


# ----------------------------------------------------------------- config
def _resolve(path_value: str) -> Path:
    p = Path(path_value)
    return p if p.is_absolute() else (BASE / p)


def load_config(path: Path) -> dict:
    global BASE
    if not path.exists():
        raise SystemExit(f"settings file not found: {path}\nRun Setup first.")
    BASE = path.resolve().parent
    with open(path, "r", encoding="utf-8-sig") as f:
        cfg = json.load(f)
    cfg.setdefault("data_dir", "Data")
    cfg.setdefault("file_pattern", "*.MDF")
    cfg.setdefault("exclude", [])
    cfg.setdefault("overlap_rows", 5)
    cfg.setdefault("staging_dir", "staging")
    cfg.setdefault("keep_copies", 2)
    cfg.setdefault("copy_retries", 3)
    cfg.setdefault("copy_retry_seconds", 20)
    cfg.setdefault("interval_minutes", 60)
    cfg.setdefault("log_dir", "logs")
    sb = cfg.setdefault("supabase", {})
    sb["url"] = os.environ.get("SUPABASE_URL") or sb.get("url", "")
    sb["key"] = os.environ.get("SUPABASE_KEY") or sb.get("key", "")
    sb.setdefault("table", "passport_log")
    sb.setdefault("state_table", "passport_sync_state")
    sb.setdefault("run_table", "passport_sync_run")
    sb.setdefault("batch_size", 1000)
    return cfg


def setup_logging(log_dir: Path, verbose: bool):
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        log_dir / "sync_passport.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.handlers.clear()
    log.addHandler(fh)
    log.addHandler(ch)


# -------------------------------------------------------------- discovery
def data_folder(cfg: dict, args) -> Path:
    if args.data_dir:
        return Path(args.data_dir)
    if not str(cfg.get("data_dir") or "").strip():
        raise SystemExit("data folder is not set - open Setup and choose the folder with the .MDF files")
    return _resolve(cfg["data_dir"])


def discover_machines(data_dir: Path, pattern: str, exclude: list[str]) -> list[tuple[str, Path]]:
    """Every file matching the pattern in the (flat) data folder is one machine."""
    if not data_dir.is_dir():
        raise SystemExit(f"data folder not found: {data_dir}")
    found = []
    for p in sorted(data_dir.iterdir()):
        if not p.is_file() or not fnmatch.fnmatch(p.name.upper(), pattern.upper()):
            continue
        if any(fnmatch.fnmatch(p.name.upper(), x.upper()) or fnmatch.fnmatch(p.stem.upper(), x.upper())
               for x in exclude):
            continue
        found.append((p.stem, p))
    return found


# ------------------------------------------------------------------- copy
def file_md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def stage_copy(name: str, src: Path, cfg: dict) -> Path:
    """Copy the file to local staging and make sure it was not caught half written."""
    stage = _resolve(cfg["staging_dir"]) / name
    stage.mkdir(parents=True, exist_ok=True)
    dst = stage / f"{name}_{dt.datetime.now():%Y%m%d_%H%M%S}.MDF"
    last_err = None
    for attempt in range(1, cfg["copy_retries"] + 1):
        try:
            size_before = src.stat().st_size
            shutil.copy2(src, dst)
            size_after = src.stat().st_size
            copied = dst.stat().st_size
            if size_before != size_after or copied != size_after:
                raise OSError(f"size changed during copy ({size_before} -> {size_after}, copied {copied})")
            if copied == 0 or copied % PAGE_SIZE != 0:
                raise OSError(f"size {copied} is not a whole number of 8 KB pages (file still being written?)")
            last_err = None
            break
        except OSError as e:
            last_err = e
            log.warning("[%s] copy attempt %d/%d failed: %s", name, attempt, cfg["copy_retries"], e)
            dst.unlink(missing_ok=True)
            if attempt < cfg["copy_retries"]:
                time.sleep(cfg["copy_retry_seconds"])
    if last_err is not None:
        raise SkipMachine(f"could not get a clean copy of {src.name}: {last_err}")
    return dst


def prune_copies(name: str, cfg: dict, drop: Path | None = None):
    stage = _resolve(cfg["staging_dir"]) / name
    if drop is not None:
        drop.unlink(missing_ok=True)
    copies = sorted(stage.glob("*.MDF"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in copies[max(0, int(cfg["keep_copies"])):]:
        try:
            old.unlink()
        except OSError as e:
            log.warning("could not delete old copy %s: %s", old, e)


# ------------------------------------------------------------------ parse
def read_rows(mdf_path: Path) -> list[dict]:
    try:
        reader = MdfReader(str(mdf_path))
        table = reader.table(SOURCE_TABLE)
    except StopIteration:
        raise SkipMachine(f"{mdf_path.name}: no '{SOURCE_TABLE}' table found (not a machine log or incomplete file)")
    except Exception as e:
        raise SkipMachine(f"{mdf_path.name}: cannot parse file ({e!r})")
    have = {c.name for c in table.columns}
    missing = [c for c in COL_NAMES if c not in have]
    if missing:
        raise SkipMachine(f"{mdf_path.name}: passport table is missing columns {missing}")
    try:
        rows = [r for r in reader.rows(table) if r.get("recordno") is not None and r.get("datestart") is not None]
    except Exception as e:
        raise SkipMachine(f"{mdf_path.name}: cannot read rows ({e!r})")
    rows.sort(key=lambda r: (r["recordno"], r["datestart"]))
    return rows


# ------------------------------------------------------- incremental plan
def parse_ts(v):
    if v is None or isinstance(v, dt.datetime):
        return v
    return dt.datetime.fromisoformat(str(v)).replace(tzinfo=None)


def plan_upload(rows: list[dict], state: dict | None, overlap: int, force_full: bool):
    """Decide which rows to send.

    Returns (mode, rows_to_send):
      FULL         no watermark yet (new machine) or --full
      INCREMENTAL  rows from the watermark on (the unfinished record again + newer ones)
      RESET        the machine cleared its log; send the whole new file, keep old history
    Raises SkipMachine when the file looks older/incomplete compared to what was uploaded.
    """
    if not rows:
        raise SkipMachine("file contains no log rows")
    if force_full or not state or state.get("last_recordno") is None:
        return "FULL", rows

    wm_rec = int(state["last_recordno"])
    wm_start = parse_ts(state.get("last_datestart"))
    keys = {(r["recordno"], r["datestart"]) for r in rows}

    if (wm_rec, wm_start) in keys:
        first = wm_rec - max(0, int(overlap))
        return "INCREMENTAL", [r for r in rows if r["recordno"] >= first]

    # The record we uploaded last is not in this file any more.
    newest = max(r["datestart"] for r in rows)
    if wm_start is None or newest > wm_start:
        return "RESET", rows
    raise SkipMachine(
        f"file ends at record {rows[-1]['recordno']} ({newest}) but record {wm_rec} ({wm_start}) "
        f"was already uploaded - file looks older or incomplete, skipped")


def mark_open(rows_all: list[dict], rows_send: list[dict]) -> list[dict]:
    """Add is_open: true only for the machine's newest record (the interval still running)."""
    last = rows_all[-1]
    last_key = (last["recordno"], last["datestart"])
    return [{**r, "is_open": (r["recordno"], r["datestart"]) == last_key} for r in rows_send]


# --------------------------------------------------------------- supabase
def to_json(v):
    if isinstance(v, dt.datetime):
        return v.isoformat(sep="T", timespec="seconds")
    if isinstance(v, (bytes, bytearray)):
        return None
    return v


class SupabaseBackend:
    """Talks to Supabase through its REST API using the secret (service role) key."""

    def __init__(self, cfg: dict):
        sb = cfg["supabase"]
        if not sb["url"] or not sb["key"] or "YOUR-" in sb["url"] or "YOUR-" in sb["key"]:
            raise SystemExit("config.json: fill in supabase.url and supabase.key "
                             "(or set SUPABASE_URL / SUPABASE_KEY environment variables)")
        from supabase import create_client
        self.table, self.state_table, self.run_table = sb["table"], sb["state_table"], sb["run_table"]
        self.batch = int(sb["batch_size"])
        self.client = create_client(sb["url"], sb["key"])

    def check_schema(self):
        checks = [(self.table, "recordno,is_open"), (self.state_table, "machine_name,last_recordno,file_hash"),
                  (self.run_table, "run_id,mode,rows_sent")]
        for table, cols in checks:
            try:
                self.client.table(table).select(cols).limit(1).execute()
            except Exception as e:
                raise SystemExit(
                    f"Supabase table '{table}' is missing or out of date ({getattr(e, 'message', e)}).\n"
                    f"Open Supabase > SQL Editor, paste supabase_schema.sql, run it, then start the sync again.")

    def get_state(self, machine: str) -> dict | None:
        r = self.client.table(self.state_table).select("*").eq("machine_name", machine).limit(1).execute()
        return r.data[0] if r.data else None

    def count(self, machine: str) -> int:
        r = (self.client.table(self.table).select("recordno", count="exact")
             .eq("machine_name", machine).limit(1).execute())
        return int(r.count or 0)

    def upsert_rows(self, machine: str, rows: list[dict]):
        payload = [{"machine_name": machine, "is_open": bool(r.get("is_open")),
                    **{c: to_json(r.get(c)) for c in COL_NAMES}} for r in rows]
        for i in range(0, len(payload), self.batch):
            self.client.table(self.table).upsert(
                payload[i:i + self.batch], on_conflict="machine_name,recordno,datestart",
                returning="minimal").execute()
            log.debug("[%s] sent %d/%d", machine, min(i + self.batch, len(payload)), len(payload))

    def close_open_except(self, machine: str, recordno: int, datestart: dt.datetime):
        """Only the newest record may stay flagged as open."""
        t = self.client.table(self.table)
        (t.update({"is_open": False}, returning="minimal").eq("machine_name", machine)
         .eq("is_open", True).neq("recordno", recordno).execute())
        (self.client.table(self.table).update({"is_open": False}, returning="minimal")
         .eq("machine_name", machine).eq("is_open", True).eq("recordno", recordno)
         .neq("datestart", to_json(datestart)).execute())

    def save_state(self, machine: str, state: dict):
        row = {"machine_name": machine, **{k: to_json(v) for k, v in state.items()}}
        self.client.table(self.state_table).upsert(row, on_conflict="machine_name", returning="minimal").execute()

    def record_run(self, run: dict):
        try:
            self.client.table(self.run_table).insert(
                {k: to_json(v) for k, v in run.items()}, returning="minimal").execute()
        except Exception as e:  # bookkeeping must never break the sync
            log.warning("could not write run log: %s", e)


# ------------------------------------------------------------------- sync
def now_tz() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def sync_machine(name: str, src: Path, cfg: dict, backend, args) -> str:
    """Returns the status: OK, UNCHANGED, SKIPPED or ERROR."""
    started = now_tz()
    run = {"machine_name": name, "started_at": started, "status": "ERROR", "mode": None, "source_rows": None,
           "rows_sent": None, "inserted": None, "updated": None, "max_recordno": None, "message": None}
    copy = None
    try:
        copy = stage_copy(name, src, cfg)
        digest = file_md5(copy)
        state = backend.get_state(name) if backend else None

        if state and state.get("file_hash") == digest and not args.full:
            log.info("[%s] file unchanged since last sync, nothing to do", name)
            run.update(status="UNCHANGED", mode="NONE", rows_sent=0, inserted=0, updated=0,
                       source_rows=state.get("row_count"), max_recordno=state.get("last_recordno"))
            prune_copies(name, cfg, drop=copy)
            return "UNCHANGED"

        rows = read_rows(copy)
        mode, to_send = plan_upload(rows, state, cfg["overlap_rows"], args.full)
        last = rows[-1]
        run.update(mode=mode, source_rows=len(rows), rows_sent=len(to_send), max_recordno=last["recordno"])
        log.info("[%s] %d rows in file (record %d..%d, newest %s) -> %s, sending %d rows",
                 name, len(rows), rows[0]["recordno"], last["recordno"], last["datestart"], mode, len(to_send))
        if mode == "RESET":
            log.warning("[%s] machine log was reset (last uploaded record %s not in file); "
                        "uploading the new log, old history is kept", name, state.get("last_recordno"))

        if args.dry_run:
            r0, r1 = to_send[0], to_send[-1]
            log.info("[%s] dry-run: would send record %d (%s) .. %d (%s)", name,
                     r0["recordno"], r0["datestart"], r1["recordno"], r1["datestart"])
            run.update(status="OK")
            prune_copies(name, cfg, drop=copy)
            return "OK"

        before = backend.count(name)
        backend.upsert_rows(name, mark_open(rows, to_send))
        backend.close_open_except(name, last["recordno"], last["datestart"])
        inserted = backend.count(name) - before
        backend.save_state(name, {
            "last_recordno": last["recordno"], "last_datestart": last["datestart"],
            "file_hash": digest, "file_size": copy.stat().st_size, "row_count": len(rows),
            "source_path": str(src), "last_sync_at": now_tz(),
        })
        run.update(status="OK", inserted=inserted, updated=len(to_send) - inserted)
        log.info("[%s] done: %d new rows, %d existing rows refreshed", name, inserted, len(to_send) - inserted)
        prune_copies(name, cfg)
        return "OK"
    except SkipMachine as e:
        log.warning("[%s] skipped: %s", name, e)
        run.update(status="SKIPPED", message=str(e)[:2000])
        if copy is not None:
            prune_copies(name, cfg, drop=copy)
        return "SKIPPED"
    except Exception as e:
        log.exception("[%s] sync failed: %s", name, e)
        run.update(status="ERROR", message=str(e)[:2000])
        return "ERROR"
    finally:
        run["finished_at"] = now_tz()
        if backend is not None and not args.dry_run:
            backend.record_run(run)


def run_once(cfg: dict, args, backend=None) -> bool:
    data_dir = data_folder(cfg, args)
    machines = discover_machines(data_dir, cfg["file_pattern"], cfg["exclude"])
    if args.machine:
        machines = [m for m in machines if m[0].lower() == args.machine.lower()]
    log.info("data folder %s: %d machine file(s): %s", data_dir, len(machines), ", ".join(m[0] for m in machines))
    if not machines:
        log.error("no machine files found")
        return False

    if backend is None:
        try:
            backend = SupabaseBackend(cfg)
            backend.check_schema()
        except SystemExit:
            if not args.dry_run:
                raise
            log.warning("Supabase not reachable/ready; dry-run continues without watermarks (shows a full upload)")
            backend = None

    results = {name: sync_machine(name, src, cfg, backend, args) for name, src in machines}
    summary = ", ".join(f"{s}: {sum(1 for v in results.values() if v == s)}"
                        for s in ("OK", "UNCHANGED", "SKIPPED", "ERROR") if s in results.values())
    log.info("summary - %s", summary)
    return "ERROR" not in results.values()


def run_check(cfg: dict, args) -> int:
    """Test the settings: data folder readable, machine files found, Supabase reachable."""
    ok = True
    try:
        data_dir = data_folder(cfg, args)
    except SystemExit as e:
        print(f"FAIL  {e}")
        data_dir, ok = None, False
    if data_dir is None:
        pass
    elif not data_dir.is_dir():
        print(f"FAIL  data folder not found or not reachable: {data_dir}")
        ok = False
    else:
        machines = discover_machines(data_dir, cfg["file_pattern"], cfg["exclude"])
        if machines:
            print(f"OK    data folder {data_dir}: {len(machines)} machine file(s)")
            for name, src in machines:
                try:
                    with open(src, "rb") as f:
                        f.read(PAGE_SIZE)
                    print(f"OK      {name}  ({src.stat().st_size // 1024} KB)")
                except OSError as e:
                    print(f"FAIL    {name}: cannot read file ({e})")
                    ok = False
        else:
            print(f"FAIL  data folder {data_dir} contains no {cfg['file_pattern']} files")
            ok = False
    try:
        backend = SupabaseBackend(cfg)
        backend.check_schema()
        print("OK    Supabase connection and tables")
        try:
            backend.client.table(backend.state_table).upsert(
                {"machine_name": "__setup_test__"}, on_conflict="machine_name", returning="minimal").execute()
            backend.client.table(backend.state_table).delete().eq("machine_name", "__setup_test__").execute()
            print("OK    Supabase key can write")
        except Exception as e:
            print(f"FAIL  Supabase key cannot write - use the secret key (sb_secret_...), not the publishable one ({getattr(e, 'message', e)})")
            ok = False
    except SystemExit as e:
        print(f"FAIL  {e}")
        ok = False
    except Exception as e:
        print(f"FAIL  Supabase not reachable: {e}")
        ok = False
    print("RESULT: " + ("all checks passed" if ok else "some checks failed"))
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(default_config_path()))
    ap.add_argument("--check", action="store_true", help="test the settings and exit, upload nothing")
    ap.add_argument("--data-dir", help="folder with the machines' .MDF files (overrides config.json)")
    ap.add_argument("--machine", help="sync only this machine name")
    ap.add_argument("--dry-run", action="store_true", help="read and plan only, upload nothing")
    ap.add_argument("--full", action="store_true", help="ignore the watermark and re-send every row")
    ap.add_argument("--loop", action="store_true", help="keep running every interval_minutes")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config(Path(args.config))
    if args.check:
        return run_check(cfg, args)
    setup_logging(_resolve(cfg["log_dir"]), args.verbose)
    log.info("=== sync start (%s) ===", "dry-run" if args.dry_run else "live")

    if not args.loop:
        ok = run_once(cfg, args)
        log.info("=== sync %s ===", "finished" if ok else "finished WITH ERRORS")
        return 0 if ok else 1

    interval = max(1, int(cfg["interval_minutes"])) * 60
    while True:
        try:
            run_once(cfg, args)
        except BaseException as e:
            if isinstance(e, KeyboardInterrupt):
                raise
            log.exception("run failed: %s", e)
        log.info("sleeping %d minutes", interval // 60)
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
