-- Transfer slips read by AI and matched to bank credits (demo phase).
-- A slip is auto-recorded only when the match is unambiguous; everything else waits for review.

CREATE TABLE receipts (
  id                   BIGSERIAL PRIMARY KEY,
  file_path            TEXT NOT NULL,
  file_hash            TEXT NOT NULL UNIQUE,          -- same file uploaded twice
  media_type           TEXT,
  source               TEXT NOT NULL DEFAULT 'upload' CHECK (source IN ('upload','whatsapp')),
  uploaded_by          INT REFERENCES staff(id),
  uploaded_at          TIMESTAMPTZ DEFAULT NOW(),
  status               TEXT NOT NULL DEFAULT 'review'
                       CHECK (status IN ('matched','review','confirmed','rejected','duplicate','not_slip')),
  -- what the AI read (account numbers: last 4 digits only)
  amount               NUMERIC(12,2),
  paid_at              TIMESTAMP,                      -- local (Asia/Kuching) time printed on the slip
  sender_name          TEXT,
  sender_bank          TEXT,
  recipient_name       TEXT,
  recipient_acct_last4 TEXT,
  transfer_mode        TEXT,
  reference            TEXT,
  txn_ref              TEXT,
  duitnow_ref          TEXT,
  ai_confidence        NUMERIC(3,2),
  ai_raw               JSONB,
  flags                TEXT[] NOT NULL DEFAULT '{}',
  -- what it was matched to
  bank_txn_id          BIGINT REFERENCES bank_transactions(id),
  tenancy_id           INT REFERENCES tenancies(id),
  rent_schedule_id     BIGINT REFERENCES rent_schedule(id),
  payment_id           BIGINT REFERENCES payments(id),
  created_payment      BOOLEAN NOT NULL DEFAULT FALSE,  -- the match created payment_id (Undo voids it)
  no_auto              BOOLEAN NOT NULL DEFAULT FALSE,  -- staff undid a match: never auto-record again
  duplicate_of         BIGINT REFERENCES receipts(id),
  reviewed_by          INT REFERENCES staff(id),
  reviewed_at          TIMESTAMPTZ,
  note                 TEXT
);
-- the same transfer screenshotted twice (different file, same bank reference)
CREATE UNIQUE INDEX receipts_one_per_txn_ref ON receipts (txn_ref)
  WHERE txn_ref IS NOT NULL AND status <> 'duplicate';
CREATE INDEX receipts_status ON receipts (status);

-- Names a tenant pays under (spouse, company...), learned when staff confirm a slip.
CREATE TABLE payer_aliases (
  id         BIGSERIAL PRIMARY KEY,
  tenant_id  INT NOT NULL REFERENCES tenants(id),
  alias_norm TEXT NOT NULL,                            -- normalised: upper case, single spaces
  created_by INT REFERENCES staff(id),
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (tenant_id, alias_norm)
);

ALTER TABLE bank_transactions ADD COLUMN IF NOT EXISTS payer_name TEXT;
ALTER TABLE bank_transactions ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'csv';

ALTER TABLE receipts      ENABLE ROW LEVEL SECURITY;   -- same as 003: no public API access
ALTER TABLE payer_aliases ENABLE ROW LEVEL SECURITY;
