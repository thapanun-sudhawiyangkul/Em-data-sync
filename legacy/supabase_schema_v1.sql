-- Run this once in Supabase: Dashboard > SQL Editor > New query > paste > Run.
-- Creates the two tables sync_passport.py writes to.

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
    loaded_at     timestamptz not null default now(),
    updated_at    timestamptz,
    primary key (machine_name, recordno, datestart)
);

create index if not exists passport_log_datestart_idx
    on public.passport_log (machine_name, datestart desc);

-- Stamp updated_at only when the machine's values actually changed
-- (the sync upserts every row it reads, unchanged rows must not look modified).
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

-- One row per sync run, for monitoring.
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

-- Lock the tables down: the script uses the service_role key, which bypasses RLS,
-- and the anon key (used by web/mobile apps) gets read-only access.
alter table public.passport_log      enable row level security;
alter table public.passport_sync_run enable row level security;

drop policy if exists "anon read passport_log" on public.passport_log;
create policy "anon read passport_log" on public.passport_log
    for select to anon, authenticated using (true);

drop policy if exists "anon read passport_sync_run" on public.passport_sync_run;
create policy "anon read passport_sync_run" on public.passport_sync_run
    for select to anon, authenticated using (true);

-- Handy view: sewing minutes and stitches per design per day
create or replace view public.passport_daily_design as
select machine_name,
       datestart::date                                   as day,
       designno,
       round(sum(extract(epoch from (dateend - datestart))) / 60.0, 1) as sewing_minutes,
       sum(strokes)                                      as stitches,
       count(*)                                          as runs
from public.passport_log
where reasonid = 0
group by machine_name, datestart::date, designno;
