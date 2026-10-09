-- Every bank statement file uploaded (screenshot or CSV), so the same file is never read by the AI twice,
-- and a log of each incoming payment: which upload it came from and what it was recorded as.

CREATE TABLE IF NOT EXISTS bank_uploads (
  id            BIGSERIAL PRIMARY KEY,
  file_hash     TEXT NOT NULL UNIQUE,                 -- sha256 of the file: same file = same upload
  file_name     TEXT,
  source        TEXT NOT NULL CHECK (source IN ('screenshot', 'csv')),
  account_label TEXT,
  taken_on      DATE,                                 -- screenshot date ('Today' / 'Yesterday')
  credits       JSONB,                                -- what the AI read, kept so it is never read again
  notes         TEXT,
  uploaded_by   INT REFERENCES staff(id),
  uploaded_at   TIMESTAMPTZ DEFAULT NOW(),
  added_at      TIMESTAMPTZ,                          -- NULL = read but not saved yet
  added         INT,                                  -- new incoming payments saved
  total         INT                                   -- lines in the file (the rest were already saved)
);
ALTER TABLE bank_uploads ENABLE ROW LEVEL SECURITY;

ALTER TABLE bank_transactions ADD COLUMN IF NOT EXISTS upload_id BIGINT REFERENCES bank_uploads(id);

-- One row per incoming payment, with what became of it
CREATE OR REPLACE VIEW v_bank_log WITH (security_invoker = true) AS
SELECT b.id, b.txn_date, b.description, b.amount, b.account_label, b.status, b.imported_at,
       up.file_name, up.source, s.name AS uploaded_by,
       string_agg(DISTINCT u.code || ' · ' || tn.name || ' · ' || to_char(rs.period, 'Mon YYYY'), '; ')
         AS paid_for,
       CASE WHEN b.status = 'ignored' THEN 'Ignored (not rent)'
            WHEN count(p.id) = 0 THEN 'Not recorded yet'
            WHEN bool_or(p.auto_from_bank) THEN 'Auto: tenant''s name on the bank line'
            WHEN EXISTS (SELECT 1 FROM receipts x WHERE x.bank_txn_id = b.id
                         AND x.status IN ('matched', 'confirmed')) THEN 'Checked with a slip'
            ELSE 'Recorded by staff' END AS how
FROM bank_transactions b
LEFT JOIN bank_uploads up ON up.id = b.upload_id
LEFT JOIN staff s ON s.id = up.uploaded_by
LEFT JOIN payments p ON NOT p.voided AND (p.bank_txn_id = b.id OR p.id = b.matched_payment_id)
LEFT JOIN rent_schedule rs ON rs.id = p.rent_schedule_id
LEFT JOIN tenancies t ON t.id = p.tenancy_id
LEFT JOIN units u ON u.id = t.unit_id
LEFT JOIN tenants tn ON tn.id = t.tenant_id
GROUP BY b.id, up.file_name, up.source, s.name;
