"""Create or reset the initial local accounts and write login.txt (which is git-ignored).

    python scripts/provision_accounts.py                    # create the database if needed; add any missing initial accounts
    python scripts/provision_accounts.py --reset-passwords  # give every initial account a NEW random password

Passwords are random, stored only as hashes in the database, and written in clear text only to login.txt on your machine.
Every password listed is verified against the database before the file is written."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from werkzeug.security import check_password_hash, generate_password_hash  # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import User  # noqa: E402
from app.services.validators import generate_password  # noqa: E402
from data.seed.seed import ACCOUNTS, STUDENTS, write_login_file  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset-passwords", action="store_true", help="set new random passwords for all initial accounts")
    args = ap.parse_args()
    app = create_app()
    with app.app_context():
        creds = {}
        login_file = app.config["LOGIN_FILE"]
        if login_file.exists() and not args.reset_passwords and User.query.count() > 0:
            print(f"{login_file.name} already exists; use --reset-passwords to generate new credentials.")
            return 0
        for a in ACCOUNTS:
            u = User.query.filter_by(username=a["username"]).first()
            if u is None or args.reset_passwords:
                pw = generate_password()
                if u is None:
                    from app.models import StudentProfile
                    if a["student_id"] and not StudentProfile.query.filter_by(student_id=a["student_id"]).first():
                        print(f"skipped {a['username']}: student record {a['student_id']} is missing")
                        continue
                    name = next((s["full_name"] for s in STUDENTS if s["student_id"] == a["student_id"]), "Registrar Office")
                    u = User(username=a["username"], role=a["role"], display_name=name, student_id=a["student_id"], password_hash="x", active=True)
                    db.session.add(u)
                u.password_hash = generate_password_hash(pw)
                u.active = True
                creds[a["username"]] = pw
        db.session.commit()
        if not creds:
            print("All initial accounts already exist. Run with --reset-passwords to write fresh credentials.")
            return 0
        for name, pw in creds.items():   # verify against the database before writing
            assert check_password_hash(User.query.filter_by(username=name).first().password_hash, pw), f"verification failed for {name}"
        write_login_file(creds, login_file)
        print(f"Verified {len(creds)} account(s) against the database and wrote {login_file.name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
