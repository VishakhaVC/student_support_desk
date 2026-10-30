"""Application configuration. Every secret comes from the environment (.env), never from source."""
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


class Config:
    # If FLASK_SECRET_KEY is missing we generate a random one per process (sessions reset on restart)
    # rather than shipping a guessable default.
    SECRET_KEY = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///student_support.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    DEBUG = os.getenv("FLASK_DEBUG", "false").lower() == "true"

    # --- Session / cookie hardening ---
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"  # set true behind HTTPS
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 4
    LOGIN_MAX_FAILURES = 5
    LOGIN_LOCK_MINUTES = 5

    # --- LLM provider (Groq) ---
    LLM_MODE = os.getenv("LLM_MODE", "groq")  # "groq" | "deterministic"
    GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
    GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    GROQ_TIMEOUT_SECONDS = float(os.getenv("GROQ_TIMEOUT_SECONDS", "30"))
    GROQ_MAX_RETRIES = int(os.getenv("GROQ_MAX_RETRIES", "2"))

    # --- Agent safety limits ---
    AGENT_MAX_ITERATIONS = 6
    AGENT_MAX_TOOL_CALLS = 8
    AGENT_TIMEOUT_SECONDS = 60.0

    # --- Context budget (tokens are estimated as chars/4 before the API reports real usage) ---
    CONTEXT_HISTORY_MESSAGES = 6
    CONTEXT_HISTORY_TOKEN_BUDGET = 1200
    CONTEXT_MEMORY_MAX_ITEMS = 5
    POLICY_TOP_K = 3

    MAX_MESSAGE_CHARS = 1000
    POLICY_FILE = BASE_DIR / "data" / "sample_policy" / "student_handbook.md"
    RESULTS_DIR = BASE_DIR / "evaluations" / "results"
    LOGIN_FILE = Path(os.getenv("LOGIN_FILE", str(BASE_DIR / "login.txt")))


class TestConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SECRET_KEY = "test-only-secret"
    LLM_MODE = "deterministic"
    GROQ_API_KEY = ""
