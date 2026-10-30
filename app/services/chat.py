"""Glue between the web layer and the agent: conversation handling, persistence of messages, response payload."""
from flask import current_app

from ..agents.agent import SAFE_MESSAGES, StudentSupportAgent
from ..agents.providers import DeterministicProvider, GroqProvider, ProviderError
from ..extensions import db
from ..models import ChatMessage, Conversation
from . import audit


def make_provider(cfg):
    if cfg["LLM_MODE"] == "deterministic":
        return DeterministicProvider()
    return GroqProvider(cfg["GROQ_API_KEY"], cfg["GROQ_MODEL"], cfg["GROQ_TIMEOUT_SECONDS"], cfg["GROQ_MAX_RETRIES"])


def get_provider():
    ext = current_app.extensions
    if "provider" not in ext:
        ext["provider"] = make_provider(current_app.config)   # may raise ProviderError(not_configured)
    return ext["provider"]


def _error(kind, message, http=400):
    return {"ok": False, "error": kind, "message": message, "http": http}


def handle_chat(user, message: str, conversation_id=None) -> dict:
    cfg = current_app.config
    message = (message or "").strip()
    if not message:
        return _error("empty_message", "Please type a question.")
    if len(message) > cfg["MAX_MESSAGE_CHARS"]:
        return _error("message_too_long", f"Messages are limited to {cfg['MAX_MESSAGE_CHARS']} characters.")
    try:
        provider = get_provider()
    except ProviderError as e:
        return _error(e.kind, SAFE_MESSAGES.get(e.kind, str(e)), 503)

    if conversation_id:
        conv = Conversation.query.filter_by(id=conversation_id, user_id=user.id).first()   # ownership-scoped
        if conv is None:
            if Conversation.query.filter_by(id=conversation_id).first() is not None:
                audit.record(user, "chat_in_conversation", "conversation", conversation_id, "denied", result="blocked", reason="not the owner")
            return _error("conversation_not_found", "Conversation not found.", 404)
    else:
        conv = Conversation(user_id=user.id)
        db.session.add(conv)
        db.session.flush()

    agent = StudentSupportAgent(provider, current_app.extensions["registry"], current_app.extensions["retriever"], cfg)
    result = agent.run(user=user, conversation=conv, message=message)

    if conv.title == "New conversation":
        conv.title = result.clean_message[:60]
    db.session.add(ChatMessage(conversation_id=conv.id, role="user", content=result.clean_message, run_id=result.run_id))
    db.session.add(ChatMessage(conversation_id=conv.id, role="assistant", content=result.answer, run_id=result.run_id))
    db.session.commit()

    seen, sources = set(), []
    for e in result.evidence:
        if e["chunk_id"] not in seen:
            seen.add(e["chunk_id"])
            sources.append(e)
    return {
        "ok": True, "reply": result.answer, "conversation_id": conv.id, "run_id": result.run_id, "trace_id": result.trace_id,
        "status": result.status, "error_kind": result.error_kind, "sources": sources,
        "activity": {
            "tools": [{"name": o["name"], "status": o["status"], "error_code": o["error_code"], "latency_ms": o["latency_ms"]} for o in result.tool_outcomes],
            "memory_saved": result.memory_report.get("saved", []), "memory_blocked": result.memory_report.get("blocked"),
            "redactions": result.redactions, "iterations": result.iterations, "latency_ms": round(result.latency_ms, 1),
            "tokens": result.usage or None,
            "context": [c for c in result.context_report if c.get("selected")],
            "provider": result.provider, "model": result.model,
        },
    }
