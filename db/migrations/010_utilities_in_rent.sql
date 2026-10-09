-- Monthly total = rent + electricity (SEB) + water. Each utility bill is charged once, to the first rent
-- month of that unit's tenancy that falls due on or after the bill date (= the newest bill before the
-- due date). Payments clear rent first, then utilities.

CREATE TABLE IF NOT EXISTS utility_charges (
  id               BIGSERIAL PRIMARY KEY,
  rent_schedule_id BIGINT NOT NULL REFERENCES rent_schedule(id),
  utility_bill_id  BIGINT NOT NULL UNIQUE REFERENCES utility_bills(id),
  kind             TEXT NOT NULL CHECK (kind IN ('electric', 'water')),
  amount           NUMERIC(10,2) NOT NULL,
  created_at       TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS utility_charges_line ON utility_charges (rent_schedule_id);
ALTER TABLE utility_charges ENABLE ROW LEVEL SECURITY;

-- Staff ask the worker to run a job now (e.g. the SEB check) from the Utilities page
CREATE TABLE IF NOT EXISTS job_requests (
  id           BIGSERIAL PRIMARY KEY,
  job          TEXT NOT NULL,
  requested_by INT REFERENCES staff(id),
  requested_at TIMESTAMPTZ DEFAULT NOW(),
  picked_at    TIMESTAMPTZ
);
ALTER TABLE job_requests ENABLE ROW LEVEL SECURITY;

-- Charge new bills to rent months. Only months due from 10 days ago onwards and within 45 days after the
-- bill, so the 12 months of history SEBCares shows on the first run never land on old, settled months.
CREATE OR REPLACE FUNCTION attach_utility_bills() RETURNS integer
LANGUAGE plpgsql SET search_path = public AS $$
DECLARE n integer;
BEGIN
  INSERT INTO utility_charges (rent_schedule_id, utility_bill_id, kind, amount)
  SELECT DISTINCT ON (b.id) rs.id, b.id, ua.type, b.amount
  FROM utility_bills b
  JOIN utility_accounts ua ON ua.id = b.utility_account_id AND ua.unit_id IS NOT NULL
  JOIN tenancies t ON t.unit_id = ua.unit_id AND t.status IN ('active', 'ended')
                  AND b.bill_date >= t.start_date AND (t.end_date IS NULL OR b.bill_date <= t.end_date)
  JOIN rent_schedule rs ON rs.tenancy_id = t.id
                       AND rs.due_date BETWEEN b.bill_date AND b.bill_date + 45
                       AND rs.due_date >= today_myt() - 10
  WHERE NOT b.reversed AND b.amount > 0
    AND NOT EXISTS (SELECT 1 FROM utility_charges c WHERE c.utility_bill_id = b.id)
  ORDER BY b.id, rs.due_date
  ON CONFLICT (utility_bill_id) DO NOTHING;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

REVOKE EXECUTE ON FUNCTION attach_utility_bills() FROM PUBLIC;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    REVOKE EXECUTE ON FUNCTION attach_utility_bills() FROM anon, authenticated;
  END IF;
END $$;

-- Rent lines appear on the 25th / daily: attach waiting bills right after (pg_cron where available)
CREATE OR REPLACE FUNCTION run_rent_job(p_which text) RETURNS void
LANGUAGE plpgsql SET search_path = public AS $$
DECLARE
  run_id   bigint;
  v_period date := date_trunc('month', today_myt())::date;
  n        integer;
BEGIN
  IF p_which = 'next' THEN
    v_period := (v_period + INTERVAL '1 month')::date;
  END IF;
  INSERT INTO job_runs (job, status)
  VALUES ('rent_schedule ' || to_char(v_period, 'YYYY-MM') || ' (db)', 'running')
  RETURNING id INTO run_id;
  BEGIN
    n := generate_rent_schedule(v_period);
    PERFORM attach_utility_bills();
    UPDATE job_runs SET finished_at = NOW(), status = 'ok', rows_affected = n WHERE id = run_id;
  EXCEPTION WHEN OTHERS THEN
    UPDATE job_runs SET finished_at = NOW(), status = 'failed', error = SQLERRM WHERE id = run_id;
  END;
END $$;

-- v_rent_status gains the utilities (new columns last). amount_due stays the rent; balance and status
-- now follow the monthly total. v_reminder_due lists v_rent_status's columns, so it is rebuilt.
DROP VIEW IF EXISTS v_reminder_due;
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
       rs.amount_due + COALESCE(uc.total, 0) - COALESCE(p.paid, 0) AS balance,
       p.last_paid_date,
       CASE WHEN COALESCE(p.paid, 0) >= rs.amount_due + COALESCE(uc.total, 0) THEN 'PAID'
            WHEN today_myt() > rs.due_date + t.grace_days      THEN 'OVERDUE'
            WHEN today_myt() > rs.due_date                     THEN 'GRACE'
            WHEN COALESCE(p.paid, 0) > 0                       THEN 'PARTIAL'
            ELSE 'DUE' END                     AS status,
       CASE WHEN COALESCE(p.paid, 0) >= rs.amount_due + COALESCE(uc.total, 0) THEN 0
            ELSE GREATEST(today_myt() - rs.due_date, 0) END AS days_late,
       rs.due_date + t.grace_days              AS grace_end,
       COALESCE(uc.electric, 0)                AS electric_due,
       COALESCE(uc.water, 0)                   AS water_due,
       rs.amount_due + COALESCE(uc.total, 0)   AS total_due,
       LEAST(COALESCE(p.paid, 0), rs.amount_due)               AS rent_paid,      -- rent is paid first
       GREATEST(COALESCE(p.paid, 0) - rs.amount_due, 0)        AS utilities_paid
FROM rent_schedule rs
JOIN tenancies t  ON t.id  = rs.tenancy_id
JOIN units u      ON u.id  = t.unit_id
JOIN tenants tn   ON tn.id = t.tenant_id
LEFT JOIN staff s ON s.id  = u.assigned_staff_id
LEFT JOIN (
  SELECT rent_schedule_id, SUM(amount) AS paid, MAX(paid_date) AS last_paid_date
  FROM payments WHERE NOT voided
  GROUP BY rent_schedule_id
) p ON p.rent_schedule_id = rs.id
LEFT JOIN (
  SELECT rent_schedule_id, SUM(amount) AS total,
         SUM(amount) FILTER (WHERE kind = 'electric') AS electric,
         SUM(amount) FILTER (WHERE kind = 'water')    AS water
  FROM utility_charges GROUP BY rent_schedule_id
) uc ON uc.rent_schedule_id = rs.id;

-- Same as in 009, rebuilt on the wider v_rent_status
CREATE VIEW v_reminder_due WITH (security_invoker = true) AS
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

-- Messages show what the total is made of: {breakdown} = "rent RM850.00 + electricity RM61.95 + water RM20.00"
UPDATE message_templates SET body = replace(body, '(RM{balance})', '(RM{balance}: {breakdown})')
WHERE code IN ('rent_reminder', 'rent_grace_reminder', 'rent_grace_end') AND body LIKE '%(RM{balance})%';
UPDATE message_templates SET body = replace(body, 'the rent for', 'the rent and utilities for')
WHERE code IN ('rent_reminder', 'rent_grace_reminder', 'rent_grace_end', 'rent_overdue')
  AND body NOT LIKE '%rent and utilities%';
