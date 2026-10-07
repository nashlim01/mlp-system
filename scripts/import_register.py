"""Import the register workbook.

  python scripts/import_register.py MLP_Register.xlsx            # validate only
  python scripts/import_register.py MLP_Register.xlsx --commit   # write to DB
"""
import os
import re
import sys
from pathlib import Path

import pandas as pd
import psycopg
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
SHEETS = ["Staff", "Landlords", "Units", "Tenants", "Tenancies",
          "Utility_Accounts", "Payment_History"]
METHODS = {"bank_transfer", "duitnow", "cash", "cheque", "other"}


def norm_phone(v):
    d = re.sub(r"\D", "", str(v or ""))
    if d.startswith("0"):
        d = "60" + d[1:]
    return d or None


def to_date(v):
    v = str(v).strip()
    if not v:
        return None
    if re.match(r"\d{4}-\d{2}-\d{2}", v):          # a real Excel date cell, read as "2026-03-01 00:00:00"
        return pd.to_datetime(v).date()           # (dayfirst would turn it into 3 January)
    return pd.to_datetime(v, dayfirst=True).date()


def to_num(v, default=None):
    s = str(v).replace(",", "").replace("RM", "").strip()
    return float(s) if s else default


def read(path):
    book = pd.read_excel(path, sheet_name=None, dtype=str, skiprows=[1])
    out = {}
    for name in SHEETS:
        df = book.get(name, pd.DataFrame()).dropna(how="all").fillna("")
        df.columns = [str(c).replace("*", "").strip() for c in df.columns]
        out[name] = df
    return out


