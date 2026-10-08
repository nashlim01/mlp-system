-- "Today" in Malaysia, regardless of the server's timezone
CREATE OR REPLACE FUNCTION today_myt() RETURNS date
LANGUAGE sql STABLE AS $$ SELECT (now() AT TIME ZONE 'Asia/Kuching')::date $$;

-- Create one rent line per active tenancy for a month (safe to re-run)
CREATE OR REPLACE FUNCTION generate_rent_schedule(p_period date)
RETURNS integer LANGUAGE plpgsql AS $$
DECLARE
  n integer;
  month_end date;
BEGIN
  p_period  := date_trunc('month', p_period)::date;
  month_end := (p_period + INTERVAL '1 month' - INTERVAL '1 day')::date;

  INSERT INTO rent_schedule (tenancy_id, period, amount_due, due_date, note)
  SELECT t.id, p_period, t.monthly_rent,
         GREATEST(make_date(EXTRACT(YEAR FROM p_period)::int,
                            EXTRACT(MONTH FROM p_period)::int, t.due_day),
                  t.start_date),                 -- never due before the tenancy starts
         CASE WHEN t.start_date > p_period THEN 'First month: check pro-rata'
              WHEN t.end_date < month_end THEN 'Last month: check pro-rata'
         END
  FROM tenancies t
  WHERE t.status = 'active'
    AND t.start_date <= month_end
    AND (t.end_date IS NULL OR t.end_date >= p_period)
  ON CONFLICT (tenancy_id, period) DO NOTHING;

  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

-- Views use security_invoker so Supabase's public API (anon/authenticated) is still blocked
-- by RLS on the underlying tables. The portal and worker connect as the owner: unaffected.

-- Rent status is always computed, never stored
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
       CASE WHEN COALESCE(p.paid, 0) >= rs.amount_due THEN 'PAID'
            WHEN today_myt() > rs.due_date            THEN 'OVERDUE'
            WHEN COALESCE(p.paid, 0) > 0              THEN 'PARTIAL'
            ELSE 'DUE' END                     AS status,
       CASE WHEN COALESCE(p.paid, 0) >= rs.amount_due THEN 0
            ELSE GREATEST(today_myt() - rs.due_date, 0) END AS days_late
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

CREATE OR REPLACE VIEW v_lease_expiry WITH (security_invoker = true) AS
SELECT t.id AS tenancy_id, u.code AS unit_code, u.assigned_staff_id,
       tn.name AS tenant_name, t.end_date, t.end_date - today_myt() AS days_left
FROM tenancies t
JOIN units u    ON u.id  = t.unit_id
JOIN tenants tn ON tn.id = t.tenant_id
WHERE t.status = 'active' AND t.end_date IS NOT NULL
  AND t.end_date <= today_myt() + 60;

CREATE OR REPLACE VIEW v_electric_latest WITH (security_invoker = true) AS
SELECT DISTINCT ON (ua.id)
       ua.id AS utility_account_id, ua.account_no,
       u.code AS unit_code, u.assigned_staff_id,
       bc.amount_due, bc.outstanding, bc.due_date, bc.checked_at
FROM utility_accounts ua
JOIN units u ON u.id = ua.unit_id
LEFT JOIN bill_checks bc ON bc.utility_account_id = ua.id
WHERE ua.type = 'electric'
ORDER BY ua.id, bc.checked_at DESC NULLS LAST;

CREATE OR REPLACE VIEW v_last_reminder WITH (security_invoker = true) AS
SELECT DISTINCT ON (tenancy_id)
       tenancy_id,
       (created_at AT TIME ZONE 'Asia/Kuching') AS last_reminded_at,
       staff_id
FROM reminder_log
ORDER BY tenancy_id, created_at DESC;

-- The public API must not call these either (only the portal, worker and SQL Editor)
REVOKE EXECUTE ON FUNCTION generate_rent_schedule(date) FROM PUBLIC;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    REVOKE EXECUTE ON FUNCTION generate_rent_schedule(date) FROM anon, authenticated;
  END IF;
END $$;
