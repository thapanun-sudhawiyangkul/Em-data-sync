"""
sync_passport.py - copy the machine's Pulse Design Spooler report database
(<machine>.MDF) and upload new/changed rows of its "passport" log table into
your own SQL Server.

Usage:
    py sync_passport.py                     one sync run (use with Task Scheduler)
    py sync_passport.py --dry-run           copy + parse, print what would be uploaded
    py sync_passport.py --loop              run forever, every interval_minutes
    py sync_passport.py --machine NAME      only this machine from config
    py sync_passport.py --config other.json

The .MDF is read directly by mdf_reader.py, so nothing is ever attached to a
SQL Server. The target is chosen with "backend" in config.json:
    "supabase"  - upsert through the Supabase REST API (run supabase_schema.sql first)
    "sqlserver" - MERGE into your own SQL Server through pyodbc
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import logging.handlers
import os
import shutil
import sys
import time
from pathlib import Path

from mdf_reader import MdfReader

HERE = Path(__file__).resolve().parent
SOURCE_TABLE = "passport"

# Columns of the machine's passport table, in the order they are uploaded.
COLUMNS = [
    ("recordno", "int NOT NULL"),
    ("state", "int NULL"),
    ("boxsrnr", "int NULL"),
    ("reasonid", "int NULL"),
    ("datestart", "datetime NOT NULL"),
    ("dateend", "datetime NULL"),
    ("designno", "nvarchar(254) NULL"),
    ("operator", "int NULL"),
    ("headno", "int NULL"),
    ("needleno", "int NULL"),
    ("headsrun", "int NULL"),
    ("strokes", "int NULL"),
    ("stitches", "int NULL"),
    ("numberx", "int NULL"),
    ("aheadmask1", "int NULL"),
    ("aheadmask2", "int NULL"),
    ("thrdbrku1", "int NULL"),
    ("thrdbrkl1", "int NULL"),
    ("thrdbrku2", "int NULL"),
    ("thrdbrkl2", "int NULL"),
    ("errcode", "nvarchar(4) NULL"),
    ("reservedi", "int NULL"),
    ("reservedg", "int NULL"),
]
COL_NAMES = [c for c, _ in COLUMNS]
KEY_COLS = ["MachineName", "recordno", "datestart"]

log = logging.getLogger("sync_passport")


# ----------------------------------------------------------------- config
def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("staging_dir", str(HERE / "staging"))
    cfg.setdefault("keep_copies", 3)
    cfg.setdefault("copy_retries", 5)
    cfg.setdefault("copy_retry_seconds", 30)
    cfg.setdefault("interval_minutes", 60)
    cfg.setdefault("lookback_days", None)
    cfg.setdefault("target_table", "dbo.PassportLog")
    cfg.setdefault("run_table", "dbo.PassportSyncRun")
    cfg.setdefault("log_dir", str(HERE / "logs"))
    cfg.setdefault("backend", "supabase" if "supabase" in cfg else "sqlserver")
    if cfg["backend"] not in ("supabase", "sqlserver"):
        raise SystemExit("config: 'backend' must be 'supabase' or 'sqlserver'")
    if cfg["backend"] == "supabase":
        sb = cfg.setdefault("supabase", {})
        sb["url"] = os.environ.get("SUPABASE_URL") or sb.get("url", "")
        sb["key"] = os.environ.get("SUPABASE_KEY") or sb.get("key", "")
        sb.setdefault("table", "passport_log")
        sb.setdefault("run_table", "passport_sync_run")
        sb.setdefault("batch_size", 1000)
        if not sb["url"] or not sb["key"]:
            raise SystemExit("config: supabase.url and supabase.key are required "
                             "(or set SUPABASE_URL / SUPABASE_KEY environment variables)")
    if "machines" not in cfg or not cfg["machines"]:
        raise SystemExit("config: 'machines' list is empty")
    for m in cfg["machines"]:
        if "mdf" not in m:
            raise SystemExit("config: every machine needs an 'mdf' path")
        m.setdefault("name", Path(m["mdf"]).stem)
    return cfg


def setup_logging(log_dir: str, verbose: bool):
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        Path(log_dir) / "sync_passport.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.addHandler(fh)
    log.addHandler(ch)


# ------------------------------------------------------------------- copy
def copy_machine_files(machine: dict, cfg: dict) -> Path:
    """Copy <name>.MDF (and its .LDF if present) into staging/<name>/.
    Retries when the file is locked by the machine's own SQL Server."""
    src_mdf = Path(machine["mdf"])
    stage = Path(cfg["staging_dir"]) / machine["name"]
    stage.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    dst_mdf = stage / f"{machine['name']}_{stamp}.MDF"

    # the machine names its log <name>_log.LDF
    src_ldf = Path(machine.get("ldf") or src_mdf.with_name(src_mdf.stem + "_log.LDF"))

    last_err = None
    for attempt in range(1, cfg["copy_retries"] + 1):
        try:
            shutil.copy2(src_mdf, dst_mdf)
            last_err = None
            break
        except (PermissionError, OSError) as e:
            last_err = e
            log.warning("[%s] copy attempt %d/%d failed: %s", machine["name"], attempt, cfg["copy_retries"], e)
            time.sleep(cfg["copy_retry_seconds"])
    if last_err is not None:
        raise RuntimeError(f"could not copy {src_mdf}: {last_err}")
    log.info("[%s] copied %s (%d KB)", machine["name"], src_mdf, dst_mdf.stat().st_size // 1024)

    if src_ldf.exists():
        try:
            shutil.copy2(src_ldf, dst_mdf.with_name(f"{machine['name']}_{stamp}_log.LDF"))
        except OSError as e:
            log.warning("[%s] LDF not copied (not needed for parsing): %s", machine["name"], e)

    prune_copies(stage, cfg["keep_copies"])
    return dst_mdf


def prune_copies(stage: Path, keep: int):
    mdfs = sorted(stage.glob("*.MDF"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in mdfs[keep:]:
        for f in (old, old.with_name(old.stem + "_log.LDF")):
            try:
                f.unlink(missing_ok=True)
            except OSError as e:
                log.warning("could not delete old copy %s: %s", f, e)


# ------------------------------------------------------------------ parse
def read_rows(mdf_path: Path, lookback_days) -> list[dict]:
    reader = MdfReader(str(mdf_path))
    table = reader.table(SOURCE_TABLE)
    missing = [c for c in COL_NAMES if c not in {col.name for col in table.columns}]
    if missing:
        raise RuntimeError(f"{mdf_path.name}: passport table is missing columns {missing}")
    rows = [r for r in reader.rows(table) if r.get("recordno") is not None and r.get("datestart") is not None]
    if lookback_days and rows:
        newest = max((r["dateend"] or r["datestart"]) for r in rows)
        cutoff = newest - dt.timedelta(days=lookback_days)
        rows = [r for r in rows if (r["dateend"] or r["datestart"]) >= cutoff]
    rows.sort(key=lambda r: (r["recordno"], r["datestart"]))
    return rows


# --------------------------------------------------------------- supabase
class SupabaseBackend:
    """Uploads through the Supabase REST API (PostgREST) with the service_role key."""

    def __init__(self, cfg: dict):
        from supabase import create_client
        sb = cfg["supabase"]
        self.table = sb["table"]
        self.run_table = sb["run_table"]
        self.batch = int(sb["batch_size"])
        self.client = create_client(sb["url"], sb["key"])

    @staticmethod
    def _json(v):
        if isinstance(v, dt.datetime):
            return v.isoformat(sep="T", timespec="seconds")
        if isinstance(v, bytes):
            return None
        return v

    def ensure_schema(self):
        # Tables are created by supabase_schema.sql; here we only check they exist.
        try:
            self.client.table(self.table).select("recordno").limit(1).execute()
        except Exception as e:
            raise RuntimeError(f"table '{self.table}' not reachable in Supabase - run supabase_schema.sql first ({e})")

    def _count(self, machine_name: str) -> int:
        res = (self.client.table(self.table).select("recordno", count="exact")
               .eq("machine_name", machine_name).limit(1).execute())
        return int(res.count or 0)

    def upload(self, machine_name: str, rows: list[dict]) -> tuple[int, int]:
        before = self._count(machine_name)
        payload = [{"machine_name": machine_name, **{c: self._json(r.get(c)) for c in COL_NAMES}} for r in rows]
        for i in range(0, len(payload), self.batch):
            chunk = payload[i:i + self.batch]
            self.client.table(self.table).upsert(
                chunk, on_conflict="machine_name,recordno,datestart", returning="minimal").execute()
            log.debug("[%s] upserted %d/%d", machine_name, min(i + self.batch, len(payload)), len(payload))
        inserted = self._count(machine_name) - before
        # rows that already existed were sent as updates; the DB trigger stamps only real changes
        return inserted, len(payload) - inserted

    def record_run(self, machine_name, started, source_rows, inserted, updated, max_recordno, status, message):
        try:
            self.client.table(self.run_table).insert({
                "machine_name": machine_name,
                "started_at": started.astimezone().isoformat(timespec="seconds"),
                "finished_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                "source_rows": source_rows, "inserted": inserted, "updated": updated,
                "max_recordno": max_recordno, "status": status, "message": (message or "")[:2000] or None,
            }).execute()
        except Exception as e:
            log.warning("could not write run log: %s", e)

    def rollback(self):
        pass  # REST upserts commit per batch; nothing to roll back

    def close(self):
        pass


# -------------------------------------------------------------------- sql
def pick_driver(requested: str) -> str:
    import pyodbc
    installed = pyodbc.drivers()
    if requested and requested.lower() != "auto":
        if requested not in installed:
            raise RuntimeError(f"ODBC driver '{requested}' not installed. Installed: {installed}")
        return requested
    for pref in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server",
                 "ODBC Driver 13 for SQL Server", "SQL Server Native Client 11.0", "SQL Server"):
        if pref in installed:
            return pref
    raise RuntimeError(f"no SQL Server ODBC driver found. Installed: {installed}")


def connection_string(sql: dict, database: str | None = None) -> str:
    driver = pick_driver(sql.get("driver", "auto"))
    parts = [f"DRIVER={{{driver}}}", f"SERVER={sql['server']}", f"DATABASE={database or sql['database']}"]
    if sql.get("trusted_connection", True):
        parts.append("Trusted_Connection=yes")
    else:
        parts.append(f"UID={sql['username']}")
        parts.append(f"PWD={sql['password']}")
    if "ODBC Driver" in driver:
        parts.append(f"Encrypt={sql.get('encrypt', 'yes')}")
        if sql.get("trust_server_certificate", True):
            parts.append("TrustServerCertificate=yes")
    if sql.get("extra"):
        parts.append(sql["extra"])
    return ";".join(parts)


def connect(sql: dict):
    import pyodbc
    try:
        return pyodbc.connect(connection_string(sql), autocommit=False, timeout=30)
    except pyodbc.Error as e:
        # 4060 = cannot open database: create it if allowed, then retry
        if "4060" in str(e) and sql.get("create_database_if_missing", True):
            log.info("database %s not found, creating it", sql["database"])
            master = pyodbc.connect(connection_string(sql, "master"), autocommit=True, timeout=30)
            master.execute(f"IF DB_ID(?) IS NULL EXEC('CREATE DATABASE [{sql['database']}]')", sql["database"])
            master.close()
            return pyodbc.connect(connection_string(sql), autocommit=False, timeout=30)
        raise


def split_name(qualified: str):
    if "." in qualified:
        schema, name = qualified.split(".", 1)
    else:
        schema, name = "dbo", qualified
    return schema.strip("[]"), name.strip("[]")


def ensure_schema(conn, cfg: dict):
    schema, name = split_name(cfg["target_table"])
    cols = ",\n        ".join(f"[{c}] {t}" for c, t in COLUMNS)
    conn.execute(f"""
    IF OBJECT_ID(N'[{schema}].[{name}]', N'U') IS NULL
    CREATE TABLE [{schema}].[{name}] (
        [MachineName] nvarchar(100) NOT NULL,
        {cols},
        [LoadedAt]  datetime2(0) NOT NULL CONSTRAINT [DF_{name}_LoadedAt] DEFAULT SYSDATETIME(),
        [UpdatedAt] datetime2(0) NULL,
        CONSTRAINT [PK_{name}] PRIMARY KEY CLUSTERED ([MachineName], [recordno], [datestart])
    );""")
    rschema, rname = split_name(cfg["run_table"])
    conn.execute(f"""
    IF OBJECT_ID(N'[{rschema}].[{rname}]', N'U') IS NULL
    CREATE TABLE [{rschema}].[{rname}] (
        [RunId]        int IDENTITY(1,1) NOT NULL CONSTRAINT [PK_{rname}] PRIMARY KEY,
        [MachineName]  nvarchar(100) NOT NULL,
        [StartedAt]    datetime2(0) NOT NULL,
        [FinishedAt]   datetime2(0) NULL,
        [SourceRows]   int NULL,
        [Inserted]     int NULL,
        [Updated]      int NULL,
        [MaxRecordNo]  int NULL,
        [Status]       nvarchar(20) NOT NULL,
        [Message]      nvarchar(2000) NULL
    );""")
    conn.commit()


def upload_rows(conn, cfg: dict, machine_name: str, rows: list[dict]) -> tuple[int, int]:
    """Stage all rows in a temp table and MERGE them into the target.
    Returns (inserted, updated)."""
    schema, name = split_name(cfg["target_table"])
    target = f"[{schema}].[{name}]"
    cur = conn.cursor()
    cols_ddl = ",\n        ".join(f"[{c}] {t}" for c, t in COLUMNS)
    cur.execute(f"""
    CREATE TABLE #stage (
        [MachineName] nvarchar(100) NOT NULL,
        {cols_ddl}
    );""")

    placeholders = ",".join("?" for _ in range(len(COL_NAMES) + 1))
    insert_sql = f"INSERT INTO #stage ([MachineName],{','.join('[' + c + ']' for c in COL_NAMES)}) VALUES ({placeholders})"
    params = [[machine_name] + [row.get(c) for c in COL_NAMES] for row in rows]
    driver_supports_fast = "ODBC Driver" in conn.getinfo(6)  # SQL_DRIVER_NAME
    try:
        cur.fast_executemany = driver_supports_fast
        for i in range(0, len(params), 5000):
            cur.executemany(insert_sql, params[i:i + 5000])
    except Exception:
        if not driver_supports_fast:
            raise
        log.warning("fast_executemany failed, retrying row by row")
        cur.execute("TRUNCATE TABLE #stage")
        cur.fast_executemany = False
        cur.executemany(insert_sql, params)

    data_cols = [c for c in COL_NAMES if c not in ("recordno", "datestart")]
    set_clause = ", ".join(f"t.[{c}] = s.[{c}]" for c in data_cols)
    s_cols = ", ".join(f"s.[{c}]" for c in data_cols)
    t_cols = ", ".join(f"t.[{c}]" for c in data_cols)
    all_cols = ", ".join(f"[{c}]" for c in ["MachineName"] + COL_NAMES)
    s_all = ", ".join(f"s.[{c}]" for c in ["MachineName"] + COL_NAMES)
    merge_sql = f"""
    SET NOCOUNT ON;
    DECLARE @actions TABLE (act nvarchar(10));
    MERGE {target} WITH (HOLDLOCK) AS t
    USING #stage AS s
       ON t.[MachineName] = s.[MachineName] AND t.[recordno] = s.[recordno] AND t.[datestart] = s.[datestart]
    WHEN MATCHED AND EXISTS (SELECT {s_cols} EXCEPT SELECT {t_cols})
        THEN UPDATE SET {set_clause}, t.[UpdatedAt] = SYSDATETIME()
    WHEN NOT MATCHED BY TARGET
        THEN INSERT ({all_cols}) VALUES ({s_all})
    OUTPUT $action INTO @actions;
    SELECT
        SUM(CASE WHEN act = 'INSERT' THEN 1 ELSE 0 END),
        SUM(CASE WHEN act = 'UPDATE' THEN 1 ELSE 0 END)
    FROM @actions;"""
    cur.execute(merge_sql)
    inserted, updated = cur.fetchone()
    cur.execute("DROP TABLE #stage")
    conn.commit()
    return int(inserted or 0), int(updated or 0)


def record_run(conn, cfg, machine_name, started, source_rows, inserted, updated, max_recordno, status, message):
    rschema, rname = split_name(cfg["run_table"])
    try:
        conn.execute(
            f"INSERT INTO [{rschema}].[{rname}] (MachineName, StartedAt, FinishedAt, SourceRows, Inserted, Updated, MaxRecordNo, Status, Message) "
            f"VALUES (?,?,?,?,?,?,?,?,?)",
            machine_name, started, dt.datetime.now(), source_rows, inserted, updated, max_recordno, status, (message or "")[:2000])
        conn.commit()
    except Exception as e:  # never let bookkeeping break the sync
        log.warning("could not write run log: %s", e)


class SqlServerBackend:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.conn = connect(cfg["sql"])

    def ensure_schema(self):
        ensure_schema(self.conn, self.cfg)

    def upload(self, machine_name, rows):
        return upload_rows(self.conn, self.cfg, machine_name, rows)

    def record_run(self, *a):
        record_run(self.conn, self.cfg, *a)

    def rollback(self):
        try:
            self.conn.rollback()
        except Exception:
            pass

    def close(self):
        self.conn.close()


def make_backend(cfg: dict):
    return SupabaseBackend(cfg) if cfg["backend"] == "supabase" else SqlServerBackend(cfg)


# ------------------------------------------------------------------- main
def sync_machine(machine: dict, cfg: dict, backend, dry_run: bool) -> bool:
    name = machine["name"]
    started = dt.datetime.now()
    try:
        mdf_copy = copy_machine_files(machine, cfg)
        rows = read_rows(mdf_copy, cfg["lookback_days"])
        max_rec = max((r["recordno"] for r in rows), default=None)
        newest = max(((r["dateend"] or r["datestart"]) for r in rows), default=None)
        log.info("[%s] parsed %d rows, max recordno %s, newest %s", name, len(rows), max_rec, newest)
        if dry_run:
            for r in rows[-3:]:
                log.info("[%s] sample: %s", name, {k: r[k] for k in ("recordno", "reasonid", "datestart", "dateend", "designno", "strokes", "stitches")})
            return True
        inserted, updated = backend.upload(name, rows)
        log.info("[%s] uploaded: %d new rows, %d existing rows re-sent", name, inserted, updated)
        backend.record_run(name, started, len(rows), inserted, updated, max_rec, "OK", None)
        return True
    except Exception as e:
        log.exception("[%s] sync failed: %s", name, e)
        if backend is not None:
            backend.rollback()
            backend.record_run(name, started, None, None, None, None, "ERROR", str(e))
        return False


def run_once(cfg: dict, args) -> bool:
    machines = cfg["machines"]
    if args.machine:
        machines = [m for m in machines if m["name"].lower() == args.machine.lower()]
        if not machines:
            log.error("no machine named %s in config", args.machine)
            return False
    backend = None
    if not args.dry_run:
        backend = make_backend(cfg)
        backend.ensure_schema()
    ok = True
    try:
        for m in machines:
            ok = sync_machine(m, cfg, backend, args.dry_run) and ok
    finally:
        if backend is not None:
            backend.close()
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--dry-run", action="store_true", help="copy and parse only, do not touch SQL Server")
    ap.add_argument("--loop", action="store_true", help="keep running every interval_minutes")
    ap.add_argument("--machine", help="sync only this machine name")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config(Path(args.config))
    setup_logging(cfg["log_dir"], args.verbose)
    log.info("=== sync start (%s, backend %s) ===", "dry-run" if args.dry_run else "live", cfg["backend"])

    if not args.loop:
        ok = run_once(cfg, args)
        log.info("=== sync %s ===", "finished" if ok else "finished WITH ERRORS")
        return 0 if ok else 1

    interval = max(1, int(cfg["interval_minutes"])) * 60
    while True:
        try:
            run_once(cfg, args)
        except Exception as e:
            log.exception("run failed: %s", e)
        log.info("sleeping %d minutes", interval // 60)
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