def main(path, commit):
    data, errors, counts = read(path), [], {}

    def err(sheet, i, msg):
        errors.append(f"{sheet} row {i + 3}: {msg}")   # +3 = header + example row

    with psycopg.connect(os.environ["DATABASE_URL"], prepare_threshold=None) as conn:
        with conn.transaction() as outer:

            def run(sheet, i, sql, params, fetch=True):
                try:
                    with conn.transaction():          # savepoint per row
                        cur = conn.execute(sql, params)
                        return cur.fetchone() if fetch else True
                except psycopg.Error as e:
                    err(sheet, i, str(e).splitlines()[0])
                    return None

            staff = dict(conn.execute("SELECT lower(email), id FROM staff").fetchall())
            landlords = dict(conn.execute("SELECT code, id FROM landlords").fetchall())
            units = dict(conn.execute("SELECT code, id FROM units").fetchall())
            tenants = dict(conn.execute("SELECT phone, id FROM tenants").fetchall())

            for i, r in data["Staff"].iterrows():
                email, role = r["email"].strip().lower(), (r["role"] or "staff").strip().lower()
                if not email or not r["staff_name"].strip():
                    err("Staff", i, "staff_name and email are required"); continue
                if role not in ("admin", "staff", "viewer"):
                    err("Staff", i, f"unknown role '{role}'"); continue
                row = run("Staff", i, """
                    INSERT INTO staff (name, email, phone, role) VALUES (%s, %s, %s, %s)
                    ON CONFLICT (email) DO UPDATE
                    SET name = EXCLUDED.name, phone = EXCLUDED.phone, role = EXCLUDED.role
                    RETURNING id""", (r["staff_name"].strip(), email, norm_phone(r["phone"]), role))
                if row: staff[email] = row[0]

            for i, r in data["Landlords"].iterrows():
                code = r["landlord_code"].strip().upper()
                if not code or not r["name"].strip():
                    err("Landlords", i, "landlord_code and name are required"); continue
                row = run("Landlords", i, """
                    INSERT INTO landlords (code, name, phone, email, management_fee_pct)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (code) DO UPDATE
                    SET name = EXCLUDED.name, phone = EXCLUDED.phone, email = EXCLUDED.email,
                        management_fee_pct = EXCLUDED.management_fee_pct
                    RETURNING id""", (code, r["name"].strip(), norm_phone(r["phone"]),
                                      r["email"].strip() or None,
                                      to_num(r["management_fee_pct"], 10.0)))
                if row: landlords[code] = row[0]

            for i, r in data["Units"].iterrows():
                code = r["unit_code"].strip().upper()
                ll = landlords.get(r["landlord_code"].strip().upper())
                sid = staff.get(r["assigned_staff_email"].strip().lower())
                status = (r["status"] or "vacant").strip().lower()
                if not code or not r["address"].strip():
                    err("Units", i, "unit_code and address are required"); continue
                if ll is None: err("Units", i, f"unknown landlord_code '{r['landlord_code']}'"); continue
                if sid is None: err("Units", i, f"unknown staff email '{r['assigned_staff_email']}'"); continue
                if status not in ("occupied", "vacant", "inactive"):
                    err("Units", i, f"unknown status '{status}'"); continue
                row = run("Units", i, """
                    INSERT INTO units (code, address, area, unit_type, landlord_id,
                                       assigned_staff_id, status, notes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (code) DO UPDATE
                    SET address = EXCLUDED.address, area = EXCLUDED.area,
                        unit_type = EXCLUDED.unit_type, landlord_id = EXCLUDED.landlord_id,
                        assigned_staff_id = EXCLUDED.assigned_staff_id,
                        status = EXCLUDED.status, notes = EXCLUDED.notes
                    RETURNING id""", (code, r["address"].strip(), r["area"].strip() or None,
                                      r["unit_type"].strip() or None, ll, sid, status,
                                      r["notes"].strip() or None))
                if row: units[code] = row[0]

            for i, r in data["Tenants"].iterrows():
                phone = norm_phone(r["phone"])
                if not phone or not r["tenant_name"].strip():
                    err("Tenants", i, "tenant_name and phone are required"); continue
                row = run("Tenants", i, """
                    INSERT INTO tenants (name, phone, email, notes) VALUES (%s, %s, %s, %s)
                    ON CONFLICT (phone) DO UPDATE
                    SET name = EXCLUDED.name, email = EXCLUDED.email, notes = EXCLUDED.notes
                    RETURNING id""", (r["tenant_name"].strip(), phone,
                                      r["email"].strip() or None, r["notes"].strip() or None))
                if row: tenants[phone] = row[0]

            active_seen = set()
            for i, r in data["Tenancies"].iterrows():
                uid = units.get(r["unit_code"].strip().upper())
                tid = tenants.get(norm_phone(r["tenant_phone"]))
                status = (r["status"] or "active").strip().lower()
                try:
                    start, end = to_date(r["start_date"]), to_date(r["end_date"])
                    rent, due = to_num(r["monthly_rent"]), int(to_num(r["due_day"], 1))
                except (ValueError, TypeError) as e:
                    err("Tenancies", i, f"bad date or number ({e})"); continue
                if uid is None: err("Tenancies", i, f"unknown unit_code '{r['unit_code']}'"); continue
                if tid is None: err("Tenancies", i, f"unknown tenant_phone '{r['tenant_phone']}'"); continue
                if not start or not rent: err("Tenancies", i, "start_date and monthly_rent are required"); continue
                if not 1 <= due <= 28: err("Tenancies", i, "due_day must be 1-28"); continue
                if status not in ("upcoming", "active", "ended"):
                    err("Tenancies", i, f"unknown status '{status}'"); continue
                if status == "active":
                    if uid in active_seen:
                        err("Tenancies", i, "unit already has an active tenancy in this file"); continue
                    active_seen.add(uid)
                run("Tenancies", i, """
                    INSERT INTO tenancies (unit_id, tenant_id, start_date, end_date, monthly_rent,
                                           due_day, deposit_rental, deposit_utility, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (unit_id, start_date) DO UPDATE
                    SET tenant_id = EXCLUDED.tenant_id, end_date = EXCLUDED.end_date,
                        monthly_rent = EXCLUDED.monthly_rent, due_day = EXCLUDED.due_day,
                        deposit_rental = EXCLUDED.deposit_rental,
                        deposit_utility = EXCLUDED.deposit_utility, status = EXCLUDED.status
                    RETURNING id""", (uid, tid, start, end, rent, due,
                                      to_num(r["deposit_rental"], 0), to_num(r["deposit_utility"], 0),
                                      status))

            conn.execute("""
                UPDATE units u SET status = CASE
                  WHEN EXISTS (SELECT 1 FROM tenancies t
                               WHERE t.unit_id = u.id AND t.status = 'active') THEN 'occupied'
                  ELSE 'vacant' END
                WHERE u.status <> 'inactive'""")

            for i, r in data["Utility_Accounts"].iterrows():
                uid = units.get(r["unit_code"].strip().upper())
                kind, acc = r["type"].strip().lower(), r["account_no"].strip()
                if uid is None: err("Utility_Accounts", i, f"unknown unit_code '{r['unit_code']}'"); continue
                if kind not in ("electric", "water") or not acc:
                    err("Utility_Accounts", i, "type must be electric/water and account_no is required"); continue
                run("Utility_Accounts", i, """
                    INSERT INTO utility_accounts (unit_id, type, account_no) VALUES (%s, %s, %s)
                    ON CONFLICT (type, account_no) DO UPDATE SET unit_id = EXCLUDED.unit_id
                    RETURNING id""", (uid, kind, acc))

            for i, r in data["Payment_History"].iterrows():
                uid = units.get(r["unit_code"].strip().upper())
                try:
                    period = pd.to_datetime(r["period"]).date().replace(day=1)
                    paid_date, amount = to_date(r["paid_date"]), to_num(r["amount_paid"])
                except (ValueError, TypeError) as e:
                    err("Payment_History", i, f"bad date or number ({e})"); continue
                if uid is None or not amount or not paid_date:
                    err("Payment_History", i, "unit_code, amount_paid and paid_date are required"); continue
                ten = conn.execute("""
                    SELECT id, monthly_rent, due_day FROM tenancies
                    WHERE unit_id = %s AND start_date <= (%s::date + INTERVAL '1 month - 1 day')
                      AND (end_date IS NULL OR end_date >= %s)
                    ORDER BY start_date DESC LIMIT 1""", (uid, period, period)).fetchone()
                if not ten:
                    err("Payment_History", i, "no tenancy covers this period"); continue
                run("Payment_History", i, """
                    INSERT INTO rent_schedule (tenancy_id, period, amount_due, due_date)
                    VALUES (%s, %s, %s, make_date(%s, %s, %s))
                    ON CONFLICT (tenancy_id, period) DO NOTHING""",
                    (ten[0], period, ten[1], period.year, period.month, ten[2]), fetch=False)
                sched = conn.execute("SELECT id FROM rent_schedule WHERE tenancy_id = %s AND period = %s",
                                     (ten[0], period)).fetchone()
                dup = conn.execute("""SELECT 1 FROM payments WHERE rent_schedule_id = %s
                                      AND amount = %s AND paid_date = %s AND NOT voided""",
                                   (sched[0], amount, paid_date)).fetchone()
                if dup:
                    continue
                method = (r["method"] or "other").strip().lower()
                run("Payment_History", i, """
                    INSERT INTO payments (tenancy_id, rent_schedule_id, amount, paid_date,
                                          method, reference)
                    VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                    (ten[0], sched[0], amount, paid_date,
                     method if method in METHODS else "other", r["reference"].strip() or "import"))

            for name in SHEETS:
                counts[name] = len(data[name])
            print("Rows read:", counts)
            if errors:
                print(f"\n{len(errors)} problem(s) found. Nothing was saved:")
                print("\n".join(errors))
                raise psycopg.Rollback(outer)
            if not commit:
                print("\nValidation OK. Run again with --commit to save.")
                raise psycopg.Rollback(outer)
            print("\nImported and committed.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1], "--commit" in sys.argv)
