# mlp-system

Rental management system for **Miri Landmark Property Enterprise**.

**Phase 1 (this prototype)** covers:

- Rental register (units, landlords, tenants, tenancies)
- Automatic monthly rent lines
- Payment log with receipts
- Staff portal, with a **Today** page as each staff member's daily list
- One-click **WhatsApp reminders** from the company number (click-to-chat, no API)
- Bank statement matching
- Daily Sarawak Energy bill check

| Document (in `docs/`) | Use it for |
|---|---|
| **`MLP_Phase1_System_Guide.docx`** | **Start here.** The system as built: status, staff how-to, technical reference (v2.0) |
| `MLP_Automation_Proposal.docx` | Scope, phases, decisions for the owner |
| `MLP_Budget_Plan.docx` | Costs and hosting options |
| `MLP_Prototype_Setup_Guide.docx` | Original build guide v1.1 (history; the System Guide lists what changed since) |
| `MLP_Technical_Brief.docx` | Early design, v0.3 (n8n-based; superseded for Phase 1) |
| `MLP_WhatsApp_Bot_Local_Setup.docx` | **Phase 2 reference** (tenant WhatsApp, receipts, n8n) |

---

## Architecture

```
ALPHA (now)                                   PRODUCTION (later)
Staff browser                                 Staff browser
  │ Streamlit viewer allowlist                  │ Cloudflare Access → Cloudflare Tunnel
  ▼                                             ▼
portal (Streamlit Community Cloud)            portal + worker (Docker on Singapore VPS)
  │                                             │
  └──► Supabase (Postgres + Storage, Singapore) ◄┘
         ├─ pg_cron: rent lines
         └─ ◄── GitHub Actions: electric_check (alpha; Playwright → Sarawak Energy)

Staff PC: "💬 Open in WhatsApp" (wa.me link) ─► WhatsApp Web (company number) ─► tenant
```

Phase 1 does **not** include n8n, the WhatsApp Cloud API (automatic sending), AI receipt reading, Google Drive, SQL Account or Sarawak Water. Those arrive in Phases 2–4.

---

## Repository structure

```
mlp-system/
├─ docker-compose.yml
├─ .env.example
├─ .github/workflows/    electric_check.yml   (daily electricity check for the alpha)
├─ db/migrations/        001_core … 006_rent_cron   (run in number order)
├─ db/demo_cleanup.sql   removes the DEMO- sample data
├─ portal/               app.py auth.py db.py storage.py messages.py bank.py views/ requirements.txt Dockerfile
│                        views: today reminders rent_board record_payment tenancy register
│                               utilities reports bank_matching admin account
├─ worker/               scheduler.py common.py jobs/{rent,electric}.py requirements.txt Dockerfile
├─ scripts/              import_register.py  set_password.py  make_template.py  requirements.txt
├─ templates/            MLP_Register_Template.xlsx   (rebuild with scripts/make_template.py)
├─ tests/                smoke_test.py  fixtures/
├─ raw/  state/          runtime data (git-ignored)
└─ docs/
```

`.gitignore` must include `.env`, `raw/`, `state/`, `.venv/` and any filled-in register workbooks.

---

## Quick start (local)

### 1. Configure

```bash
git clone git@github.com:nashlim01/mlp-system.git
cd mlp-system
cp .env.example .env
```

