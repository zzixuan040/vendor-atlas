-- Vendor Atlas — shared sourcing workflow state.
-- Paste this into your Supabase project's SQL editor and run it once.
--
-- Scope note: this stores ONLY human workflow state. Vendor facts stay in
-- vendors.json in git, refreshed weekly by scripts/scrape_vendors.py. The two
-- never share a store, which is what keeps an AI refresh from touching a
-- sourcing decision.

create table if not exists vendor_workflow (
  vendor_id   text primary key,          -- matches "id" in vendors.json
  status      text        not null default 'New',
  owner       text,                      -- null = Unassigned
  priority    text        not null default 'Medium',
  notes       text        not null default '',
  next_action text        not null default '',
  updated_at  timestamptz not null default now(),
  updated_by  text
);

create table if not exists vendor_activity (
  id         bigserial   primary key,
  vendor_id  text        not null,
  ts         timestamptz not null default now(),
  actor      text,
  action     text        not null,       -- e.g. 'status', 'owner', 'priority'
  old_value  text,
  new_value  text
);

create index if not exists vendor_activity_vendor_idx on vendor_activity (vendor_id, ts desc);

-- Table privileges for the public (anon) role.
--
-- These are separate from, and required in addition to, the RLS policies below:
-- a policy can only narrow access the role already has. Without these grants
-- every request fails with 42501 "permission denied for table", which reads
-- like an auth problem but isn't.
--
-- Note what is deliberately NOT granted: no delete anywhere, and no update on
-- vendor_activity — that keeps the activity log append-only at the database
-- level rather than only by convention in the UI.
grant usage on schema public to anon;
grant select, insert, update on table vendor_workflow to anon;
grant select, insert on table vendor_activity to anon;
grant usage, select on sequence vendor_activity_id_seq to anon;

alter table vendor_workflow enable row level security;
alter table vendor_activity enable row level security;

-- Open read/write to the anon (public) key.
--
-- TRADEOFF, stated plainly: anyone who can load the dashboard can also change
-- workflow state, because there is no login. That is deliberate for an internal
-- team prototype — the brief explicitly excludes authentication and granular
-- permissions. Vendor facts are not writable from the browser at all (they live
-- in git), so the blast radius is workflow fields only, and every change is
-- recorded in vendor_activity. Add Supabase Auth + per-user policies before
-- putting anything sensitive in here.
drop policy if exists vendor_workflow_anon_all on vendor_workflow;
create policy vendor_workflow_anon_all on vendor_workflow
  for all to anon using (true) with check (true);

drop policy if exists vendor_activity_anon_all on vendor_activity;
create policy vendor_activity_anon_all on vendor_activity
  for all to anon using (true) with check (true);
