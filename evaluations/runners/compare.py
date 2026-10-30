"""Generate docs/ASSIGNMENT_COMPARISON.md from recorded result files. No figure in the tables is typed by hand.

    python -m evaluations.runners.compare            # compares a1_<mode> with a2_<mode> for every mode that has both files
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
R = ROOT / "evaluations" / "results"
OUT = ROOT / "docs" / "ASSIGNMENT_COMPARISON.md"


def load(name):
    f = R / name
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def fmt_tokens(s):
    return "unavailable" if s.get("total_tokens") is None else f"{s['input_tokens']} in / {s['output_tokens']} out / {s['total_tokens']} total"


def section(mode):
    a1, a2 = load(f"results_a1_{mode}.json"), load(f"results_a2_{mode}.json")
    sec = load(f"security_a2_{mode}.json")
    if not (a1 and a2):
        return None
    s1, s2 = a1["summary"], a2["summary"]
    traced = lambda d: sum(1 for r in d["records"] if r.get("trace_id"))
    p = sec["summary"]["by_kind"] if sec else {}
    kind = lambda k: f"{p[k]['passed']}/{p[k]['total']}" if k in p else "not run"
    unsafe_a1 = "UNSAFE: change applied immediately, no approval (G18)" if s1.get("sensitive_change_executed_immediately") else "waited for approval"
    unsafe_a2 = ("sensitive change held for approval (G18)" if s2.get("sensitive_change_executed_immediately") is False else "UNSAFE: applied immediately") \
        + (f"; security scenarios of this type passed {kind('unsafe_action')}" if sec else "")
    rows = [
        ("Task accuracy (golden set)", f"{s1['passed']}/{s1['cases']} ({s1['accuracy']:.0%})", f"{s2['passed']}/{s2['cases']} ({s2['accuracy']:.0%})"),
        ("Tool selection appropriate", f"{s1['tool_selection_rate']:.0%}", f"{s2['tool_selection_rate']:.0%}"),
        ("Average latency", f"{s1['avg_latency_ms']:.1f} ms", f"{s2['avg_latency_ms']:.1f} ms"),
        ("Median / max latency", f"{s1['median_latency_ms']:.1f} / {s1['max_latency_ms']:.1f} ms", f"{s2['median_latency_ms']:.1f} / {s2['max_latency_ms']:.1f} ms"),
        ("Tokens (input / output / total)", fmt_tokens(s1), fmt_tokens(s2)),
        ("Privacy tests (other student's data blocked)", "N/A: no authentication or per-user authorization existed", f"{kind('privacy')} passed" if sec else "not run"),
        ("Unsafe-action tests", unsafe_a1, unsafe_a2),
        ("Memory exclusion of OTP/password", "covered by G12/G13 (both pass)" if not any(f.startswith("G1") for f in s1["failed_cases"]) else "see failures", f"G12/G13 and security scenario S09 ({kind('memory')})"),
        ("Trace completeness (runs with trace id)", f"{traced(a1)}/{len(a1['records'])}", f"{traced(a2)}/{len(a2['records'])}"),
        ("Audit evidence", "N/A: no audit log existed", f"{sec['summary']['passed']}/{sec['summary']['total']} security scenarios passed (the denial, approval and tool scenarios assert specific audit rows)" if sec else "not run"),
    ]
    lines = [f"### Mode: `{s1['provider']}` (model: {s1['model']})", "", "| Measure | Assignment 1 baseline | Assignment 2 (final) |", "|---|---|---|"]
    lines += [f"| {m} | {x} | {y} |" for m, x, y in rows]
    lines += ["", f"Generated from `results_a1_{mode}.json` ({s1['generated_utc']} UTC) and `results_a2_{mode}.json` ({s2['generated_utc']} UTC).", ""]
    if sec:
        lines += ["#### Assignment 2 security scenarios", "", "| ID | Scenario | Expected | Observed | Result |", "|---|---|---|---|---|"]
        lines += [f"| {r['id']} | {r['scenario']} | {r['expected']} | {r['observed']} | {'pass' if r['passed'] else 'FAIL'} |" for r in sec["records"]]
        lines.append("")
    return "\n".join(lines)


def _case_table(d):
    rows = ["| ID | Question | Tools called | Appropriate | Evidence | Latency | Tokens | Result |", "|---|---|---|---|---|---|---|---|"]
    for r in d["records"]:
        q = r["question"].replace("|", "/")
        q = q if len(q) < 60 else q[:57] + "..."
        rows.append(f"| {r['test_id']} | {q} | {', '.join(r['actual_tools']) or 'none'} | {'yes' if r['tool_appropriate'] else 'NO'} | "
                    f"{', '.join(r['retrieved_chunks']) or '-'} | {r['latency_ms']} ms | {r['total_tokens']} | {'pass' if r['success'] else 'FAIL'} |")
    return "\n".join(rows)


def _summary(s):
    return (f"- Cases: **{s['passed']}/{s['cases']} passed ({s['accuracy']:.0%})**; appropriate tool choice {s['tool_selection_rate']:.0%}\n"
            f"- Latency: mean {s['avg_latency_ms']:.1f} ms, median {s['median_latency_ms']:.1f} ms, max {s['max_latency_ms']:.1f} ms\n"
            f"- Tokens: {'unavailable (the provider reported none)' if s['total_tokens'] is None else s['total_tokens']}\n"
            f"- Provider/model: `{s['provider']}` / {s['model']}; generated {s['generated_utc']} UTC")


def write_assignment_docs(mode):
    a1, a2, sec = load(f"results_a1_{mode}.json"), load(f"results_a2_{mode}.json"), load(f"security_a2_{mode}.json")
    if not (a1 and a2 and sec):
        return
    note = ("> **Read this first.** These figures come from the offline workflow-check provider (`deterministic-rules-v1`, *not an LLM*), because the build "
            "environment could not reach Groq. They show the tools, retrieval, memory rules, permissions and graders work. They do **not** measure LLM accuracy, "
            "latency or token cost. Produce those with the `--mode groq` commands in README.md and paste the new table here.") if mode == "deterministic" else ""
    (ROOT / "docs" / "ASSIGNMENT_1.md").write_text(f"""# Assignment 1 — Intelligent Student Support Agent

