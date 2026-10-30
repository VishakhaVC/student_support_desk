import json

from evaluations.runners import run_eval


def test_golden_dataset_is_well_formed():
    data = json.loads(run_eval.GOLDEN.read_text(encoding="utf-8"))["cases"]
    ids = [c["id"] for c in data]
    assert len(ids) == len(set(ids)) >= 15
    cats = {c["category"] for c in data}
    assert {"direct_answer", "fee_lookup", "scholarship_lookup", "policy_retrieval", "ticket_creation", "memory",
            "sensitive_exclusion", "missing_information", "tool_failure", "irrelevant_tool_avoidance"} <= cats


def test_grader_can_fail_a_broken_agent(app, tmp_path):
    """A harness that can only pass proves nothing: an agent that never uses tools must FAIL the fee case."""
    from tests.conftest import ScriptedProvider, text_response
    app.extensions["provider"] = ScriptedProvider([text_response("I think you owe about 5000.")])
    case = next(c for c in json.loads(run_eval.GOLDEN.read_text(encoding="utf-8"))["cases"] if c["id"] == "G02_fee_lookup")
    rec = run_eval.run_case(app, case, 1)
    assert rec["success"] is False and rec["tool_appropriate"] is False
    assert any("tools" in f for f in rec["failed_checks"]) and any("keywords" in f for f in rec["failed_checks"])


def test_summary_metrics_come_from_records():
    recs = [dict(test_id='a', success=True, tool_appropriate=True, latency_ms=10.0, total_tokens=30, input_tokens=20, output_tokens=10, category="a"),
            dict(test_id='b', success=False, tool_appropriate=True, latency_ms=30.0, total_tokens="unavailable", input_tokens="unavailable", output_tokens="unavailable", category="a"),
            dict(test_id='c', success=True, tool_appropriate=False, latency_ms=20.0, total_tokens=50, input_tokens=30, output_tokens=20, category="b")]
    s = run_eval.summarise(recs, "x", "groq", "m")
    assert s["accuracy"] == 2 / 3 and s["avg_latency_ms"] == 20.0 and s["total_tokens"] == 80
    assert s["token_data_available_for"] == "2/3 runs" and s["tool_selection_rate"] == 2 / 3


def test_full_golden_run_with_deterministic_provider(app):
    cases = json.loads(run_eval.GOLDEN.read_text(encoding="utf-8"))["cases"]
    recs = [run_eval.run_case(app, c, 1) for c in cases]
    assert all(r["total_tokens"] == "unavailable" for r in recs)         # test double reports no tokens; none invented
    assert [r["test_id"] for r in recs if not r["success"]] == []


def test_security_suite_detects_broken_authorization(app, monkeypatch):
    """Negative control: if ownership checks are disabled, the security suite must report failures (it cannot only pass)."""
    from app.services import students, tickets
    monkeypatch.setattr(students, "require_student_scope", lambda actor, sid: "owner")
    monkeypatch.setattr(tickets, "require_student_scope", lambda actor, sid: "owner")
    from evaluations.runners import security_suite
    records, summary = security_suite.run_all(app, run_eval.reset_db)
    s04 = next(r for r in records if r["id"] == "S04")
    assert s04["passed"] is False and summary["passed"] < summary["total"]
