-- Blocks Supabase's public API from reading tenant data.
-- The portal and worker connect as the table owner, so they are not affected.
ALTER TABLE staff             ENABLE ROW LEVEL SECURITY;
ALTER TABLE landlords         ENABLE ROW LEVEL SECURITY;
ALTER TABLE landlord_bank     ENABLE ROW LEVEL SECURITY;
ALTER TABLE units             ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants           ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenancies         ENABLE ROW LEVEL SECURITY;
ALTER TABLE rent_schedule     ENABLE ROW LEVEL SECURITY;
ALTER TABLE payments          ENABLE ROW LEVEL SECURITY;
ALTER TABLE bank_transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE followups         ENABLE ROW LEVEL SECURITY;
ALTER TABLE utility_accounts  ENABLE ROW LEVEL SECURITY;
ALTER TABLE bill_checks       ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log         ENABLE ROW LEVEL SECURITY;
ALTER TABLE message_templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE reminder_log      ENABLE ROW LEVEL SECURITY;
ALTER TABLE job_runs          ENABLE ROW LEVEL SECURITY;

-- Do not create RLS policies for the anon or authenticated roles. With RLS on and no
-- policies, Supabase's public API returns nothing, which is what we want.
