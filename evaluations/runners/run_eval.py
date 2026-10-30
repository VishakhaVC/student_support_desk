"""Evaluation runner.

    python -m evaluations.runners.run_eval --label a1_deterministic                    # deterministic test double (no API key)
    python -m evaluations.runners.run_eval --label a1_groq --mode groq --repeats 3     # real Groq model

Every case runs against a freshly seeded in-memory database. Grading is deterministic (tools, keywords, sources, database
state). Latency is wall-clock time of the full chat request. Token counts are the provider's reported usage, or "unavailable".
"""
import argparse
import csv
import json
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "evaluations" / "results"
GOLDEN = ROOT / "evaluations" / "golden_dataset" / "golden.json"


def make_app(mode: str):
    from app import create_app
    from app.config import TestConfig
    over = {"LLM_MODE": mode, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"}
    if mode == "groq":
        over["GROQ_API_KEY"] = os.getenv("GROQ_API_KEY", "")
        over["GROQ_MODEL"] = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    return create_app(TestConfig, **over)


def reset_db():
    from app.extensions import db
    from data.seed.seed import seed_database
    db.session.remove()
    db.drop_all()
    db.create_all()
    from . import security_suite
    seed_database(security_suite.PW)


# ------------------------------------------------------------------ DB-state checks
def run_db_check(check, user, before):
    from app.models import ChatMessage, MemoryItem, StudentProfile, SupportTicket
    t = check["type"]
    if t == "ticket_delta":
        now = SupportTicket.query.filter_by(student_id=user.student_id).count()
        return now - before["tickets"] == check["equals"], f"ticket delta {now - before['tickets']} (expected {check['equals']})"
    if t == "memory_has":
        item = MemoryItem.query.filter_by(user_id=user.id, key=check["key"]).first()
        ok = bool(item and check["contains"].lower() in item.value.lower())
        return ok, f"memory {check['key']}={'present' if ok else 'missing'}"
    if t == "value_absent":
        v = check["value"]
        leaked = any(v in m.value for m in MemoryItem.query.all()) or any(v in m.content for m in ChatMessage.query.all())
        return not leaked, "value found in stored data" if leaked else "value not stored"
    if t == "profile_unchanged":
        p = StudentProfile.query.filter_by(student_id=user.student_id).first()
        return getattr(p, check["field"]) == before["profile"][check["field"]], f"{check['field']} unchanged"
    return False, f"unknown db check {t}"


def grade(case, final, user, before):
    """Returns (success, tool_appropriate, failed_checks, details)."""
    failed = []
    actual = [t["name"] for t in final["activity"]["tools"]] if final.get("ok") else []
    reply = (final.get("reply") or final.get("message") or "")
    low = reply.lower()
    expected, prohibited = case["expected_tools"], case["prohibited_tools"]
    tool_ok = all(t in actual for t in expected) and not any(t in actual for t in prohibited)
    if case["expect_no_tool"] and actual:
        tool_ok = False
    if not tool_ok:
        failed.append(f"tools: expected {expected or 'none'} prohibited {prohibited} -> actual {actual}")
    if not final.get("ok") or final.get("status") != "completed":
        failed.append(f"run status {final.get('status') or final.get('error')}")
    if case["keywords_any"] and not any(k.lower() in low for k in case["keywords_any"]):
        failed.append(f"none of keywords {case['keywords_any']} in reply")
    if case["keywords_all"] and not all(k.lower() in low for k in case["keywords_all"]):
        failed.append(f"missing some of {case['keywords_all']}")
    for bad in case["must_not_contain"]:
        if bad.lower() in low:
            failed.append(f"reply contains forbidden text '{bad}'")
    ids = [s["chunk_id"] for s in final.get("sources", [])]
    if case["expected_sources_any"] and not any(i.startswith(p) for p in case["expected_sources_any"] for i in ids):
        failed.append(f"expected a source starting with {case['expected_sources_any']}, got {ids}")
    if case["expect_no_sources"] and ids:
        failed.append(f"expected no sources, got {ids}")
    for chk in case["db_checks"]:
        ok, why = run_db_check(chk, user, before)
        if not ok:
            failed.append(f"db: {why}")
    return not failed, tool_ok, failed


def run_case(app, case, repeat):
    from app.models import StudentProfile, SupportTicket, User
    from app.services.chat import handle_chat
    reset_db()
    user = User.query.filter_by(username=case["user"]).first()
    app.config["INJECT_TOOL_FAILURES"] = set(case["setup"].get("inject_tool_failure", []))
    before = {"tickets": SupportTicket.query.filter_by(student_id=user.student_id).count(),
              "profile": {c: getattr(StudentProfile.query.filter_by(student_id=user.student_id).first(), c) for c in ("email", "phone")}}
    conv_id, final, wall_ms = None, {}, 0.0
    for step in case["steps"]:
        if step.get("new_conversation"):
            conv_id = None
        t0 = time.perf_counter()
        final = handle_chat(user, step["message"], conv_id)
        wall_ms = (time.perf_counter() - t0) * 1000
        conv_id = final.get("conversation_id", conv_id)
    success, tool_ok, failed = grade(case, final, user, before)
    act = final.get("activity", {})
    tok = act.get("tokens")
    rec = {
        "test_id": case["id"], "repeat": repeat, "category": case["category"], "question": case["steps"][-1]["message"],
        "expected_behavior": case["expected_behavior"], "response": final.get("reply") or final.get("message"),
        "expected_tools": case["expected_tools"] or ("none" if case["expect_no_tool"] else "any"),
        "actual_tools": [t["name"] for t in act.get("tools", [])], "tool_statuses": [t["status"] for t in act.get("tools", [])],
        "tool_appropriate": tool_ok, "retrieved_chunks": [s["chunk_id"] for s in final.get("sources", [])],
        "memory_items_used": [c["item"] for c in act.get("context", []) if c.get("source") == "memory"],
        "latency_ms": round(wall_ms, 1),
        "input_tokens": tok["input_tokens"] if tok else "unavailable", "output_tokens": tok["output_tokens"] if tok else "unavailable",
        "total_tokens": tok["total_tokens"] if tok else "unavailable",
        "success": success, "failed_checks": failed, "error": final.get("error") or final.get("error_kind"),
        "trace_id": final.get("trace_id"), "provider": act.get("provider"), "model": act.get("model"),
    }
    if case.get("sensitive_action"):
        p = StudentProfile.query.filter_by(student_id=user.student_id).first()
        rec["sensitive_change_executed_immediately"] = getattr(p, case["sensitive_action"]["field"]) == case["sensitive_action"]["new_value"]
    return rec


def summarise(records, label, mode, model):
    lat = [r["latency_ms"] for r in records]
    toks = [r["total_tokens"] for r in records if isinstance(r["total_tokens"], int)]
    cats = {}
    for r in records:
        c = cats.setdefault(r["category"], [0, 0])
        c[0] += r["success"]; c[1] += 1
    s = {
        "label": label, "provider": mode, "model": model, "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "cases": len(records), "passed": sum(r["success"] for r in records), "accuracy": sum(r["success"] for r in records) / len(records),
        "tool_selection_rate": sum(r["tool_appropriate"] for r in records) / len(records),
        "avg_latency_ms": statistics.mean(lat), "median_latency_ms": statistics.median(lat), "max_latency_ms": max(lat),
        "total_tokens": sum(toks) if toks else None, "input_tokens": sum(r["input_tokens"] for r in records if isinstance(r["input_tokens"], int)) if toks else None,
        "output_tokens": sum(r["output_tokens"] for r in records if isinstance(r["output_tokens"], int)) if toks else None,
        "token_data_available_for": f"{len(toks)}/{len(records)} runs",
        "by_category": {k: {"passed": v[0], "total": v[1]} for k, v in sorted(cats.items())},
        "failed_cases": [r["test_id"] for r in records if not r["success"]],
        "sensitive_change_executed_immediately": next((r["sensitive_change_executed_immediately"] for r in records if "sensitive_change_executed_immediately" in r), None),
    }
    return s


def save(records, summary, label):
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"results_{label}.json").write_text(json.dumps({"summary": summary, "records": records}, indent=2, default=str), encoding="utf-8")
    (RESULTS / f"summary_{label}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    cols = ["test_id", "repeat", "category", "question", "expected_tools", "actual_tools", "tool_appropriate", "retrieved_chunks",
            "memory_items_used", "latency_ms", "input_tokens", "output_tokens", "total_tokens", "success", "failed_checks", "error", "trace_id", "response"]
    with open(RESULTS / f"results_{label}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows({**r, **{k: json.dumps(r[k]) for k in ("expected_tools", "actual_tools", "retrieved_chunks", "memory_items_used", "failed_checks")}} for r in records)


def run_security(app, mode, label):
    from . import security_suite
    with app.app_context():
        records, summary = security_suite.run_all(app, reset_db)
    summary.update({"label": label, "provider": mode, "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")})
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"security_{label}.json").write_text(json.dumps({"summary": summary, "records": records}, indent=2, default=str), encoding="utf-8")
    for r in records:
        print(f"{'PASS' if r['passed'] else 'FAIL'}  {r['id']}  {r['scenario']:<60} {r['observed']}")
    print(f"\nSecurity scenarios: {summary['passed']}/{summary['total']} passed. Saved to evaluations/results/security_{label}.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--mode", choices=["deterministic", "groq"], default="deterministic")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--suite", choices=["golden", "security", "all"], default="golden")
    args = ap.parse_args()
    app = make_app(args.mode)
    if args.suite in ("security", "all"):
        run_security(app, args.mode, args.label)
        if args.suite == "security":
            return
    cases = json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"]
    records = []
    with app.app_context():
        for rep in range(1, args.repeats + 1):
            for case in cases:
                rec = run_case(app, case, rep)
                records.append(rec)
                print(f"{'PASS' if rec['success'] else 'FAIL'}  {rec['test_id']:<36} {rec['latency_ms']:>8.1f} ms  tools={rec['actual_tools']}" + ("" if rec["success"] else f"\n      -> {rec['failed_checks']}"))
        model = records[0]["model"] if records else ""
    summary = summarise(records, args.label, args.mode, model)
    save(records, summary, args.label)
    print(f"\n{summary['passed']}/{summary['cases']} passed ({summary['accuracy']:.0%}); tool selection {summary['tool_selection_rate']:.0%}; "
          f"avg latency {summary['avg_latency_ms']:.1f} ms; tokens: {summary['total_tokens'] if summary['total_tokens'] is not None else 'unavailable'}")
    print(f"Saved to evaluations/results/results_{args.label}.(json|csv) and summary_{args.label}.json")


if __name__ == "__main__":
    main()
