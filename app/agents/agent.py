"""The agent loop, written from first principles (no agent framework).

    receive message -> redact secrets -> build context -> [ call model -> tool requested? -> validate + execute tool ->
    feed observation back ] repeat -> final answer -> save permitted memory -> persist trace

Safety limits: max iterations, max tool calls, overall wall-clock timeout, one retry for an invalid tool call, and every
failure ends in a clear, safe message instead of an exception or an endless loop.
"""
import json
import time
from dataclasses import dataclass, field

from ..memory import store as memory
from ..observability.tracing import TraceRecorder, persist_run
from ..security.redaction import redact
from ..services import students
from ..tools.registry import ToolContext
from .context import build_context
from .providers import ProviderError

SAFE_MESSAGES = {
    "rate_limit": "The AI service is busy right now (rate limit). Please wait a few seconds and try again.",
    "timeout": "The AI service took too long to respond. Please try again.",
    "connection": "I couldn't reach the AI service. Please check the connection and try again.",
    "auth": "The AI service rejected the configured credentials. Please tell the administrator.",
    "model_unavailable": "The configured AI model is not available. Please tell the administrator.",
    "invalid_tool_call": "I had trouble preparing that action. Please rephrase your request.",
    "server": "The AI service reported an error. Please try again shortly.",
    "bad_request": "The AI service could not process that request.",
    "not_configured": "The AI service is not configured (missing API key). Please tell the administrator.",
}


@dataclass
class AgentResult:
    answer: str
    status: str                       # completed | error | timeout | max_iterations | tool_limit
    error_kind: str | None = None
    trace_id: str = ""
    run_id: int | None = None
    iterations: int = 0
    tool_outcomes: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    context_report: list = field(default_factory=list)
    memory_report: dict = field(default_factory=dict)
    redactions: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    latency_ms: float = 0.0
    provider: str = ""
    model: str = ""
    clean_message: str = ""


class StudentSupportAgent:
    def __init__(self, provider, registry, retriever, cfg: dict):
        self.provider, self.registry, self.retriever, self.cfg = provider, registry, retriever, cfg

    def run(self, *, user, conversation, message: str) -> AgentResult:
        t0 = time.perf_counter()
        rec = TraceRecorder()
        clean, findings = redact(message)                       # secrets never reach the model, the DB or memory
        rec.event("request_received", redactions=findings, message_chars=len(clean))
        profile = students.get_profile(user) if user.student_id else None
        messages, context_report = build_context(user, profile, conversation, clean, self.cfg)
        rec.event("context_built", sources=context_report)
        ctx = ToolContext(user=user, config=self.cfg, retriever=self.retriever, trace_id=rec.trace_id)
        tools = self.registry.definitions_for(user)

        outcomes, usage_parts = [], []
        status, error_kind, answer, iterations, retried = "max_iterations", None, "", 0, False
        deadline = t0 + self.cfg["AGENT_TIMEOUT_SECONDS"]

        while iterations < self.cfg["AGENT_MAX_ITERATIONS"]:
            if time.perf_counter() > deadline:
                status, error_kind, answer = "timeout", "timeout", SAFE_MESSAGES["timeout"]
                break
            iterations += 1
            m0 = time.perf_counter()
            try:
                resp = self.provider.chat(messages, tools)
            except ProviderError as e:
                rec.event("model_error", iteration=iterations, kind=e.kind, message=str(e))
                if e.kind == "invalid_tool_call" and not retried:
                    retried = True
                    messages.append({"role": "system", "content": "Your previous tool call was malformed. Call the tool again with valid JSON arguments, or answer directly."})
                    continue
                status, error_kind, answer = "error", e.kind, SAFE_MESSAGES.get(e.kind, SAFE_MESSAGES["server"])
                break
            rec.event("model_call", iteration=iterations, latency_ms=round((time.perf_counter() - m0) * 1000, 1),
                      usage=resp.usage, tool_calls=[c.name for c in resp.tool_calls], finish_reason=resp.finish_reason)
            usage_parts.append(resp.usage)

            if not resp.tool_calls:                              # no tool requested -> this is the final answer
                status, answer = "completed", (resp.content or "").strip() or "I'm sorry, I could not produce an answer."
                break

            if len(outcomes) + len(resp.tool_calls) > self.cfg["AGENT_MAX_TOOL_CALLS"]:
                status, error_kind, answer = "tool_limit", "tool_limit", "I needed too many steps for that request, so I stopped. Please try a simpler request."
                rec.event("tool_limit_reached", limit=self.cfg["AGENT_MAX_TOOL_CALLS"])
                break

            messages.append({"role": "assistant", "content": resp.content or "",
                             "tool_calls": [{"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                                            for c in resp.tool_calls]})
            for call in resp.tool_calls:
                outcome = self.registry.execute(call.name, call.arguments, ctx)
                outcomes.append(outcome)
                rec.event("tool_call", name=call.name, arguments=call.arguments[:300], status=outcome["status"],
                          error_code=outcome["error_code"], latency_ms=outcome["latency_ms"])
                messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name,
                                 "content": json.dumps(outcome["result"], default=str)})   # observation goes back to the model

        mem_report = memory.process_message(user.id, clean, had_secret=bool(findings)) if user.role == "student" else {"saved": [], "blocked": "not a student"}
        rec.event("memory_processed", **mem_report)

        usages = [u for u in usage_parts if u]
        usage = ({"input_tokens": sum(u["input_tokens"] for u in usages), "output_tokens": sum(u["output_tokens"] for u in usages),
                  "total_tokens": sum(u["total_tokens"] for u in usages)} if usages else {})
        latency_ms = (time.perf_counter() - t0) * 1000
        rec.event("run_finished", status=status, error_kind=error_kind, latency_ms=round(latency_ms, 1))
        result = AgentResult(answer=answer, status=status, error_kind=error_kind, trace_id=rec.trace_id, iterations=iterations,
                             tool_outcomes=outcomes, evidence=ctx.evidence, context_report=context_report,
                             memory_report=mem_report, redactions=findings, usage=usage, latency_ms=latency_ms,
                             provider=self.provider.name, model=getattr(self.provider, "model", ""), clean_message=clean)
        return self._persist(result, rec, user, conversation)

    def _persist(self, result: AgentResult, rec, user, conversation) -> AgentResult:
        run = persist_run(recorder=rec, user=user, conversation_id=conversation.id if conversation else None,
                          provider=result.provider, model=result.model, status=result.status, error_kind=result.error_kind,
                          iterations=result.iterations, tool_outcomes=result.tool_outcomes, latency_ms=result.latency_ms,
                          usage=result.usage, context_report=result.context_report)
        result.run_id = run.id
        return result
