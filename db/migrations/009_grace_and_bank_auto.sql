-- Grace period, reminder stages, and payments recorded straight from the bank (no slip)

-- Days after the due date before rent counts as overdue (7 unless agreed otherwise)
ALTER TABLE tenancies ADD COLUMN IF NOT EXISTS grace_days INT NOT NULL DEFAULT 7
  CHECK (grace_days BETWEEN 0 AND 60);

-- Bank credit recorded automatically under the tenant's name; staff can undo it
ALTER TABLE payments          ADD COLUMN IF NOT EXISTS auto_from_bank BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE bank_transactions ADD COLUMN IF NOT EXISTS no_auto        BOOLEAN NOT NULL DEFAULT FALSE;

-- Same columns as before (grace_end added last). Status:
--   DUE / PARTIAL  up to the due date · GRACE  after the due date, within the grace days · OVERDUE  after that
CREATE OR REPLACE VIEW v_rent_status WITH (security_invoker = true) AS
SELECT rs.id                                   AS schedule_id,
       rs.period, rs.amount_due, rs.due_date, rs.note,
       t.id                                    AS tenancy_id,
       u.id                                    AS unit_id,
       u.code                                  AS unit_code,
       u.area,
       u.landlord_id,
       u.assigned_staff_id,
       COALESCE(s.name, 'Unassigned')          AS staff_name,
       tn.name                                 AS tenant_name,
       tn.phone                                AS tenant_phone,
       COALESCE(p.paid, 0)                     AS paid,
       rs.amount_due - COALESCE(p.paid, 0)     AS balance,
       p.last_paid_date,
       CASE WHEN COALESCE(p.paid, 0) >= rs.amount_due           THEN 'PAID'
            WHEN today_myt() > rs.due_date + t.grace_days      THEN 'OVERDUE'
            WHEN today_myt() > rs.due_date                     THEN 'GRACE'
            WHEN COALESCE(p.paid, 0) > 0                       THEN 'PARTIAL'
            ELSE 'DUE' END                     AS status,
       CASE WHEN COALESCE(p.paid, 0) >= rs.amount_due THEN 0
            ELSE GREATEST(today_myt() - rs.due_date, 0) END AS days_late,
       rs.due_date + t.grace_days              AS grace_end
FROM rent_schedule rs
JOIN tenancies t  ON t.id  = rs.tenancy_id
JOIN units u      ON u.id  = t.unit_id
JOIN tenants tn   ON tn.id = t.tenant_id
LEFT JOIN staff s ON s.id  = u.assigned_staff_id
LEFT JOIN (
  SELECT rent_schedule_id, SUM(amount) AS paid, MAX(paid_date) AS last_paid_date
  FROM payments WHERE NOT voided
  GROUP BY rent_schedule_id
) p ON p.rent_schedule_id = rs.id;

-- Which reminder each unpaid month needs now (stage = template code), NULL = nothing to send today:
--   rent_reminder        1st: on the due date (start of the cycle)
--   rent_grace_reminder  2nd: day 5 of a 7-day grace period (5/7 of the way through)
--   rent_grace_end       3rd: on the last day of the grace period
--   rent_balance         a part payment arrived after the last reminder: ask for the remainder
-- After the grace period there are no more reminders: the month shows as OVERDUE for staff to settle in person.
CREATE OR REPLACE VIEW v_reminder_due WITH (security_invoker = true) AS
SELECT r.*, x.last_sent_at,
       CASE
         WHEN r.status = 'PAID' OR r.due_date > today_myt() OR today_myt() > r.grace_end THEN NULL
         WHEN x.last_sent_at IS NOT NULL AND pay.last_recorded_at > x.last_sent_at THEN 'rent_balance'
         WHEN today_myt() = r.grace_end AND r.grace_end > r.due_date THEN
              CASE WHEN NOT COALESCE(x.third_sent, FALSE) THEN 'rent_grace_end' END
         WHEN today_myt() >= r.due_date + round((r.grace_end - r.due_date) * 5 / 7.0)::int
              AND r.grace_end - r.due_date >= 3 THEN
              CASE WHEN NOT COALESCE(x.second_sent, FALSE) THEN 'rent_grace_reminder' END
         WHEN NOT COALESCE(x.first_sent, FALSE) THEN 'rent_reminder'
       END AS stage
FROM v_rent_status r
LEFT JOIN (SELECT rent_schedule_id, MAX(created_at) AS last_sent_at,
                  bool_or(template_code = 'rent_reminder')       AS first_sent,
                  bool_or(template_code = 'rent_grace_reminder') AS second_sent,
                  bool_or(template_code = 'rent_grace_end')      AS third_sent
           FROM reminder_log GROUP BY rent_schedule_id) x ON x.rent_schedule_id = r.schedule_id
LEFT JOIN (SELECT rent_schedule_id, MAX(recorded_at) AS last_recorded_at
           FROM payments WHERE NOT voided GROUP BY rent_schedule_id) pay ON pay.rent_schedule_id = r.schedule_id;

INSERT INTO message_templates (code, name, body) VALUES
('rent_grace_reminder', 'Rent: 2nd reminder (in grace period)',
 'Hi {tenant_name}, this is {staff_name} from Miri Landmark Property. A gentle reminder that the rent for {unit_code} for {period} (RM{balance}) was due on {due_date}. The grace period ends on {grace_end}. Kindly arrange payment and share the slip here. Thank you!'),
('rent_grace_end', 'Rent: 3rd reminder (grace period ends today)',
 'Hi {tenant_name}, this is {staff_name} from Miri Landmark Property. Today ({grace_end}) is the last day of the grace period for the rent and utilities of {unit_code} for {period} (RM{balance}), due {due_date}. Kindly make payment today and share the slip here to avoid it being marked overdue. Thank you!'),
('rent_balance', 'Rent: remaining balance',
 'Hi {tenant_name}, thank you for your payment of RM{paid} for {unit_code} ({period}). There is a remaining balance of RM{balance} (due {due_date}). Kindly arrange the remainder and share the slip here. Thank you!')
ON CONFLICT (code) DO NOTHING;
UPDATE message_templates SET name = 'Rent: 1st reminder (due date)' WHERE code = 'rent_reminder' AND name = 'Rent reminder (before due)';
UPDATE message_templates SET name = 'Rent: overdue (after grace)' WHERE code = 'rent_overdue' AND name = 'Rent overdue';
