CREATE TABLE staff (
  id               SERIAL PRIMARY KEY,
  name             TEXT NOT NULL,
  email            TEXT UNIQUE NOT NULL,          -- stored lowercase
  phone            TEXT,
  role             TEXT NOT NULL DEFAULT 'staff'
                   CHECK (role IN ('admin','staff','viewer')),
  password_hash    TEXT,
  active           BOOLEAN DEFAULT TRUE
);

CREATE TABLE landlords (
  id                 SERIAL PRIMARY KEY,
  code               TEXT UNIQUE NOT NULL,
  name               TEXT NOT NULL,
  phone              TEXT,
  email              TEXT,
  management_fee_pct NUMERIC(5,2) DEFAULT 10.00,
  notes              TEXT
);

CREATE TABLE landlord_bank (                       -- admin only in the portal
  landlord_id  INT PRIMARY KEY REFERENCES landlords(id),
  bank         TEXT,
  account_name TEXT,
  account_no   TEXT
);

CREATE TABLE units (
  id                SERIAL PRIMARY KEY,
  code              TEXT UNIQUE NOT NULL,          -- e.g. SNDN-3418
  address           TEXT,
  area              TEXT,
  unit_type         TEXT,
  landlord_id       INT REFERENCES landlords(id),
  assigned_staff_id INT REFERENCES staff(id),
  status            TEXT DEFAULT 'vacant'
                    CHECK (status IN ('occupied','vacant','inactive')),
  notes             TEXT
);

CREATE TABLE tenants (
  id    SERIAL PRIMARY KEY,
  name  TEXT NOT NULL,
  phone TEXT UNIQUE,                               -- normalised: 60123456789
  email TEXT,
  notes TEXT
);

CREATE TABLE tenancies (
  id              SERIAL PRIMARY KEY,
  unit_id         INT NOT NULL REFERENCES units(id),
  tenant_id       INT NOT NULL REFERENCES tenants(id),
  start_date      DATE NOT NULL,
  end_date        DATE,
  monthly_rent    NUMERIC(10,2) NOT NULL CHECK (monthly_rent > 0),
  due_day         INT NOT NULL DEFAULT 1 CHECK (due_day BETWEEN 1 AND 28),
  deposit_rental  NUMERIC(10,2) DEFAULT 0,
  deposit_utility NUMERIC(10,2) DEFAULT 0,
  status          TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('upcoming','active','ended')),
  notes           TEXT,
  UNIQUE (unit_id, start_date)
);
CREATE UNIQUE INDEX one_active_tenancy_per_unit
  ON tenancies (unit_id) WHERE status = 'active';

CREATE TABLE rent_schedule (
  id         BIGSERIAL PRIMARY KEY,
  tenancy_id INT NOT NULL REFERENCES tenancies(id),
  period     DATE NOT NULL,                        -- always the 1st of the month
  amount_due NUMERIC(10,2) NOT NULL,
  due_date   DATE NOT NULL,
  note       TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (tenancy_id, period),
  CHECK (period = date_trunc('month', period)::date)
);

