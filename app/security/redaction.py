"""Secret detection and redaction. Runs BEFORE a message is stored, sent to the LLM, or considered for memory.
Deliberately conservative: it prefers to over-redact."""
import re

REDACTED = "[REDACTED]"

_OTP = re.compile(r"(?i)\b(otp|one[- ]?time[- ]?(?:password|passcode|code|pin)|verification code|security code|passcode)\b"
                  r"(?P<gap>[^0-9\n]{0,25}?)(?P<val>\d{4,8})\b")
_PASSWORD = re.compile(r"(?i)\b(?P<k>password|passwd|pwd|passphrase)\b(?P<sep>\s*(?:is|was|are|=|:|-)\s*|\s+)(?P<val>\S+)")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_CVV = re.compile(r"(?i)\b(?P<k>cvv2?|cvc)\b(?P<sep>\s*(?:is|=|:)?\s*)(?P<val>\d{3,4})\b")
_PIN = re.compile(r"(?i)\b(?P<k>pin)\b(?P<sep>\s*(?:is|=|:)?\s*)(?P<val>\d{4,6})\b")
_API_KEY = re.compile(r"\b(?:gsk_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9_\-]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b")
_TOKEN = re.compile(r"(?i)\b(?P<k>api[_ -]?key|secret|token|bearer)\b(?P<sep>\s*(?:is|=|:)\s*)(?P<val>\S+)")

_PASSWORD_STOP = {"reset", "change", "expired", "policy", "manager", "and", "or", "the", "to", "for", "but", "so", "when", "again",
                  "recovery", "portal", "forgot", "link", "please", "rules", "requirements", "not", "does", "doesn", "doesnt"}


def redact(text: str) -> tuple[str, list[str]]:
    """Return (clean_text, findings). `findings` lists the kinds of secret that were removed."""
    findings: list[str] = []

    def sub_group(pattern, label, group="val", keep_filter=None):
        nonlocal text

        def repl(m):
            val = m.group(group)
            if keep_filter and keep_filter(m, val):
                return m.group(0)
            findings.append(label)
            return m.group(0).replace(val, REDACTED, 1) if group in m.groupdict() else REDACTED
        text = pattern.sub(repl, text)

    def password_keep(m, val):
        has_sep = bool(re.search(r"is|was|are|=|:|-", m.group("sep")))
        looks_secret = bool(re.search(r"\d|[^A-Za-z]", val.strip(".,;")))
        return not (has_sep or looks_secret) or val.lower().strip(".,;") in _PASSWORD_STOP

    sub_group(_OTP, "otp")
    sub_group(_PASSWORD, "password", keep_filter=password_keep)
    sub_group(_CVV, "card_cvv")
    sub_group(_PIN, "pin")
    sub_group(_TOKEN, "token")
    text = _API_KEY.sub(lambda m: (findings.append("api_key"), REDACTED)[1], text)
    text = _CARD.sub(lambda m: (findings.append("card_number"), REDACTED)[1], text)
    return text, sorted(set(findings))


def contains_secret(text: str) -> bool:
    return bool(redact(text)[1])
