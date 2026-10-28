# Embroidery machine log sync

Reads the Tajima/Pulse **Design Spooler** log files (`<machine>.MDF`) from one
folder and uploads them to **Supabase**. Each run sends only what is new.

```
machine PC (no internet) --copy job--> NAS folder --this script--> Supabase
```

## Quick start

1. Install Python 3.10+ and the one dependency:
   ```
   py -3 -m pip install -r requirements.txt
   ```
2. In Supabase open *SQL Editor*, paste all of `supabase_schema.sql`, run it.
   It is safe to run again later; existing data is kept.
3. Copy `config.example.json` to `config.json` and set three values:

   | setting | what to put there |
   |---|---|
   | `data_dir` | the folder with the `.MDF` files, e.g. `\\\\MYNAS\\embroidery\\logs` or `Z:\\logs` |
   | `supabase.url` | Project URL from Dashboard > Project Settings > API |
   | `supabase.key` | the **secret** key (`sb_secret_...`), not the publishable one |

   In JSON every backslash is written twice.
4. Try it without uploading, then for real:
   ```
   run_sync.bat --dry-run
   run_sync.bat
   ```
5. Schedule it every 60 minutes:
   ```
   powershell -ExecutionPolicy Bypass -File .\install_task.ps1
   ```
   Use `-IntervalMinutes 120` for another interval and `-Uninstall` to remove it.

## Changing where the data comes from

Edit `data_dir` in `config.json`. Nothing else has to change. For a one-off run
against another folder:

```
run_sync.bat --data-dir "D:\some\other\folder"
```

The folder is flat: every `*.MDF` directly inside it is one machine, and the
file name is the machine name (`TFGN-620--1260.MDF` becomes `TFGN-620--1260`).
A new machine is picked up automatically the first time its file appears.
Use `exclude` in the config to ignore files, e.g. `["TEST*", "OLD-*"]`.
The `.LDF` files are not needed.

## The pack

| file | purpose |
|---|---|
| `sync_passport.py` | the sync |
| `mdf_reader.py` | reads the MDF file format directly, no SQL Server needed |
| `config.json` | your settings (contains the secret key, keep it private) |
| `config.example.json` | template without secrets |
| `supabase_schema.sql` | tables, trigger, views, security policies |
| `run_sync.bat` | double-click launcher; passes options on |
| `install_task.ps1` | registers the Windows scheduled task |
| `requirements.txt` | Python dependency |
| `legacy\` | the first single-machine version, kept for reference |

`staging\` and `logs\` are created by the script. To move the pack to another
PC, copy the folder; the sync position lives in Supabase, not on the PC.

## How "only new data" works

The machine log is append-only. Record numbers only grow, and the only old
row that ever changes is the newest one, because that interval was still
running when the file was copied.

For each machine the table `passport_sync_state` holds the last uploaded record
and a fingerprint of the last file. Every run, per machine:

1. The file is copied to `staging\` and checked: size stable during the copy
   and a whole number of 8 KB pages. A half-written file is skipped until the
   next run.
2. If the file fingerprint equals the stored one, nothing is read or sent
   (`UNCHANGED`).
3. Otherwise the rows from the last uploaded record onward are sent
   (`INCREMENTAL`): the previously unfinished record again, now with its final
   end time and stroke count, plus every newer record. `overlap_rows` (default
   5) re-sends a few earlier records as a safety margin.
4. The upload is an upsert on `(machine_name, recordno, datestart)`, so sending
   a row twice never creates a duplicate.

| mode in the run log | meaning |
|---|---|
| `FULL` | first time this machine is seen, or `--full` was used |
| `INCREMENTAL` | normal case |
| `RESET` | the machine cleared its own log and started again at record 1; the new log is uploaded and the old history stays |
| `NONE` with status `UNCHANGED` | file identical to last time |
| status `SKIPPED` | file unusable this run (half copied, older than what was already uploaded, not a machine log); the message says why |

One machine failing or being skipped never stops the others.

## Tables in Supabase

**`passport_log`** - one row per run/stop interval of a machine.

| column | meaning (as far as known) |
|---|---|
| machine_name | from the file name |
| recordno | the machine's running record number |
| reasonid | 0 = sewing, other values = stop reason codes |
| datestart / dateend | interval start and end, machine local time |
| designno | design file name (Thai characters arrive as `?` from the machine) |
| operator, headno, needleno | operator id, head, needle in use |
| headsrun | number of heads sewing |
| strokes | stitches sewn in this interval |
| stitches | total stitches of the design (-1 when unknown) |
| aheadmask1/2 | bit mask of active heads |
| thrdbrku1/l1, thrdbrku2/l2 | thread-break bit masks (upper/lower) |
| state, boxsrnr, numberx, errcode, reservedi, reservedg | unused in the samples |
| **is_open** | true for each machine's newest record: still running at copy time, so `dateend` (= the snapshot moment) and `strokes` are provisional. It turns false on the next sync. |
| loaded_at / updated_at | when the row was first uploaded / last really changed |

**`passport_sync_state`** - one row per machine: last record, file fingerprint, last sync time.

**`passport_sync_run`** - one row per machine per run: status, mode, rows sent, new rows, message.

**Views:** `passport_daily_design` (sewing minutes and stitches per design per
day, finished intervals only) and `passport_machine_status` (one line per
machine with its newest record and sync freshness).

## Useful queries

```sql
-- is the sync alive, and what is each machine doing?
select * from passport_machine_status order by machine_name;

-- last runs, problems first
select started_at, machine_name, status, mode, rows_sent, inserted, message
from passport_sync_run
where status <> 'UNCHANGED'
order by run_id desc limit 30;

-- production per machine per day
select machine_name, day, sum(sewing_minutes) as sewing_minutes, sum(stitches) as stitches
from passport_daily_design
group by machine_name, day order by day desc, machine_name;

-- stop reasons per machine per day
select machine_name, datestart::date as day, reasonid, count(*) as stops,
       round(sum(extract(epoch from (dateend - datestart))) / 60.0, 1) as minutes
from passport_log
where reasonid <> 0 and not is_open
group by 1, 2, 3 order by day desc, minutes desc;
```

## Options

```
run_sync.bat --dry-run          read and plan, upload nothing
run_sync.bat --data-dir PATH    another folder for this run
run_sync.bat --machine NAME     one machine only
run_sync.bat --full             re-send every row (repair; harmless, just slower)
run_sync.bat --loop             keep running every interval_minutes
run_sync.bat -v                 verbose
```

## Developer notes

- `config.json` holds the secret key and is not in the repository. Copy
  `config.example.json` to `config.json` and fill it in.
- `tests\simulate.py` and `tests\verify_live.py` expect sample machine files in
  `Data\23` and `Data\24` (three machines, two consecutive days). These are
  production logs and are not in the repository; put your own copies there.
- `build_installer.py` builds the portable package into `installer\` and
  `EmbroiSync_Setup.zip`. Both embed the key from `config.json` and are
  therefore ignored by git. Use `--no-key` for a shareable package.
- `SELF-HOST.md` describes running Supabase on the sync PC itself.

## Notes and limits

- If you rename a machine's file, it becomes a new machine in the database.
- If the NAS keeps an older file for a while, the script skips it and waits.
- Rows that the machine PC had not yet flushed into its MDF at copy time arrive
  with the next file.
- The secret key gives full write access to the Supabase project. Keep
  `config.json` on the sync PC only.
- The `reasonid` codes are not documented in the files; compare them with the
  Passport report on the machine PC to build a lookup table.
