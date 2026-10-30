"""Structured tracing. One TraceRecorder per agent run; it is persisted into agent_runs / tool_executions."""
import json
import time
import uuid

from ..extensions import db
from ..models import AgentRun, ToolExecution


class TraceRecorder:
    def __init__(self):
        self.trace_id = uuid.uuid4().hex
        self._t0 = time.perf_counter()
        self.events: list[dict] = []

    def event(self, type_: str, **data):
        self.events.append({"t_ms": round((time.perf_counter() - self._t0) * 1000, 1), "type": type_, **data})


def _clip(obj, limit=4000):
    s = obj if isinstance(obj, str) else json.dumps(obj, default=str)
    return s if len(s) <= limit else s[:limit] + "…[truncated]"


def persist_run(*, recorder, user, conversation_id, provider, model, status, error_kind, iterations,
                tool_outcomes, latency_ms, usage, context_report) -> AgentRun:
    run = AgentRun(trace_id=recorder.trace_id, user_id=user.id, conversation_id=conversation_id, provider=provider,
                   model=model, status=status, error_kind=error_kind, iterations=iterations,
                   tool_call_count=len(tool_outcomes), latency_ms=round(latency_ms, 1),
                   input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
                   total_tokens=usage.get("total_tokens"), context_json=json.dumps(context_report),
                   trace_json=_clip(recorder.events, 60000))
    db.session.add(run)
    db.session.flush()
    for step, o in enumerate(tool_outcomes, 1):
        db.session.add(ToolExecution(run_id=run.id, step=step, tool_name=o["name"], arguments_json=_clip(o["arguments"], 1500),
                                     result_json=_clip(o["result"]), status=o["status"], error_code=o.get("error_code"),
                                     latency_ms=o.get("latency_ms")))
    db.session.commit()
    return run
