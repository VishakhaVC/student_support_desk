"""LLM providers. Both implement chat(messages, tools) -> ProviderResponse so the agent loop is provider-agnostic.

  * GroqProvider          - the real LLM (official `groq` SDK, native tool calling).
  * DeterministicProvider - a clearly labelled, rule-based TEST DOUBLE used for repeatable automated tests.
                            It is NOT an LLM, reports no token usage, and must never be presented as one.
"""
import json
import re
import time
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string, exactly as produced by the model


@dataclass
class ProviderResponse:
    content: str
    tool_calls: list = field(default_factory=list)
    usage: dict | None = None  # {"input_tokens", "output_tokens", "total_tokens"} or None if unavailable
    model: str = ""
    finish_reason: str | None = None


class ProviderError(Exception):
    """kind: not_configured | auth | rate_limit | timeout | connection | invalid_tool_call | model_unavailable | bad_request | server"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


class GroqProvider:
    name = "groq"

    def __init__(self, api_key: str, model: str, timeout: float = 30.0, max_retries: int = 2, sleep=time.sleep):
        if not api_key:
            raise ProviderError("not_configured", "GROQ_API_KEY is not set. Copy .env.example to .env and add your key.")
        import groq  # imported lazily so tests that never use Groq do not need network libs initialised
        self._groq = groq
        self.client = groq.Groq(api_key=api_key, timeout=timeout, max_retries=0)  # retries are handled (and traced) here
        self.model, self.max_retries, self._sleep = model, max_retries, sleep

    def chat(self, messages, tools) -> ProviderResponse:
        g = self._groq
        last_err = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.client.chat.completions.create(model=self.model, messages=messages, tools=tools,
                                                           tool_choice="auto", temperature=0)
                return self._convert(resp)
            except g.AuthenticationError as e:
                raise ProviderError("auth", "Groq rejected the API key.") from e
            except g.PermissionDeniedError as e:
                raise ProviderError("auth", "This API key is not allowed to use the selected model.") from e
            except g.NotFoundError as e:
                raise ProviderError("model_unavailable", f"Model '{self.model}' was not found. Set GROQ_MODEL to a currently supported model.") from e
            except g.RateLimitError as e:
                last_err = ProviderError("rate_limit", "Groq rate limit reached.")
                self._sleep(min(self._retry_after(e, default=2 ** attempt), 10))
            except g.APITimeoutError:
                last_err = ProviderError("timeout", "The model request timed out.")
            except g.APIConnectionError:
                last_err = ProviderError("connection", "Could not reach the Groq API.")
                self._sleep(min(2 ** attempt, 4))
            except g.BadRequestError as e:
                text = str(e).lower()
                if "tool_use_failed" in text or "failed to call a function" in text or "failed_generation" in text:
                    raise ProviderError("invalid_tool_call", "The model produced an invalid tool call.") from e
                raise ProviderError("bad_request", "Groq rejected the request.") from e
            except g.APIStatusError as e:
                if e.status_code >= 500:
                    last_err = ProviderError("server", "Groq reported a server error.")
                    self._sleep(min(2 ** attempt, 4))
                else:
                    raise ProviderError("bad_request", f"Groq returned HTTP {e.status_code}.") from e
        raise last_err

    @staticmethod
    def _retry_after(exc, default):
        try:
            return float(exc.response.headers.get("retry-after", default))
        except Exception:
            return default

    def _convert(self, resp) -> ProviderResponse:
        choice = resp.choices[0]
        msg = choice.message
        calls = [ToolCall(id=tc.id, name=tc.function.name, arguments=tc.function.arguments or "{}")
                 for tc in (getattr(msg, "tool_calls", None) or [])]
        usage = None
        u = getattr(resp, "usage", None)
        if u is not None and getattr(u, "total_tokens", None) is not None:
            usage = {"input_tokens": u.prompt_tokens, "output_tokens": u.completion_tokens, "total_tokens": u.total_tokens}
        return ProviderResponse(content=msg.content or "", tool_calls=calls, usage=usage, model=getattr(resp, "model", self.model),
                                finish_reason=choice.finish_reason)


# ----------------------------------------------------------------------------------------------------------------------
class DeterministicProvider:
    """Rule-based stand-in for an LLM. It plans tool calls from keywords and writes answers from tool observations.
    Used only for automated tests / reproducible harness checks. Reports usage=None (no tokens are consumed)."""
    name = "deterministic"
    model = "deterministic-rules-v1 (NOT an LLM)"

    def chat(self, messages, tools) -> ProviderResponse:
        idx = max(i for i, m in enumerate(messages) if m["role"] == "user")
        text = messages[idx]["content"]
        observed = [json.loads(m["content"]) | {"_tool": m.get("name")} for m in messages[idx + 1:] if m["role"] == "tool"]
        if observed:
            return ProviderResponse(self._compose(observed, text), model=self.model, finish_reason="stop")
        calls = self._plan(text)
        if calls:
            return ProviderResponse("", [ToolCall(f"call_{i}", n, json.dumps(a)) for i, (n, a) in enumerate(calls)],
                                    model=self.model, finish_reason="tool_calls")
        return ProviderResponse(self._direct(text, messages[0]["content"]), model=self.model, finish_reason="stop")

    # ---- planning (the "decision") ----
    def _plan(self, text: str):
        t = text.lower()
        email = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
        phone = re.search(r"\+?\d[\d \-]{8,16}\d", text)
        if re.search(r"another student|someone else|student\s*b\b|s2026\d{3}|classmate|other student", t):
            return [("get_my_fee_status", {"student_id": "S2026002"})]            # simulated unsafe attempt
        if re.search(r"approve\b.{0,25}(request|approval|change)|pending approvals", t):
            return [("approve_approval_request", {"approval_ref": "APR-0001"})]    # simulated unauthorized tool use
        if re.search(r"(change|update|set).{0,25}(email|phone|contact)", t):
            if email:
                return [("request_profile_change", {"field": "email", "new_value": email.group(0)})]
            if phone and "email" not in t:
                return [("request_profile_change", {"field": "phone", "new_value": phone.group(0).strip()})]
            return []                                                                # missing information -> ask
        if re.search(r"(create|raise|open|file|log|submit|make).{0,30}(support )?(request|ticket)", t):
            cat = ("payment" if re.search(r"pay|fee|portal", t) else "scholarship" if "scholar" in t else
                   "course_change" if "course" in t or "elective" in t else "other")
            return [("create_support_ticket", {"category": cat, "subject": f"{cat.replace('_', ' ').title()} issue reported via assistant",
                                               "description": text.strip()[:500]})]
        if re.search(r"(my|show|list|view).{0,20}(tickets|support requests|requests)", t) and "create" not in t:
            return [("get_my_support_tickets", {})]
        if re.search(r"(portal|payment).{0,30}(fail|declin|error)|next working day|debited|late fee|deadline|refund|hostel", t):
            return [("search_student_policy", {"query": text.strip()[:200]})]
        if re.search(r"course change|change (my )?(course|elective)|switch (course|elective)|escalat|academic support|handbook|policy", t):
            return [("search_student_policy", {"query": text.strip()[:200]})]
        if "scholar" in t:
            calls = [("get_my_scholarship_status", {})]
            if re.search(r"eligib|criteria|condition|renew|qualif", t):
                calls.append(("search_student_policy", {"query": "scholarship eligibility conditions"}))
            return calls
        if re.search(r"\bfee|tuition|outstanding|dues|balance|installment|instalment", t):
            return [("get_my_fee_status", {})]
        return []

    # ---- answering ----
    def _direct(self, text: str, system: str) -> str:
        t = text.lower()
        prefs = dict(re.findall(r"^- (\w+): (.+)$", system, re.M))
        if re.search(r"\b(prefer|call me|reply in|respond in|i'm in|i am in)\b", t) and not re.search(r"\b(which|what|did i|do i)\b", t):
            return "Noted. I'll remember that for future conversations."
        if re.search(r"language|reply in|respond in", t) and "preferred_language" in prefs:
            return f"You told me earlier that you prefer replies in {prefs['preferred_language']}."
        if re.search(r"style|short|detailed", t) and "response_style" in prefs:
            return f"You asked me to keep answers {prefs['response_style']}."
        if re.search(r"programme|program|course|enrolled", t) and "programme" in prefs:
            return f"You told me you are in the {prefs['programme']} programme."
        if re.search(r"(change|update).{0,25}(email|phone)", t):
            what = "phone number" if "phone" in t and "email" not in t else "email address"
            return f"Sure. What is the new {what} you would like to register?"
        if re.search(r"\botp\b|password|\bpin\b|\bcvv\b|card", t):
            return "I can't use or store passwords, OTPs or card details, so I've ignored that. Please keep them private."
        if re.search(r"help|what can you|who are you|hello|\bhi\b|\bhey\b", t):
            return "I can help with your fee status, scholarship status, handbook policies (payment problems, course changes), and support requests."
        return "That is outside what I can help with. I can help with fees, scholarships, course changes, policy questions and support requests."

    def _compose(self, observed: list, text: str) -> str:
        parts = []
        for o in observed:
            if o.get("ok") is False:
                code = o.get("error")
                if code in ("unknown_tool", "forbidden", "identity_override_blocked", "permission_denied"):
                    parts.append("I can only act for you, on your own records. That request was blocked.")
                elif code == "tool_failure":
                    parts.append("I couldn't retrieve that right now because of a system error. Please try again later or raise a support request.")
                else:
                    parts.append(f"I couldn't complete that: {o.get('message', 'unknown error')}")
                continue
            if "fee" in o:
                f = o["fee"]
                parts.append(f"Your {f['term']} fee is INR {f['total_fee']:,}; you have paid INR {f['paid']:,}, leaving INR {f['outstanding']:,} outstanding, "
                             f"due on {f['due_date']}. Status: {f['status']}.")
            elif "scholarship" in o:
                s = o["scholarship"]
                parts.append("No scholarship is recorded for you." if not s else
                             f"Your {s['name']} ({s['percent']}%) is {s['status']}. Condition: {s['condition']}. Next review: {s['next_review']}.")
            elif "chunks" in o:
                if not o.get("found"):
                    parts.append("The available policy does not establish an answer to that question, and I don't want to guess. "
                                 "You can raise a support request so Student Services can advise.")
                else:
                    best = o["chunks"][0]
                    sents = re.split(r"(?<=[.!?])\s+", best["text"])
                    parts.append(f"Handbook §{best['section'].split(' ')[0]} ({best['section'].split(' ', 1)[1]}): {' '.join(sents[:2])}")
            elif "ticket" in o:
                tk = o["ticket"]
                parts.append(f"You already have an identical request, {tk['ticket_ref']}; I did not create a duplicate." if tk.get("duplicate")
                             else f"I've raised support request {tk['ticket_ref']} ({tk['category']}). Status: {tk['status']}.")
            elif "tickets" in o:
                tl = o["tickets"]
                parts.append("You have no support requests." if not tl else
                             "Your support requests: " + "; ".join(f"{x['ticket_ref']} ({x['subject']}, {x['status']})" for x in tl))
            elif o.get("status") == "applied":
                parts.append(f"Done. Your registered {o['field']} has been updated.")
            elif o.get("status") == "pending_approval":
                parts.append(f"Your {o['field']} change request {o['approval_ref']} is pending staff approval. Your record has not been changed yet.")
            else:
                parts.append(o.get("message", "Done."))
        return " ".join(parts)