Fill in `.env` (see [Environment variables](#environment-variables)).

### 2. Database (Supabase)

1. Create a project in the **Singapore** region.
2. Copy the **Session pooler** connection string (with `sslmode=require`) into `DATABASE_URL`.
3. Copy the project URL and the `service_role` key into `.env`.
4. Create a **private** Storage bucket named `receipts`.
5. In the SQL Editor, run the files in `db/migrations/` in number order (`001_core.sql` … `006_rent_cron.sql`).

### 3. Rebuild the register

Each staff member fills in `templates/MLP_Register_Template.xlsx` for their own units:

- Row 2 is an example and is skipped on import.
- No IC numbers.
- No landlord bank details (the admin enters those in the portal).

Then validate and import:

```bash
python -m venv .venv && source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r scripts/requirements.txt
python scripts/import_register.py MLP_Register.xlsx           # validate: lists problems by sheet/row
python scripts/import_register.py MLP_Register.xlsx --commit  # save (safe to re-run)
python scripts/set_password.py                                # first admin only; others use the portal
```

Create this month's and next month's rent lines in the SQL Editor:

```sql
SELECT generate_rent_schedule(date_trunc('month', today_myt())::date);
SELECT generate_rent_schedule((date_trunc('month', today_myt()) + INTERVAL '1 month')::date);
```

### 4. Run

```bash
# Containers (same as production)
docker compose up --build portal worker          # http://localhost:8501

# Or plain Python while coding
pip install -r portal/requirements.txt -r worker/requirements.txt
playwright install chromium
cd portal && streamlit run app.py
cd worker && python scheduler.py --run rent_this_month   # run any job once
```

Available jobs are `rent_next_month`, `rent_this_month` and `electric_check`.

### 5. WhatsApp reminders (click-to-chat)

1. Put the company number on the **WhatsApp Business** app and fill in the business profile.
2. On each staff PC, open web.whatsapp.com. On the company phone, go to **Linked devices** and scan the QR code. Use the same browser as the portal.
3. In the portal, the **Reminder Queue** works like this:
   - 💬 opens `https://wa.me/<tenant phone>?text=<message>` with the reminder pre-filled.
   - The staff member presses Send in WhatsApp.
   - **✓ Sent** logs the reminder in `reminder_log`.
4. Edit the message wording in `message_templates` on the Admin page.

**Never** automate WhatsApp Web, for example with bots that press Send. It breaks WhatsApp's terms and risks the company number. Automatic sending comes in Phase 2 through the official Cloud API.

### 6. Sarawak Energy scraper

1. Confirm whether one portal login holds all the electricity accounts.
2. Record the flow with `playwright codegen <SEB_LOGIN_URL>`.
3. Replace every `TODO` selector in `worker/jobs/electric.py`.
4. Test parsing offline before running the full job:
   ```bash
   cd worker && python -m jobs.electric raw/electric/<date>
   ```

---

## Alpha hosting: Streamlit Community Cloud

For the alpha the portal runs on [Streamlit Community Cloud](https://share.streamlit.io) (free, redeploys
on every push to `main`). The VPS + Cloudflare setup below is the later production step; nothing in
the repo has to change for it.

1. Sign in to share.streamlit.io with GitHub and click **Create app → Deploy a public app from GitHub**.
2. Repository `nashlim01/mlp-system`, branch `main`, main file path `portal/app.py`.
3. **Advanced settings:** Python 3.12, and paste the secrets (same values as `.env`):
   ```toml
   DATABASE_URL = "postgresql://postgres.<ref>:<db-password>@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres?sslmode=require"
   SUPABASE_URL = "https://<ref>.supabase.co"
   SUPABASE_SERVICE_KEY = "sb_secret_..."
   ```
4. After the first deploy: app **Settings → Sharing → Only specific people can view this app**, and
   add each staff email. This is the outer gate that Cloudflare Access gives on the VPS.

What runs where in the alpha (there is no worker container):

| Job | Runs in |
|---|---|
| Rent lines (`rent_next_month`, `rent_this_month`) | Supabase `pg_cron` (migration 006); this month's lines are checked daily at 00:30 |
| `electric_check` | GitHub Actions, `.github/workflows/electric_check.yml`, daily 07:00; skipped until its secrets are set |

Limits to know: the app sleeps after a period without visitors (about 30 s to wake), servers are in
the US (pages are slower than from Singapore), and the address is `<name>.streamlit.app`.

### Staff accounts

- New staff use **Request access** on the login page; an admin approves (choosing the role) or rejects
  it under **Admin → Access requests**. On Streamlit Cloud, also add their email to the viewer list.
- Everyone changes their own password under **My account**.
- Admins can create accounts directly (with an initial password) and **reset** a forgotten password
  under **Admin → Staff**. `scripts/set_password.py` still works for the very first admin.

---

## Deploy (VPS + Cloudflare)

1. Create an Ubuntu 24.04 VPS (Singapore, 2GB).
2. Install Docker with `curl -fsSL https://get.docker.com | sh`, then `ufw allow OpenSSH && ufw enable`.
3. Clone the repo, copy `.env` over, and create `raw/` and `state/`.
4. In Cloudflare Zero Trust:
   - Create a tunnel and put its token in `TUNNEL_TOKEN`.
   - Add the public hostname `portal.<domain>` pointing to `http://portal:8501`.
   - Add an Access application for that hostname that allows staff emails only (One-time PIN).
5. Set `PORTAL_URL=https://portal.<domain>`.
6. Start everything:
   ```bash
   docker compose --profile deploy up -d --build
   ```

To update later:

```bash
git pull && docker compose --profile deploy up -d --build
```

Run any new SQL migrations in Supabase **before** deploying code that depends on them.

---

## Environment variables

| Variable | Used by | Notes |
|---|---|---|
| `DATABASE_URL` | portal, worker, scripts | Supabase **Session pooler** URL, `sslmode=require` |
| `SUPABASE_URL` | portal | `https://<ref>.supabase.co` |
| `SUPABASE_SERVICE_KEY` | portal | service_role key, server-side only (receipt storage) |
| `SEB_LOGIN_URL` / `SEB_USERNAME` / `SEB_PASSWORD` | worker | Sarawak Energy portal; the job is skipped if the URL is empty |
| `TUNNEL_TOKEN` | cloudflared | Deployment only |

---

## Schedule (Asia/Kuching)

| Job | When | What |
|---|---|---|
| `rent_next_month` | 25th, 8:00am | Create next month's rent lines |
| `rent_this_month` | 1st, 12:30am | Safety net |
| `electric_check` | Daily, 7:00am | Scrape and load Sarawak Energy bills |
Every run is logged in `job_runs`. Failures appear under **System health** on the admin's Today page, since there are no push alerts in Phase 1.

---

## Ground rules (read before changing code)

1. **Expect, then record.** Rent lines exist before payments arrive. Status (PAID / PARTIAL / DUE / OVERDUE) is always computed in `v_rent_status` and never stored.
2. **Nothing silently changes.**
   - Payments are voided with a reason, never deleted.
   - Every edit writes to `audit_log`.
   - Decisions are conditional updates (e.g. `WHERE status = 'unmatched'`).
3. **Automation suggests, staff confirm.**
   - Bank matches are suggestions until a staff member confirms them.
   - WhatsApp messages are prepared by the portal but sent by a person.
4. **Idempotent everything.** Importer, rent generation, bank uploads and jobs are all safe to run twice.
5. **Fetch, then parse.** The scraper saves raw HTML first, so parsers can be fixed and re-run offline.
6. **Read-only toward outside systems.**
7. **Least data.** No IC numbers. Landlord bank details are admin-only. Receipts sit in a private bucket and open only through short-lived signed links.
8. **Secrets stay out of git.**

---

## Tests

The Setup Guide §10 lists 17 checks. The most important ones:

- Re-importing the register creates no duplicates.
- Running `generate_rent_schedule` twice creates no extra lines.
- Duplicate payments trigger a warning.
- Voided payments are excluded from totals.
- Re-uploading a bank CSV adds nothing.
- The electricity job saves good accounts even when one account fails.
- Non-staff emails are blocked by Cloudflare Access.
- The 💬 link opens the right chat with the message intact, and ✓ Sent logs the reminder.

`tests/smoke_test.py` runs the checks that don't need a browser (import, rent schedule, payment
statuses, bank CSV parsing and re-upload, wa.me links, rent and electricity jobs). It **drops and
recreates** the schema of the database you give it, so point it at a throwaway local Postgres, never Supabase:

```bash
docker run -d --name mlp-test -p 55432:5432 -e POSTGRES_HOST_AUTH_METHOD=trust postgres:17
MLP_TEST_DATABASE_URL=postgresql://postgres@localhost:55432/postgres python tests/smoke_test.py
```

### Bank CSV formats

Mappings live in `BANK_FORMATS` in `portal/bank.py` (credit column, signed amount, or amount + DR/CR).
Until the bank is confirmed, pick **Custom** on the Bank Matching page: map the columns of a real
statement there, and the page prints the entry to paste into `BANK_FORMATS`. Keep the label the
same for every upload of an account, because it is part of the duplicate check.

---

## Common issues

| Symptom | Fix |
|---|---|
| Supabase connection timeout | Use the Session pooler (IPv4), not the direct connection |
| "prepared statement already exists" | Keep `prepare_threshold=None`; don't use the transaction pooler (6543) |
| Login always fails | Run `scripts/set_password.py` for that email |
| Pages visible before login | Pages must live in `portal/views/`, not `portal/pages/` |
| 💬 opens the wrong WhatsApp account | The browser is logged into a personal WhatsApp Web; link the company number instead |
| "Number isn't on WhatsApp" | The tenant phone is wrong or not normalised (it must start with `60`); fix it in Register |
| Playwright browser missing | Run `playwright install chromium`; the Docker image tag must match the pip version |
| Electricity parse breaks | The portal layout changed: update selectors using the saved HTML in `raw/electric/` |

---

## Backups

The Supabase free plan has no automatic backups, so during the prototype take a weekly dump and store it off the server:

```bash
docker run --rm -v "$PWD":/backup postgres:17 pg_dump "$DATABASE_URL" -Fc -f /backup/mlp_$(date +%F).dump
```

Download the `receipts` bucket monthly. Upgrade to Supabase Pro at go-live for daily backups.

---

## Status

- [ ] P1 Register imported, validation clean
- [ ] P2 Rent lines for this and next month; payments recorded in the portal
- [ ] P3 Today page and Reminder Queue used for 2 weeks; every overdue rent has a logged reminder or follow-up
- [ ] P4 One month's bank statement fully matched or explained
- [ ] P5 Electricity: 7 clean daily runs
- [ ] P6 Parallel run done; sheet retired for rent tracking

## Roadmap

- **Phase 2: WhatsApp automation.**
  - Add n8n and the Cloud API, and switch the same Reminder Queue buttons to direct sending (bulk, scheduled, with delivery status).
  - Add receipt intake with AI and a review queue, routed by assigned staff.
  - Add optional push alerts to staff.
- **Phase 3: Landlords & accounting.** Payout list (rent minus fee), owner statements, SQL Account extraction.
- **Phase 4: Water & maintenance.** Sarawak Water app bot (lists all linked accounts, stays signed in; check readability with `adb shell uiautomator dump`), maintenance tickets.
