"""Data behind the AI Performance page.

Two kinds of numbers are kept strictly apart:
  * live usage      - computed from stored agent runs that really called the model (provider == "groq").
  * evaluation runs - read from result files written by the evaluation runner. Runs that called the model are reported as model
                      performance; runs that did not call a model are reported only as checks of the application workflow.
Anything not measured is None and rendered as "Not available"."""
import json
from pathlib import Path

from ..extensions import db
from ..models import AgentRun, ToolExecution


def live_usage() -> dict:
    q = AgentRun.query.filter_by(provider="groq")
    n = q.count()
    if n == 0:
        return {"queries": 0}
    ok = q.filter_by(status="completed").count()
    lat = db.session.query(db.func.avg(AgentRun.latency_ms)).filter(AgentRun.provider == "groq").scalar()
    tok = db.session.query(db.func.sum(AgentRun.input_tokens), db.func.sum(AgentRun.output_tokens), db.func.sum(AgentRun.total_tokens),
                           db.func.count(AgentRun.total_tokens)).filter(AgentRun.provider == "groq").one()
    tool_rows = (db.session.query(ToolExecution.tool_name, ToolExecution.status, db.func.count())
                 .join(AgentRun, AgentRun.id == ToolExecution.run_id).filter(AgentRun.provider == "groq")
                 .group_by(ToolExecution.tool_name, ToolExecution.status).all())
    tools: dict = {}
    for name, status, c in tool_rows:
        t = tools.setdefault(name, {"ok": 0, "other": 0})
        t["ok" if status == "ok" else "other"] += c
    return {"queries": n, "success_rate": ok / n, "errors": n - ok, "avg_latency_ms": lat,
            "input_tokens": tok[0] if tok[3] else None, "output_tokens": tok[1] if tok[3] else None, "total_tokens": tok[2] if tok[3] else None,
            "token_runs": tok[3], "tools": tools, "evidence": retrieval_evidence(), "models": sorted(
                m for (m,) in db.session.query(AgentRun.model).filter_by(provider="groq").distinct())}


def retrieval_evidence() -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    rows = (ToolExecution.query.join(AgentRun, AgentRun.id == ToolExecution.run_id)
            .filter(AgentRun.provider == "groq", ToolExecution.tool_name == "search_student_policy", ToolExecution.status == "ok").all())
    for r in rows:
        try:
            for c in json.loads(r.result_json or "{}").get("chunks", []):
                counts[c["section"]] = counts.get(c["section"], 0) + 1
        except (ValueError, TypeError):
            continue
    return sorted(counts.items(), key=lambda x: -x[1])[:6]


def recent_runs(limit=10, provider="groq") -> list[AgentRun]:
    return AgentRun.query.filter_by(provider=provider).order_by(AgentRun.id.desc()).limit(limit).all()


def _load(results_dir: Path, pattern: str) -> list[dict]:
    out = []
    for f in sorted(Path(results_dir).glob(pattern)):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def evaluations(results_dir: Path) -> dict:
    """Split evaluation files by whether the model was really called."""
    model_runs, workflow_runs, security = [], [], []
    for d in _load(results_dir, "results_*.json"):
        s = d["summary"]
        item = {"label": s.get("label"), "model": s.get("model"), "generated": s.get("generated_utc"), "cases": s["cases"], "passed": s["passed"],
                "accuracy": s["accuracy"], "tool_rate": s["tool_selection_rate"], "avg_latency_ms": s["avg_latency_ms"],
                "input_tokens": s.get("input_tokens"), "output_tokens": s.get("output_tokens"), "total_tokens": s.get("total_tokens"),
                "categories": s.get("by_category", {}),
                "failed": [{"id": r["test_id"], "question": r["question"], "why": "; ".join(r.get("failed_checks", []))[:200]}
                           for r in d["records"] if not r["success"]]}
        (model_runs if s.get("provider") == "groq" else workflow_runs).append(item)
    for d in _load(results_dir, "security_*.json"):
        s = d["summary"]
        security.append({"label": s.get("label"), "generated": s.get("generated_utc"), "model_called": s.get("provider") == "groq",
                         "passed": s["passed"], "total": s["total"], "records": d["records"]})
    return {"model_runs": model_runs, "workflow_runs": workflow_runs, "security": security}