CREATE TABLE bank_transactions (
  id                 BIGSERIAL PRIMARY KEY,
  account_label      TEXT NOT NULL,
  txn_date           DATE NOT NULL,
  description        TEXT,
  reference          TEXT,
  amount             NUMERIC(12,2) NOT NULL,
  import_batch       TEXT,
  status             TEXT DEFAULT 'unmatched'
                     CHECK (status IN ('unmatched','matched','ignored')),
  matched_payment_id BIGINT,
  row_hash           TEXT UNIQUE,
  imported_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE payments (
  id               BIGSERIAL PRIMARY KEY,
  tenancy_id       INT NOT NULL REFERENCES tenancies(id),
  rent_schedule_id BIGINT REFERENCES rent_schedule(id),
  amount           NUMERIC(10,2) NOT NULL CHECK (amount > 0),
  paid_date        DATE NOT NULL,
  method           TEXT DEFAULT 'bank_transfer'
                   CHECK (method IN ('bank_transfer','duitnow','cash','cheque','other')),
  reference        TEXT,
  receipt_path     TEXT,
  bank_txn_id      BIGINT REFERENCES bank_transactions(id),
  recorded_by      INT REFERENCES staff(id),
  recorded_at      TIMESTAMPTZ DEFAULT NOW(),
  voided           BOOLEAN DEFAULT FALSE,
  void_reason      TEXT,
  voided_by        INT REFERENCES staff(id),
  voided_at        TIMESTAMPTZ
);
ALTER TABLE bank_transactions
  ADD CONSTRAINT bank_txn_payment_fk
  FOREIGN KEY (matched_payment_id) REFERENCES payments(id);

CREATE TABLE followups (
  id               BIGSERIAL PRIMARY KEY,
  tenancy_id       INT NOT NULL REFERENCES tenancies(id),
  rent_schedule_id BIGINT REFERENCES rent_schedule(id),
  staff_id         INT REFERENCES staff(id),
  note             TEXT NOT NULL,
  next_action_date DATE,
  done             BOOLEAN DEFAULT FALSE,
  created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE utility_accounts (
  id         SERIAL PRIMARY KEY,
  unit_id    INT REFERENCES units(id),
  type       TEXT CHECK (type IN ('electric','water')),
  account_no TEXT NOT NULL,
  UNIQUE (type, account_no)
);

CREATE TABLE bill_checks (
  id                 BIGSERIAL PRIMARY KEY,
  utility_account_id INT REFERENCES utility_accounts(id),
  checked_at         TIMESTAMPTZ DEFAULT NOW(),
  amount_due         NUMERIC(10,2),
  outstanding        NUMERIC(10,2),
  due_date           DATE,
  source             TEXT,
  raw_ref            TEXT
);

CREATE TABLE audit_log (
  id         BIGSERIAL PRIMARY KEY,
  staff_id   INT REFERENCES staff(id),
  action     TEXT,
  entity     TEXT,
  entity_id  BIGINT,
  before     JSONB,
  after      JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE message_templates (
  code   TEXT PRIMARY KEY,
  name   TEXT NOT NULL,
  body   TEXT NOT NULL,       -- placeholders: {tenant_name} {staff_name} {unit_code}
  active BOOLEAN DEFAULT TRUE -- {period} {balance} {due_date} {amount} {end_date}
);

CREATE TABLE reminder_log (   -- one row per WhatsApp message a staff member sent
  id               BIGSERIAL PRIMARY KEY,
  tenancy_id       INT NOT NULL REFERENCES tenancies(id),
  rent_schedule_id BIGINT REFERENCES rent_schedule(id),
  staff_id         INT REFERENCES staff(id),
  template_code    TEXT,
  message          TEXT,
  channel          TEXT DEFAULT 'wa_link',
  created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE job_runs (
  id            BIGSERIAL PRIMARY KEY,
  job           TEXT,
  started_at    TIMESTAMPTZ DEFAULT NOW(),
  finished_at   TIMESTAMPTZ,
  status        TEXT,
  rows_affected INT,
  error         TEXT
);

INSERT INTO message_templates (code, name, body) VALUES
('rent_reminder', 'Rent reminder (before due)',
 'Hi {tenant_name}, this is {staff_name} from Miri Landmark Property. A friendly reminder that the rent for {unit_code} for {period} (RM{balance}) is due on {due_date}. Please share the payment slip here once paid. Thank you!'),
('rent_overdue', 'Rent overdue',
 'Hi {tenant_name}, this is {staff_name} from Miri Landmark Property. Our records show the rent for {unit_code} for {period} is still outstanding (RM{balance}, due {due_date}). Kindly arrange payment and share the slip here. If you have already paid, please send us the slip so we can update our records. Thank you.'),
('payment_thanks', 'Payment received',
 'Hi {tenant_name}, we have received your payment of RM{amount} for {unit_code} ({period}). Thank you!'),
('lease_ending', 'Tenancy ending',
 'Hi {tenant_name}, this is {staff_name} from Miri Landmark Property. Your tenancy for {unit_code} ends on {end_date}. Please let us know if you would like to renew. Thank you.');
