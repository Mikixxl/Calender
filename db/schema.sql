-- IFB Scheduler core schema  (schema: sched)
-- All instants stored UTC. Host wall-clock rules live in an IANA zone and
-- are converted per-date at slot time so DST handles itself.
create schema if not exists sched;

create type sched.event_kind          as enum ('solo','group');
create type sched.question_type       as enum ('text','string','single_select','multi_select','phone_number');
create type sched.booking_status      as enum ('scheduled','completed','no_show','canceled','rescheduled');
create type sched.notification_type   as enum ('confirmation','reminder','no_show','cancellation','reschedule');
create type sched.notification_status as enum ('pending','sent','failed','skipped');

create table sched.availability_schedules (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  timezone    text not null,
  is_default  boolean not null default false,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create table sched.availability_rules (
  id             uuid primary key default gen_random_uuid(),
  schedule_id    uuid not null references sched.availability_schedules(id) on delete cascade,
  wday           smallint,
  override_date  date,
  from_min       smallint not null,
  to_min         smallint not null,
  is_unavailable boolean not null default false,
  check (from_min >= 0 and from_min < 1440),
  check (to_min   > 0  and to_min   <= 1440),
  check (to_min > from_min),
  check ((wday is not null) <> (override_date is not null))
);
create index on sched.availability_rules (schedule_id, wday);
create index on sched.availability_rules (schedule_id, override_date);

create table sched.event_types (
  id                       uuid primary key default gen_random_uuid(),
  slug                     text not null unique,
  name                     text not null,
  description_html         text,
  duration_min             smallint not null,
  buffer_before_min        smallint not null default 0,
  buffer_after_min         smallint not null default 0,
  kind                     sched.event_kind not null default 'group',
  max_invitees             smallint not null default 1,
  location_kind            text not null default 'zoom_static',
  location_url             text,
  is_paid                  boolean not null default false,
  price_cents              integer,
  currency                 text,
  color                    text,
  position                 smallint not null default 0,
  active                   boolean not null default true,
  availability_schedule_id uuid references sched.availability_schedules(id),
  min_notice_min           integer not null default 240,
  date_range_days          smallint not null default 60,
  slot_step_min            smallint not null default 30,
  min_lead_days            smallint not null default 1,
  reminder_offsets_min     integer[] not null default '{1440,60}',
  created_at               timestamptz not null default now(),
  updated_at               timestamptz not null default now()
);
create index on sched.event_types (active, position);

create table sched.event_type_questions (
  id             uuid primary key default gen_random_uuid(),
  event_type_id  uuid not null references sched.event_types(id) on delete cascade,
  position       smallint not null default 0,
  label          text not null,
  qtype          sched.question_type not null default 'text',
  required       boolean not null default false,
  enabled        boolean not null default true,
  answer_choices jsonb not null default '[]'::jsonb,
  include_other  boolean not null default false
);
create index on sched.event_type_questions (event_type_id, position);

create table sched.bookings (
  id                   uuid primary key default gen_random_uuid(),
  event_type_id        uuid not null references sched.event_types(id),
  start_utc            timestamptz not null,
  end_utc              timestamptz not null,
  status               sched.booking_status not null default 'scheduled',
  booker_name          text not null,
  booker_email         text not null,
  booker_timezone      text not null,
  host_timezone        text not null,
  location_url         text,
  answers              jsonb not null default '{}'::jsonb,
  cancel_token         uuid not null default gen_random_uuid(),
  reschedule_of        uuid references sched.bookings(id),
  attendance_marked_at timestamptz,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),
  check (end_utc > start_utc)
);
create index on sched.bookings (start_utc);
create index on sched.bookings (event_type_id, start_utc);
create index on sched.bookings (status);
create unique index on sched.bookings (cancel_token);
create unique index bookings_no_double_book
  on sched.bookings (event_type_id, start_utc, lower(booker_email))
  where status = 'scheduled';

create table sched.notifications (
  id                uuid primary key default gen_random_uuid(),
  booking_id        uuid not null references sched.bookings(id) on delete cascade,
  ntype             sched.notification_type not null,
  channel           text not null default 'email',
  scheduled_for_utc timestamptz not null,
  status            sched.notification_status not null default 'pending',
  attempts          smallint not null default 0,
  sent_at           timestamptz,
  error             text,
  dedupe_key        text not null,
  created_at        timestamptz not null default now()
);
create unique index on sched.notifications (dedupe_key);
create index on sched.notifications (status, scheduled_for_utc);

create or replace function sched.touch_updated_at() returns trigger
language plpgsql as $$
begin new.updated_at = now(); return new; end $$;

create trigger trg_event_types_touch  before update on sched.event_types
  for each row execute function sched.touch_updated_at();
create trigger trg_bookings_touch     before update on sched.bookings
  for each row execute function sched.touch_updated_at();
create trigger trg_avail_sched_touch  before update on sched.availability_schedules
  for each row execute function sched.touch_updated_at();

-- Defense in depth: RLS on, no public policies. The backend connects as the
-- owner role and bypasses RLS; anon/authenticated are shut out.
alter table sched.availability_schedules enable row level security;
alter table sched.availability_rules     enable row level security;
alter table sched.event_types            enable row level security;
alter table sched.event_type_questions   enable row level security;
alter table sched.bookings               enable row level security;
alter table sched.notifications          enable row level security;
