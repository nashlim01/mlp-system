-- Utility bills: Sarawak Energy (SEBCares, read by worker/jobs/electric.py) and water
-- Accounts are discovered from the company SEBCares login; staff link each one to a unit.

ALTER TABLE utility_accounts
  ADD COLUMN IF NOT EXISTS nickname        TEXT,
  ADD COLUMN IF NOT EXISTS address         TEXT,
  ADD COLUMN IF NOT EXISTS subscription_id TEXT,          -- SEBCares ContractSubscriptionId
  ADD COLUMN IF NOT EXISTS tariff          TEXT,
  ADD COLUMN IF NOT EXISTS active          BOOLEAN,
  ADD COLUMN IF NOT EXISTS last_seen_at    TIMESTAMPTZ;   -- last time the portal listed it

ALTER TABLE bill_checks
  ADD COLUMN IF NOT EXISTS deposit NUMERIC(10,2),
  ADD COLUMN IF NOT EXISTS credit  NUMERIC(10,2);

-- Electricity bills from SEBCares and water bills (typed by staff until the water scraper exists)
CREATE TABLE IF NOT EXISTS utility_bills (
  id                 BIGSERIAL PRIMARY KEY,
  utility_account_id INT NOT NULL REFERENCES utility_accounts(id),
  bill_no            TEXT NOT NULL,                       -- SEB PRINT_DOC; 'manual-<date>' when typed in
  bill_date          DATE NOT NULL,
  due_date           DATE,
  period_start       DATE,
  period_end         DATE,
  amount             NUMERIC(10,2) NOT NULL,              -- this bill's charges
  total_due          NUMERIC(10,2),                       -- including arrears (SEB)
  pdf_name           TEXT,
  reversed           BOOLEAN NOT NULL DEFAULT FALSE,
  source             TEXT NOT NULL DEFAULT 'seb_portal' CHECK (source IN ('seb_portal', 'manual')),
  entered_by         INT REFERENCES staff(id),
  first_seen_at      TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (utility_account_id, bill_no)
);
CREATE INDEX IF NOT EXISTS utility_bills_account ON utility_bills (utility_account_id, bill_date DESC);
ALTER TABLE utility_bills ENABLE ROW LEVEL SECURITY;

-- Linked accounts only (unit_id set); new columns go last so the view can be replaced in place
CREATE OR REPLACE VIEW v_electric_latest WITH (security_invoker = true) AS
SELECT DISTINCT ON (ua.id)
       ua.id AS utility_account_id, ua.account_no,
       u.code AS unit_code, u.assigned_staff_id,
       bc.amount_due, bc.outstanding, bc.due_date, bc.checked_at,
       ua.nickname, ua.address, bc.deposit
FROM utility_accounts ua
JOIN units u ON u.id = ua.unit_id
LEFT JOIN bill_checks bc ON bc.utility_account_id = ua.id
WHERE ua.type = 'electric'
ORDER BY ua.id, bc.checked_at DESC NULLS LAST;
