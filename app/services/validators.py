"""Shared input validation (used by routes, services and agent tools so the rules are identical everywhere)."""
import re
import secrets
import string

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
PHONE = re.compile(r"^\+?[0-9][0-9 \-]{7,18}$")
STUDENT_ID = re.compile(r"^[A-Za-z0-9\-]{4,20}$")
USERNAME = re.compile(r"^[a-z0-9_.]{3,30}$")
TRIMESTERS = ["Trimester I", "Trimester II", "Trimester III", "Trimester IV", "Trimester V", "Trimester VI"]


class ValidationError(Exception):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def password_problems(pw: str) -> list[str]:
    out = []
    if len(pw) < 10:
        out.append("at least 10 characters")
    if not re.search(r"[a-z]", pw):
        out.append("a lowercase letter")
    if not re.search(r"[A-Z]", pw):
        out.append("an uppercase letter")
    if not re.search(r"\d", pw):
        out.append("a digit")
    if not re.search(r"[^A-Za-z0-9]", pw):
        out.append("a symbol")
    return out


def generate_password(length: int = 14) -> str:
    """Random password that always satisfies the password policy."""
    sets = [string.ascii_lowercase, string.ascii_uppercase, string.digits, "!@#$%&*?"]
    chars = [secrets.choice(s) for s in sets] + [secrets.choice("".join(sets)) for _ in range(length - 4)]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)
