-- Access requests: a new staff member asks for an account from the login page;
-- an admin approves (creates the staff row) or rejects it on the Admin page.
CREATE TABLE staff_requests (
  id             BIGSERIAL PRIMARY KEY,
  name           TEXT NOT NULL,
  email          TEXT NOT NULL,                  -- stored lowercase
  phone          TEXT,
  password_hash  TEXT NOT NULL,                  -- chosen by the requester, copied to staff on approval
  requested_at   TIMESTAMPTZ DEFAULT NOW(),
  status         TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','approved','rejected')),
  decided_by     INT REFERENCES staff(id),
  decided_at     TIMESTAMPTZ,
  decision_note  TEXT
);
-- one open request per email
CREATE UNIQUE INDEX one_pending_request_per_email
  ON staff_requests (lower(email)) WHERE status = 'pending';

ALTER TABLE staff_requests ENABLE ROW LEVEL SECURITY;   -- same as 003: no public API access
