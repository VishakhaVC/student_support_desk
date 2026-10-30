import json

from app.agents.providers import ProviderError
from app.extensions import db
from app.models import AgentRun, ToolExecution
from app.services.chat import handle_chat
from tests.conftest import ScriptedProvider, text_response, tool_call_response


def test_agent_answers_directly_when_no_tool_needed(app, student_a, use_provider):
    use_provider(ScriptedProvider([text_response("Hello!")]))
    r = handle_chat(student_a, "hi")
    assert r["ok"] and r["reply"] == "Hello!" and r["activity"]["tools"] == []


def test_agent_uses_tool_result_before_answering(app, student_a, use_provider):
    p = use_provider(ScriptedProvider([tool_call_response("get_my_fee_status"), text_response("You owe 150,000")]))
    r = handle_chat(student_a, "fees?")
    assert p.calls == 2                                   # model -> tool -> model
    assert r["activity"]["tools"][0]["name"] == "get_my_fee_status" and r["status"] == "completed"
    run = db.session.get(AgentRun, r["run_id"])
    assert json.loads(ToolExecution.query.filter_by(run_id=run.id).first().result_json)["fee"]["outstanding"] == 150000


def test_token_usage_is_summed_from_provider_values(app, student_a, use_provider):
    use_provider(ScriptedProvider([tool_call_response("get_my_fee_status"), text_response("ok")]))
    r = handle_chat(student_a, "fees?")
    assert r["activity"]["tokens"] == {"input_tokens": 22, "output_tokens": 11, "total_tokens": 33}


def test_missing_usage_is_unavailable_not_invented(app, student_a, use_provider):
    use_provider(ScriptedProvider([text_response("hi", usage=False)]))
    r = handle_chat(student_a, "hi")
    assert r["activity"]["tokens"] is None
    assert db.session.get(AgentRun, r["run_id"]).total_tokens is None


def test_loop_terminates_at_iteration_limit(app, student_a, use_provider):
    app.config["AGENT_MAX_ITERATIONS"] = 3
    p = use_provider(ScriptedProvider([tool_call_response("get_my_fee_status")]))   # model never stops asking for tools
    r = handle_chat(student_a, "fees?")
    assert p.calls == 3 and r["status"] == "max_iterations"


def test_loop_terminates_at_tool_call_limit(app, student_a, use_provider):
    app.config["AGENT_MAX_TOOL_CALLS"] = 2
    use_provider(ScriptedProvider([tool_call_response("get_my_fee_status")]))
    r = handle_chat(student_a, "fees?")
    assert r["status"] == "tool_limit" and len(r["activity"]["tools"]) == 2


def test_overall_timeout_stops_the_run(app, student_a, use_provider):
    app.config["AGENT_TIMEOUT_SECONDS"] = -1
    use_provider(ScriptedProvider([text_response("never reached")]))
    r = handle_chat(student_a, "hi")
    assert r["status"] == "timeout"


def test_invalid_tool_arguments_become_observation_and_model_can_recover(app, student_a, use_provider):
    use_provider(ScriptedProvider([tool_call_response("create_support_ticket", '{"category":"payment"}'), text_response("Please give details")]))
    r = handle_chat(student_a, "make ticket")
    assert r["activity"]["tools"][0]["error_code"] == "invalid_arguments" and r["status"] == "completed"


def test_unknown_tool_is_rejected_not_executed(app, student_a, use_provider):
    use_provider(ScriptedProvider([tool_call_response("delete_everything"), text_response("Sorry")]))
    r = handle_chat(student_a, "do it")
    assert r["activity"]["tools"][0]["error_code"] == "unknown_tool"


def test_malformed_json_arguments_are_handled(app, student_a, use_provider):
    use_provider(ScriptedProvider([tool_call_response("search_student_policy", "{not json"), text_response("ok")]))
    r = handle_chat(student_a, "policy?")
    assert r["activity"]["tools"][0]["error_code"] == "invalid_json"


def test_tool_failure_is_reported_to_model_and_run_survives(app, student_a, use_provider):
    app.config["INJECT_TOOL_FAILURES"] = {"get_my_fee_status"}
    use_provider(ScriptedProvider([tool_call_response("get_my_fee_status"), text_response("I could not fetch that")]))
    r = handle_chat(student_a, "fees?")
    assert r["activity"]["tools"][0]["error_code"] == "tool_failure" and r["status"] == "completed"


def test_provider_errors_give_safe_messages_not_exceptions(app, student_a, use_provider):
    for kind in ("rate_limit", "timeout", "connection", "auth", "server"):
        use_provider(ScriptedProvider([ProviderError(kind, "boom")]))
        r = handle_chat(student_a, "hi")
        assert r["ok"] and r["status"] == "error" and r["error_kind"] == kind and "boom" not in r["reply"]


def test_invalid_tool_call_from_model_is_retried_once(app, student_a, use_provider):
    p = use_provider(ScriptedProvider([ProviderError("invalid_tool_call", "bad"), text_response("recovered")]))
    r = handle_chat(student_a, "hi")
    assert r["status"] == "completed" and r["reply"] == "recovered" and p.calls == 2


def test_missing_api_key_gives_clear_error_without_fallback(app, student_a):
    app.config["LLM_MODE"] = "groq"
    app.config["GROQ_API_KEY"] = ""
    app.extensions.pop("provider", None)
    r = handle_chat(student_a, "hi")
    assert r["ok"] is False and r["error"] == "not_configured" and r["http"] == 503


def test_every_run_is_traced(app, student_a, use_provider):
    use_provider(ScriptedProvider([tool_call_response("get_my_fee_status"), text_response("ok")]))
    r = handle_chat(student_a, "fees?")
    run = db.session.get(AgentRun, r["run_id"])
    types = [e["type"] for e in json.loads(run.trace_json)]
    assert types[0] == "request_received" and "context_built" in types and "tool_call" in types and types[-1] == "run_finished"
    assert run.latency_ms > 0 and run.trace_id == r["trace_id"]


def test_model_only_sees_tools_for_students(app, student_a, use_provider):
    p = use_provider(ScriptedProvider([text_response("hi")]))
    handle_chat(student_a, "hi")
    names = {t["function"]["name"] for t in p.seen_tools}
    assert "get_my_fee_status" in names and "search_student_policy" in names
