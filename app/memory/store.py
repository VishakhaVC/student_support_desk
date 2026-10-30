"""Persistent, per-student long-term memory.

Design rules (conservative on purpose):
  * Only an ALLOWLIST of keys can ever be stored (language, response style, preferred name, programme).
  * Facts are extracted by explicit rules from the student's message - the LLM never writes to memory.
  * Any message in which a secret was detected is skipped entirely.
  * One value per key, so a newer fact replaces a stale one.
  * Every query is scoped by user_id, so one student can never read another student's memory.
"""
import re

from ..extensions import db
from ..models import MemoryItem
from ..retrieval.policy import tokenize
from ..security.redaction import contains_secret

ALLOWED_KEYS = {
    "preferred_language": "Language the student wants replies in",
    "response_style": "Preferred answer style (short / detailed ...)",
    "preferred_name": "How the student likes to be addressed",
    "programme": "Programme the student says they are in",
}
ALWAYS_INCLUDE = {"preferred_language", "response_style", "preferred_name"}
LANGUAGES = {"english", "hindi", "gujarati", "marathi", "tamil", "telugu", "bengali", "kannada", "punjabi", "urdu"}
STYLES = {"short", "brief", "concise", "detailed", "simple", "step-by-step"}

_LANG_A = re.compile(r"(?i)\b(?:reply|respond|answer|talk|speak|write|explain)\w*(?:\s+to me)?\s+in\s+([a-z]+)")
_LANG_B = re.compile(r"(?i)\b(?:prefer\w*|favou?rite|preferred)\s+(?:language\s+(?:is\s+)?|(?:replies|answers|responses)\s+in\s+|in\s+)([a-z]+)")
_STYLE = re.compile(r"(?i)\b(?:prefer\w*|like|want)\s+(?:me to get\s+)?(short|brief|concise|detailed|simple|step[- ]by[- ]step)\b")
_STYLE_B = re.compile(r"(?i)\b(short|brief|concise|detailed|simple)\s+(?:answers|replies|responses|explanations)\b")
_NAME = re.compile(r"\b(?i:call me|my preferred name is)\s+([A-Z][a-z]{1,19})\b")
_PROGRAMME = re.compile(r"(?i)\bi(?:'m| am)\s+(?:currently\s+)?(?:in|enrolled in|studying|pursuing)\s+(?:the\s+)?(.{3,70}?)\s+(?:programme|program)\b")


def extract_candidates(text: str) -> list[tuple[str, str]]:
    """Rule-based extraction of allowlisted facts. Returns [(key, value)]."""
    found: dict[str, str] = {}
    for rx in (_LANG_A, _LANG_B):
        m = rx.search(text)
        if m and m.group(1).lower() in LANGUAGES:
            found["preferred_language"] = m.group(1).capitalize()
            break
    m = _STYLE.search(text) or _STYLE_B.search(text)
    if m:
        v = m.group(1).lower().replace(" ", "-")
        if v in STYLES:
            found["response_style"] = v
    m = _NAME.search(text)
    if m:
        found["preferred_name"] = m.group(1)
    m = _PROGRAMME.search(text)
    if m:
        found["programme"] = re.sub(r"\s+", " ", m.group(1)).strip(" .,")
    return list(found.items())


def _valid(key: str, value: str) -> bool:
    if key not in ALLOWED_KEYS or not value or len(value) > 100:
        return False
    if contains_secret(value) or "@" in value or "<" in value:
        return False
    if key != "programme" and re.search(r"\d", value):
        return False
    return True


def save(user_id: int, key: str, value: str) -> bool:
    if not _valid(key, value):
        return False
    item = MemoryItem.query.filter_by(user_id=user_id, key=key).first()
    if item:
        item.value = value
    else:
        db.session.add(MemoryItem(user_id=user_id, key=key, value=value))
    db.session.commit()
    return True


def process_message(user_id: int, clean_text: str, had_secret: bool) -> dict:
    """Called after each turn. Returns a trace-friendly description of what happened."""
    if had_secret:
        return {"saved": [], "blocked": "message contained sensitive data; nothing from it was considered for memory"}
    saved = [{"key": k, "value": v} for k, v in extract_candidates(clean_text) if save(user_id, k, v)]
    return {"saved": saved, "blocked": None}


def list_items(user_id: int) -> list[MemoryItem]:
    return MemoryItem.query.filter_by(user_id=user_id).order_by(MemoryItem.key).all()


def delete(user_id: int, key: str | None = None) -> int:
    q = MemoryItem.query.filter_by(user_id=user_id)
    if key:
        q = q.filter_by(key=key)
    n = q.delete()
    db.session.commit()
    return n


def select_relevant(user_id: int, query: str, limit: int = 5) -> list[dict]:
    """Selective retrieval: preferences are tiny and apply to every reply; other facts only when the question touches them."""
    q_tokens = set(tokenize(query))
    out = []
    for item in list_items(user_id):
        if item.key in ALWAYS_INCLUDE:
            reason = "preference applies to every reply"
        elif item.key == "programme" and q_tokens & {"programme", "program", "course", "degree", "told", "earlier", "remember", "enrol", "enroll"}:
            reason = "question mentions programme or earlier conversation"
        else:
            continue
        out.append({"key": item.key, "value": item.value, "reason": reason})
    return out[:limit]
