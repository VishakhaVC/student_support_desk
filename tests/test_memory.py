from app.memory import store as memory
from app.models import ChatMessage, MemoryItem
from app.security.redaction import redact
from app.services.chat import handle_chat


def test_permitted_preference_persists_across_sessions(app, student_a):
    handle_chat(student_a, "Please reply in Gujarati and I prefer short answers.")          # session 1 (new conversation)
    r = handle_chat(student_a, "Which language did I tell you I prefer?")                    # session 2 (new conversation)
    assert "Gujarati" in r["reply"]
    assert {i["item"] for i in r["activity"]["context"] if i["source"] == "memory"} >= {"preferred_language"}


def test_otp_is_not_saved_anywhere(app, student_a):
    r = handle_chat(student_a, "My OTP is 482913, please remember it for later.")
    assert "482913" not in r["reply"]
    assert MemoryItem.query.count() == 0
    assert not any("482913" in m.content for m in ChatMessage.query.all())
    assert r["activity"]["memory_blocked"] and "otp" in r["activity"]["redactions"]


def test_password_is_not_saved_anywhere(app, student_a):
    handle_chat(student_a, "My portal password is Tr0ub4dor&3 and I prefer short answers")
    assert MemoryItem.query.count() == 0                       # whole message skipped because it held a secret
    assert not any("Tr0ub4dor" in m.content for m in ChatMessage.query.all())


def test_non_allowlisted_facts_are_never_saved(app, student_a):
    handle_chat(student_a, "I am at the library right now and my flight leaves tomorrow")
    assert MemoryItem.query.count() == 0


def test_save_rejects_unknown_keys_and_secret_values(app, student_a):
    assert memory.save(student_a.id, "credit_card", "4111 1111 1111 1111") is False
    assert memory.save(student_a.id, "preferred_language", "gsk_abcdefghijklmnopqrstuvwx") is False
    assert memory.save(student_a.id, "preferred_language", "Hindi") is True


def test_memory_is_isolated_per_student(app, student_a, student_b):
    memory.save(student_a.id, "preferred_language", "Gujarati")
    assert memory.list_items(student_b.id) == []
    assert "Gujarati" not in handle_chat(student_b, "Which language did I tell you I prefer?")["reply"]


def test_newer_fact_replaces_stale_one(app, student_a):
    memory.save(student_a.id, "preferred_language", "Hindi")
    memory.save(student_a.id, "preferred_language", "Tamil")
    items = memory.list_items(student_a.id)
    assert len(items) == 1 and items[0].value == "Tamil"


def test_memory_can_be_inspected_and_deleted_via_api(app, client, student_a):
    from tests.conftest import login
    login(client, "student_a")
    memory.save(student_a.id, "preferred_language", "Hindi")
    assert client.get("/api/memory").get_json() == [{"key": "preferred_language", "value": "Hindi"}]
    assert client.delete("/api/memory/preferred_language").get_json()["deleted"] == 1
    assert client.get("/api/memory").get_json() == []


def test_redaction_covers_common_secrets():
    for text, kind in [("otp 123456", "otp"), ("password: hunter2!", "password"), ("cvv 123", "card_cvv"),
                       ("4111 1111 1111 1111", "card_number"), ("token = abc123xyz", "token"), ("gsk_abcdefghijklmnopqrstuvwx", "api_key")]:
        clean, found = redact(text)
        assert kind in found and "[REDACTED]" in clean
    assert redact("I forgot my password, how do I reset it?")[1] == []          # no false positive on a question


def test_context_selects_memory_only_when_relevant(app, student_a):
    memory.save(student_a.id, "programme", "PGDM (Big Data Analytics)")
    r = handle_chat(student_a, "What is my current fee status?")
    items = {c["item"] for c in r["activity"]["context"] if c.get("source") == "memory"}
    assert "programme" not in items
