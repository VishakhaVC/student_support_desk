"""Groq connectivity test. Run this BEFORE using the app:

    python scripts/groq_connectivity_test.py            # uses GROQ_MODEL from .env
    python scripts/groq_connectivity_test.py --model openai/gpt-oss-20b

Checks: 1) API key authenticates  2) model is available  3) basic chat completion
        4) native tool calling (tool request -> our result -> final answer)  5) token usage is reported.
The API key is never printed.
"""
import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

results = []


def report(name, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def scrub(text, key):
    return str(text).replace(key, "***") if key else str(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"))
    args = ap.parse_args()
    key = os.getenv("GROQ_API_KEY", "")
    if not key or key.startswith("your_"):
        print("[FAIL] GROQ_API_KEY is not set. Copy .env.example to .env and put your key in it (do not paste it anywhere else).")
        return 1
    try:
        import groq
    except ImportError:
        print("[FAIL] The 'groq' package is missing. Run: pip install -r requirements.txt")
        return 1

    client = groq.Groq(api_key=key, timeout=30, max_retries=1)
    print(f"Model under test: {args.model}\n")

    # 1 + 2: authentication and model availability
    try:
        ids = sorted(m.id for m in client.models.list().data)
        report("API authentication", True, f"{len(ids)} models visible to this key")
        ok = args.model in ids
        report("Model availability", ok, "listed" if ok else "NOT listed. Candidates: " + ", ".join(i for i in ids if "gpt-oss" in i or "qwen" in i))
    except groq.AuthenticationError:
        report("API authentication", False, "key rejected (check GROQ_API_KEY)")
        return 1
    except Exception as e:
        report("API authentication", False, scrub(f"{type(e).__name__}: {e}", key))
        return 1

    # 3: basic completion + 5: usage
    try:
        r = client.chat.completions.create(model=args.model, temperature=0, messages=[{"role": "user", "content": "Reply with exactly one word: ready"}])
        report("Basic chat completion", bool(r.choices[0].message.content), (r.choices[0].message.content or "")[:40].strip())
        u = r.usage
        report("Token usage reported", u is not None and u.total_tokens is not None,
               f"prompt={u.prompt_tokens} completion={u.completion_tokens} total={u.total_tokens}" if u else "no usage in response")
    except Exception as e:
        report("Basic chat completion", False, scrub(f"{type(e).__name__}: {e}", key))

    # 4: tool calling round trip
    tools = [{"type": "function", "function": {
        "name": "get_my_fee_status", "description": "Get the signed-in student's own fee status.",
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}}}]
    try:
        msgs = [{"role": "system", "content": "Use tools for the student's own records."},
                {"role": "user", "content": "What is my current fee status?"}]
        r1 = client.chat.completions.create(model=args.model, messages=msgs, tools=tools, tool_choice="auto", temperature=0)
        calls = r1.choices[0].message.tool_calls or []
        report("Tool call requested", bool(calls) and calls[0].function.name == "get_my_fee_status",
               f"tool={calls[0].function.name if calls else 'none'}")
        if calls:
            msgs.append({"role": "assistant", "content": r1.choices[0].message.content or "",
                         "tool_calls": [{"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}} for c in calls]})
            msgs.append({"role": "tool", "tool_call_id": calls[0].id, "name": calls[0].function.name,
                         "content": json.dumps({"ok": True, "fee": {"outstanding": 150000, "currency": "INR"}})})
            r2 = client.chat.completions.create(model=args.model, messages=msgs, tools=tools, tool_choice="auto", temperature=0)
            txt = r2.choices[0].message.content or ""
            report("Final answer uses tool result", "150" in txt, txt[:70].replace("\n", " "))
    except Exception as e:
        report("Tool calling", False, scrub(f"{type(e).__name__}: {e}", key))

    print(f"\n{sum(results)}/{len(results)} checks passed.")
    if not all(results):
        print("If the model is missing or tool calling fails, set GROQ_MODEL in .env to a model from the candidate list above and re-run.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
