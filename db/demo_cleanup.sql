-- Removes the demo data (codes DEMO-*, tenants "Demo …", demo.staff@mlp.test).
-- Run in the Supabase SQL Editor before go-live. Also delete the folder
-- receipts/DEMO-PJT-02/ in Storage. Real data is not touched.
BEGIN;

CREATE TEMP TABLE demo_tenancies ON COMMIT DROP AS
SELECT t.id FROM tenancies t JOIN units u ON u.id = t.unit_id WHERE u.code LIKE 'DEMO-%';

DELETE FROM followups     WHERE tenancy_id IN (SELECT id FROM demo_tenancies);
DELETE FROM reminder_log  WHERE tenancy_id IN (SELECT id FROM demo_tenancies);
UPDATE bank_transactions SET status = 'unmatched', matched_payment_id = NULL
 WHERE matched_payment_id IN (SELECT id FROM payments WHERE tenancy_id IN (SELECT id FROM demo_tenancies));
DELETE FROM payments      WHERE tenancy_id IN (SELECT id FROM demo_tenancies);
DELETE FROM rent_schedule WHERE tenancy_id IN (SELECT id FROM demo_tenancies);
DELETE FROM tenancies     WHERE id IN (SELECT id FROM demo_tenancies);
DELETE FROM bill_checks   WHERE utility_account_id IN
  (SELECT ua.id FROM utility_accounts ua JOIN units u ON u.id = ua.unit_id WHERE u.code LIKE 'DEMO-%');
DELETE FROM utility_accounts WHERE unit_id IN (SELECT id FROM units WHERE code LIKE 'DEMO-%');
DELETE FROM units         WHERE code LIKE 'DEMO-%';
DELETE FROM tenants       WHERE name LIKE 'Demo %' AND phone LIKE '6010000000%';
DELETE FROM landlord_bank WHERE landlord_id IN (SELECT id FROM landlords WHERE code LIKE 'DEMO-%');
DELETE FROM landlords     WHERE code LIKE 'DEMO-%';
UPDATE audit_log SET staff_id = NULL
 WHERE staff_id IN (SELECT id FROM staff WHERE email = 'demo.staff@mlp.test');
DELETE FROM staff         WHERE email = 'demo.staff@mlp.test';

COMMIT;