## What was built
One chat interface where the same agent decides to answer directly, call a tool, retrieve handbook policy, or use remembered information.
The original version is preserved in Git tag `assignment-1-baseline` (sign-in was a student picker; no passwords, approvals or audit).

| PDF expectation | Where |
|---|---|
| Agent loop that uses tool results before answering | `app/agents/agent.py` |
| At least two useful tools | six tools in `app/tools/registry.py` |
| Persistent memory; temporary/sensitive information not stored | `app/memory/store.py`, `app/security/redaction.py` |
| Retrieval from one policy document, only relevant parts | `app/retrieval/policy.py`, `data/sample_policy/student_handbook.md` |
| Context selection | `app/agents/context.py` |
| Missing information / tool errors / unanswerable question | golden cases G14, G15, G16 |
| Trace, accuracy, tools, latency, tokens, evidence | `evaluations/runners/run_eval.py` → `evaluations/results/results_*.csv` |

## Evaluation table (baseline code, run from the tag)
{note}

{_summary(a1['summary'])}

{_case_table(a1)}

### Key observations (from the recorded results)
- The five scenarios suggested in the PDF are covered by G02 (fee status), G06 (portal failure), G11 (new-session recall), G12/G13 (sensitive statements) and G15 (policy gap).
- Graders are proven able to fail: `tests/test_eval.py::test_grader_can_fail_a_broken_agent`.
- In the baseline, G18 shows the weakness Assignment 2 fixes: the contact change was applied immediately (`sensitive_change_executed_immediately = {a1['summary']['sensitive_change_executed_immediately']}`).
- Not yet observed here: real model behaviour (paraphrase handling, non-determinism, tokens, latency).
""", encoding="utf-8")
    (ROOT / "docs" / "ASSIGNMENT_2.md").write_text(f"""# Assignment 2 — Safe and Production-Aware Student Support Agent

## Privacy and approval rules implemented
1. **Identity**: the signed-in user comes from the server session (hashed passwords, CSRF, cookie flags, lockout, deactivated accounts refused and their open sessions ended). The model never names a student.
2. **Own data only**: students read only their own fees, scholarship, requests, memory and conversations. Enforced in services (`require_student_scope`), owner-scoped SQL and at the tool boundary. Another student's id through the agent is denied and audited; direct API calls return HTTP 403; another student's request looks "not found".
3. **Least privilege**: students are never offered administrator tools; if the model requests one it is denied. Administrator pages and endpoints reject students with 403.
4. **Human approval**: a contact-detail change creates a `pending` approval; the record is untouched. Only an administrator can approve or reject (a reason is required to reject). Approval re-checks role, self-approval, account status and request state, then applies the change and the audit row in one transaction; replays return HTTP 409; rejection never executes. An approval raised from a support request is linked to it and closes it.
5. **Secrets**: OTPs, passwords, card numbers and keys are redacted before storage, memory, the model and the audit log.
6. **Audit**: who, what, decision, result, reason, trace id. Append-only (ORM guard + database triggers). A demonstration trail, not tamper-proof enterprise logging.

## Required scenarios — recorded results (end to end over HTTP with real logins)
| ID | Scenario | Expected | Observed | Result |
|---|---|---|---|---|
""" + "\n".join(f"| {r['id']} | {r['scenario']} | {r['expected']} | {r['observed']} | {'pass' if r['passed'] else 'FAIL'} |" for r in sec["records"]) + f"""

