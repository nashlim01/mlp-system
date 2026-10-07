import getpass
import os
from pathlib import Path

import bcrypt
import psycopg
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
email = input("Staff email: ").strip().lower()
pw = getpass.getpass("New password (min 10 chars): ")
if len(pw) < 10:
    raise SystemExit("Too short.")
hashed = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
with psycopg.connect(os.environ["DATABASE_URL"], prepare_threshold=None) as conn:
    n = conn.execute("UPDATE staff SET password_hash = %s WHERE lower(email) = %s",
                     (hashed, email)).rowcount
print("Password set." if n else "No staff with that email (import the register first).")
