-- Embroidery log sync - Supabase schema (version 2: multi machine, incremental, is_open)
-- Run in Supabase: Dashboard > SQL Editor > New query > paste everything > Run.
-- Safe to run again at any time; it keeps existing data.

-- 1. machine log ------------------------------------------------------------
create table if not exists public.passport_log (
    machine_name  text        not null,
    recordno      integer     not null,
    state         integer,
    boxsrnr       integer,
    reasonid      integer,
    datestart     timestamp   not null,   -- machine local time, stored as-is
    dateend       timestamp,
    designno      text,
    operator      integer,
    headno        integer,
    needleno      integer,
    headsrun      integer,
    strokes       integer,
    stitches      integer,
    numberx       integer,
    aheadmask1    integer,
    aheadmask2    integer,
    thrdbrku1     integer,
    thrdbrkl1     integer,
    thrdbrku2     integer,
    thrdbrkl2     integer,
    errcode       text,
    reservedi     integer,
    reservedg     integer,
    is_open       boolean     not null default false,
    loaded_at     timestamptz not null default now(),
    updated_at    timestamptz,
    primary key (machine_name, recordno, datestart)
);

-- is_open = true marks each machine's newest record: the interval was still
-- running when the file was copied, so dateend and strokes are provisional.
alter table public.passport_log add column if not exists is_open boolean not null default false;

create index if not exists passport_log_datestart_idx
    on public.passport_log (machine_name, datestart desc);
create index if not exists passport_log_open_idx
    on public.passport_log (machine_name) where is_open;

-- Stamp updated_at only when the machine's values really changed.
create or replace function public.passport_log_touch()
returns trigger language plpgsql as $$
begin
    if (old.state, old.boxsrnr, old.reasonid, old.dateend, old.designno, old.operator,
        old.headno, old.needleno, old.headsrun, old.strokes, old.stitches, old.numberx,
        old.aheadmask1, old.aheadmask2, old.thrdbrku1, old.thrdbrkl1, old.thrdbrku2,
        old.thrdbrkl2, old.errcode, old.reservedi, old.reservedg)
       is distinct from
       (new.state, new.boxsrnr, new.reasonid, new.dateend, new.designno, new.operator,
        new.headno, new.needleno, new.headsrun, new.strokes, new.stitches, new.numberx,
        new.aheadmask1, new.aheadmask2, new.thrdbrku1, new.thrdbrkl1, new.thrdbrku2,
        new.thrdbrkl2, new.errcode, new.reservedi, new.reservedg)
    then
        new.updated_at := now();
    else
        new.updated_at := old.updated_at;
    end if;
    new.loaded_at := old.loaded_at;
    return new;
end $$;

drop trigger if exists passport_log_touch on public.passport_log;
create trigger passport_log_touch
    before update on public.passport_log
    for each row execute function public.passport_log_touch();

-- 2. watermark per machine ---------------------------------------------------
create table if not exists public.passport_sync_state (
    machine_name    text primary key,
    last_recordno   integer,
    last_datestart  timestamp,
    file_hash       text,
    file_size       bigint,
    row_count       integer,
    source_path     text,
    last_sync_at    timestamptz
);

-- 3. run log -----------------------------------------------------------------
create table if not exists public.passport_sync_run (
    run_id        bigint generated always as identity primary key,
    machine_name  text        not null,
    started_at    timestamptz not null,
    finished_at   timestamptz,
    source_rows   integer,
    inserted      integer,
    updated       integer,
    max_recordno  integer,
    status        text        not null,
    message       text
);
alter table public.passport_sync_run add column if not exists mode      text;
alter table public.passport_sync_run add column if not exists rows_sent integer;

-- 4. security: the sync uses the secret key (bypasses RLS); other keys read only
alter table public.passport_log        enable row level security;
alter table public.passport_sync_state enable row level security;
alter table public.passport_sync_run   enable row level security;

drop policy if exists "anon read passport_log" on public.passport_log;
create policy "anon read passport_log" on public.passport_log
    for select to anon, authenticated using (true);

drop policy if exists "anon read passport_sync_state" on public.passport_sync_state;
create policy "anon read passport_sync_state" on public.passport_sync_state
    for select to anon, authenticated using (true);

drop policy if exists "anon read passport_sync_run" on public.passport_sync_run;
create policy "anon read passport_sync_run" on public.passport_sync_run
    for select to anon, authenticated using (true);

-- 5. views -------------------------------------------------------------------
-- sewing minutes and stitches per design per day (finished intervals only)
create or replace view public.passport_daily_design as
select machine_name,
       datestart::date                                   as day,
       designno,
       round(sum(extract(epoch from (dateend - datestart))) / 60.0, 1) as sewing_minutes,
       sum(strokes)                                      as stitches,
       count(*)                                          as runs
from public.passport_log
where reasonid = 0 and not is_open
group by machine_name, datestart::date, designno;

-- one line per machine: what it is doing in its newest record, and sync freshness
create or replace view public.passport_machine_status as
select s.machine_name,
       l.recordno     as last_recordno,
       l.reasonid     as last_reasonid,
       l.designno     as last_design,
       l.datestart    as last_datestart,
       l.dateend      as snapshot_time,
       s.row_count    as rows_in_file,
       s.last_sync_at
from public.passport_sync_state s
left join public.passport_log l
       on l.machine_name = s.machine_name
      and l.recordno     = s.last_recordno
      and l.datestart    = s.last_datestart;
