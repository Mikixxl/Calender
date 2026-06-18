-- Host availability: "Working hours", Europe/Berlin.
-- Sun 10-22, Mon-Thu 09-22, Fri 09-16, Sat closed (Shabbat pattern preserved).
with s as (
  insert into sched.availability_schedules (name, timezone, is_default)
  values ('Working hours', 'Europe/Berlin', true)
  returning id
)
insert into sched.availability_rules (schedule_id, wday, from_min, to_min)
select s.id, v.wday, v.from_min, v.to_min
from s, (values
  (0, 600, 1320), (1, 540, 1320), (2, 540, 1320),
  (3, 540, 1320), (4, 540, 1320), (5, 540, 960)
) as v(wday, from_min, to_min);

-- Pilot event type + intake questions.
with et as (
  insert into sched.event_types
    (slug, name, description_html, duration_min, kind, max_invitees,
     location_kind, location_url, is_paid, color, position, active,
     availability_schedule_id, min_notice_min, date_range_days,
     slot_step_min, reminder_offsets_min)
  values (
    'initial-prospective-client-video-conference-30-min',
    'Initial Prospective Client Video Conference, 30 min.',
    '<p><strong>Strategic 30-Minute Zoom Consultation</strong></p>',
    30, 'group', 5, 'zoom_static', 'https://us02web.zoom.us/j/3039347355',
    false, '#17e885', 2, true,
    (select id from sched.availability_schedules where is_default limit 1),
    240, 60, 30, '{1440,60}'
  )
  returning id
)
insert into sched.event_type_questions (event_type_id, position, label, qtype, required, enabled)
select et.id, q.position, q.label, q.qtype::sched.question_type, q.required, true
from et, (values
  (0, 'Which specific services or products are you seeking information about?', 'text',         true),
  (1, 'Available budget?',                                                     'string',       true),
  (2, 'Please provide any supporting links that may help us reply to your inquiry', 'text',     false),
  (3, 'Your phone number, please',                                             'phone_number', false)
) as q(position, label, qtype, required);