Provider for these runs: `{sec['summary']['provider']}`. Scenarios S03 and S10 use the offline provider to *simulate* a model that misbehaves (passing a `student_id`, requesting an administrator tool); with a real model that is less likely, but the backend rules hold either way.

## Sample trace and audit evidence
In the running application: **Activity Logs** (filter Decision = denied / pending / approved), and **AI Performance → Recent agent runs → Details**, which lists the permission and audit rows that share the run's trace id. Typical rows: `login/allowed`, `tool:view_fee_status/allowed`, `tool:get_my_fee_status/denied (model supplied identity argument)`, `request_contact_change/pending`, `decide_approval/approved (executed)`, `decide_approval/denied (request is no longer pending)`, `update_ticket/allowed`, `create_student/allowed`.

## Final evaluation (golden set after controls)
{_summary(a2['summary'])}

Side-by-side table with Assignment 1: [ASSIGNMENT_COMPARISON.md](ASSIGNMENT_COMPARISON.md). The same limitation applies until you run with Groq.
""", encoding="utf-8")


def main():
    for m in ("deterministic", "groq"):
        write_assignment_docs(m)
    parts = [section(m) for m in ("deterministic", "groq")]
    body = "\n".join(p for p in parts if p)
    have_groq = parts[1] is not None
    doc = f"""# Assignment 1 vs Assignment 2 — measured comparison

This file is **generated** by `python -m evaluations.runners.compare` from the result files in `evaluations/results/`. Do not edit the tables by hand.

## How the comparison was made

- **Assignment 1 baseline** = Git tag `assignment-1-baseline` (commit before any Assignment 2 code). It was checked out into a separate folder and run with the same golden dataset (`evaluations/golden_dataset/golden.json`, 18 cases). Security was *not* switched off in the final app to imitate Assignment 1.
- **Assignment 2** = final code, same golden dataset, plus the 12 security scenarios.
- **Same conditions**: identical seed data, identical dataset, fresh in-memory database per case, latency = wall-clock time of the whole chat request.
- **One intended difference in the data**: handbook section 6.3 says contact changes need staff approval only in the Assignment 2 version, because that is the behaviour that version implements.

## Important limitation of the numbers below

{"Both a deterministic section and a Groq section are present." if have_groq else "**Only the deterministic section exists.** The `deterministic` mode uses a rule-based test double, not an LLM. It reports no tokens, and its latency (a few milliseconds) is pure application time. These tables therefore demonstrate that the harness, tools, privacy and approval controls behave as specified; they say **nothing about LLM accuracy, LLM latency or token cost**. To produce those, run the Groq commands at the end of this file and re-run this script."}

## Results

{body}

## What the evidence does and does not show

- **Supported by the recorded results:** in the baseline code the sensitive contact change was applied with no approval; in the final code it stays pending until an administrator approves, is applied once, and never executes after rejection. Cross-student access attempts (through the agent and through direct API calls) were blocked and audited. Secrets typed in chat were not stored.
- **Normal support tasks still work** after the controls were added (same accuracy on the same golden set).
- **Not supported yet (needs the Groq run):** any claim that the controls change real model latency or token usage. The controls add database reads/writes (audit rows) per tool call, which should be small next to a network round trip to the model, but this must be measured, not assumed.
- **Privacy tests are N/A for Assignment 1** because it had no authentication: there was no "other student" to be blocked from.

## Trade-offs

| Control | Benefit | Cost |
|---|---|---|
| Approval workflow | Sensitive data cannot change without a human decision | The student waits for staff; the agent cannot finish the task in one turn |
| Backend authorization + identity from session | The model cannot be talked into reading another student's data | More code paths to test; every new tool must declare roles and audit behaviour |
| Audit log | Accountability and evidence for demonstrations | Extra writes per action; demonstration-grade only (not tamper-proof) |
| Secret redaction before the LLM | Secrets never reach storage or the model | Conservative patterns can redact harmless text |
| Login throttling, CSRF, cookie flags | Standard web protections | Locked-out users must wait |

## Reproducing with the real Groq model

```powershell
# 1. Final version (this folder), real model, 3 repeats to see non-determinism
python -m evaluations.runners.run_eval --label a2_groq --mode groq --suite all --repeats 3

# 2. Baseline version
git worktree add ..\\a1_baseline assignment-1-baseline
cd ..\\a1_baseline
python -m evaluations.runners.run_eval --label a1_groq --mode groq --repeats 3
copy evaluations\\results\\*a1_groq* ..\\student-support-ai\\evaluations\\results\\
cd ..\\student-support-ai

# 3. Regenerate this document
python -m evaluations.runners.compare
```
"""
    OUT.write_text(doc, encoding="utf-8")
    print(f"Wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
