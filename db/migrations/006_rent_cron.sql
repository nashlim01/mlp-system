-- Rent lines scheduled inside the database with pg_cron, so they keep being created when the
-- worker isn't running (alpha on Streamlit Community Cloud). Safe alongside the worker:
-- generate_rent_schedule is idempotent. On a Postgres without pg_cron the schedule is skipped.

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
    UPDATE job_runs SET finished_at = NOW(), status = 'ok', rows_affected = n WHERE id = run_id;
  EXCEPTION WHEN OTHERS THEN            -- shows under System health on the admin's Today page
    UPDATE job_runs SET finished_at = NOW(), status = 'failed', error = SQLERRM WHERE id = run_id;
  END;
END $$;

REVOKE EXECUTE ON FUNCTION run_rent_job(text) FROM PUBLIC;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    REVOKE EXECUTE ON FUNCTION run_rent_job(text) FROM anon, authenticated;
  END IF;
END $$;

-- pg_cron runs in UTC; Asia/Kuching is UTC+8.
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_cron') THEN
    CREATE EXTENSION IF NOT EXISTS pg_cron;
    -- 25th, 08:00 MYT: next month's lines
    PERFORM cron.schedule('rent_next_month', '0 0 25 * *', $c$SELECT public.run_rent_job('next')$c$);
    -- daily 00:30 MYT: this month's lines. Daily (not just the 1st) so a tenancy started
    -- mid-month gets its line the next morning, and a missed run is caught the day after.
    PERFORM cron.schedule('rent_this_month', '30 16 * * *', $c$SELECT public.run_rent_job('this')$c$);
  ELSE
    RAISE NOTICE 'pg_cron not available: run rent jobs with the worker instead';
  END IF;
END $$;
