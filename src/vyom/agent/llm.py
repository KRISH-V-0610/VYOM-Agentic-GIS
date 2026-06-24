"""
LLM backend abstraction for the VYOM agent.

The orchestrator owns the conversation loop and a NEUTRAL history format; it delegates the
single step "given history + tools, produce the next model turn" to an LLMBackend. This
keeps the orchestrator entirely SDK-free and lets tests drive it with a scripted backend —
essential here because an API key may still be a template placeholder.

Neutral history is a list of turn dicts:
  {"role": "user",  "text": str}
  {"role": "model", "text": str|None, "function_calls": [{"name", "args"}]}
  {"role": "tool",  "responses": [{"name", "result": dict}]}

A backend's generate() returns the next model step as:
  {"text": str|None, "function_calls": [{"name": str, "args": dict}, ...]}
"""

import json

from ..config import get_env

DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_GROQ_MODEL = "gemma-4-27b-it"
DEFAULT_SARVAM_MODEL = "sarvam-30b"
SARVAM_BASE_URL = "https://api.sarvam.ai/v1"


def _make_httpx_client():
    """Return an httpx.Client that handles corporate SSL inspection proxies.

    Checks REQUESTS_CA_BUNDLE for a custom CA bundle path, or SSL_NO_VERIFY=true
    to skip verification entirely (needed when a proxy injects a self-signed cert).
    Returns None when standard certifi verification is fine.
    """
    import httpx

    ca_bundle = get_env("REQUESTS_CA_BUNDLE") or get_env("SSL_CERT_FILE")
    no_verify = (get_env("SSL_NO_VERIFY") or "").lower() in ("1", "true", "yes")
    if no_verify:
        return httpx.Client(verify=False)
    if ca_bundle:
        return httpx.Client(verify=ca_bundle)
    return None


class LLMBackend:
    """Interface every backend implements."""

    def generate(self, history: list, system_instruction: str, declarations: list) -> dict:
        raise NotImplementedError


class GeminiBackend(LLMBackend):
    """google-genai backend with manual (developer-driven) function calling.

    Manual function calling — not the SDK's automatic mode — because the orchestrator
    must inspect and log every tool call and enforce the check_coverage-first rule.
    """

    def __init__(self, api_key: str = None, model: str = None, temperature: float = 0.0):
        from google import genai  # imported lazily so the module loads without the SDK

        api_key = api_key or get_env("GEMINI_API_KEY")
        if not api_key or "<" in api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is unset or still a template placeholder in .env. "
                "Set a real key to use the Gemini backend (tests can use a mock backend)."
            )
        self._genai = genai
        self.client = genai.Client(api_key=api_key)
        self.model = model or get_env("GEMINI_MODEL") or DEFAULT_MODEL
        self.temperature = temperature

    def _to_contents(self, history: list) -> list:
        from google.genai import types

        contents = []
        for turn in history:
            role = turn["role"]
            if role == "user":
                contents.append(types.Content(
                    role="user", parts=[types.Part(text=turn["text"])]))
            elif role == "model":
                parts = []
                if turn.get("text"):
                    parts.append(types.Part(text=turn["text"]))
                for fc in turn.get("function_calls", []):
                    parts.append(types.Part(function_call=types.FunctionCall(
                        name=fc["name"], args=fc.get("args") or {})))
                contents.append(types.Content(role="model", parts=parts))
            elif role == "tool":
                parts = [
                    types.Part.from_function_response(
                        name=r["name"], response=_as_response_dict(r["result"]))
                    for r in turn["responses"]
                ]
                # google-genai carries function responses in a user-role turn.
                contents.append(types.Content(role="user", parts=parts))
        return contents

    def generate(self, history: list, system_instruction: str, declarations: list) -> dict:
        import re
        import time
        from google.genai import types
        from google.genai.errors import ClientError

        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=[{"function_declarations": declarations}],
            temperature=self.temperature,
        )

        # Free-tier Gemini is 5 RPM — the agent loop makes ~10+ calls per query.
        # Also retry on transient 503 (model overloaded). Use string matching on
        # the error message because the SDK's exception attribute names vary by version.
        from google.genai.errors import ServerError

        backoff = 15.0
        for attempt in range(5):
            try:
                resp = self.client.models.generate_content(
                    model=self.model,
                    contents=self._to_contents(history),
                    config=config,
                )
                break
            except (ClientError, ServerError) as exc:
                msg = str(exc)
                is_429 = "429" in msg or "RESOURCE_EXHAUSTED" in msg
                is_503 = "503" in msg or "UNAVAILABLE" in msg
                # Daily quota exhaustion is not retryable — fail immediately.
                # Per-minute limits say "PerMinute"; daily say "PerDay" or "billing".
                is_daily_quota = is_429 and (
                    "PerDay" in msg or "per_day" in msg
                    or "exceeded your current quota" in msg
                    or "check your plan and billing" in msg
                )
                if is_daily_quota or not (is_429 or is_503) or attempt == 4:
                    raise
                # For per-minute 429 honour the "retry in Xs" hint; for 503 use backoff.
                m = re.search(r"retry in ([0-9.]+)s", msg)
                wait = float(m.group(1)) + 2.0 if (m and is_429) else backoff
                time.sleep(wait)
                backoff = min(backoff * 2, 120.0)

        function_calls, text_parts = [], []
        candidates = getattr(resp, "candidates", None) or []
        if candidates and candidates[0].content and candidates[0].content.parts:
            for p in candidates[0].content.parts:
                fc = getattr(p, "function_call", None)
                if fc:
                    function_calls.append({"name": fc.name, "args": dict(fc.args or {})})
                elif getattr(p, "text", None):
                    text_parts.append(p.text)

        return {
            "text": "".join(text_parts) or None,
            "function_calls": function_calls,
        }


def _as_response_dict(result) -> dict:
    """FunctionResponse.response must be a JSON object — wrap non-dict results."""
    return result if isinstance(result, dict) else {"result": result}


def _gemini_schema_to_openai(schema: dict) -> dict:
    """Recursively convert Gemini JSON-schema (uppercase types) to OpenAI format (lowercase)."""
    if not schema:
        return {"type": "object", "properties": {}}
    out = {}
    for k, v in schema.items():
        if k == "type" and isinstance(v, str):
            out[k] = v.lower()
        elif k == "properties" and isinstance(v, dict):
            out[k] = {pk: _gemini_schema_to_openai(pv) for pk, pv in v.items()}
        elif k == "items" and isinstance(v, dict):
            out[k] = _gemini_schema_to_openai(v)
        else:
            out[k] = v
    return out


class GroqBackend(LLMBackend):
    """Groq backend (OpenAI-compatible API) — supports Gemma, Llama, and other hosted models.

    Accepts the same neutral history + Gemini-style declarations as the rest of the system;
    converts them to OpenAI messages/tools format internally.
    """

    def __init__(self, api_key: str = None, model: str = None, temperature: float = 0.0):
        from groq import Groq  # imported lazily

        api_key = api_key or get_env("GROQ_API_KEY")
        if not api_key or "<" in api_key:
            raise RuntimeError(
                "GROQ_API_KEY is unset or still a placeholder in .env. "
                "Add a real Groq key to use this backend."
            )
        http_client = _make_httpx_client()
        self.client = Groq(api_key=api_key, **({'http_client': http_client} if http_client else {}))
        self.model = model or get_env("GROQ_MODEL") or DEFAULT_GROQ_MODEL
        self.temperature = temperature

    def _declarations_to_tools(self, declarations: list) -> list:
        """Convert Gemini-style declarations to OpenAI tools array."""
        tools = []
        for decl in declarations:
            params = _gemini_schema_to_openai(decl.get("parameters", {}))
            params.setdefault("type", "object")
            params.setdefault("properties", {})
            tools.append({
                "type": "function",
                "function": {
                    "name": decl["name"],
                    "description": decl.get("description", ""),
                    "parameters": params,
                },
            })
        return tools

    def _to_messages(self, history: list, system_instruction: str) -> list:
        """Convert neutral history to OpenAI messages list.

        Tool call IDs are generated deterministically (by turn + call index) so that
        tool-response turns can reference the IDs from the preceding assistant turn.
        """
        messages = [{"role": "system", "content": system_instruction}]
        # turn_idx -> [id, ...] for model turns that had tool calls
        model_call_ids: dict = {}

        for i, turn in enumerate(history):
            role = turn["role"]
            if role == "user":
                messages.append({"role": "user", "content": turn["text"]})

            elif role == "model":
                fcs = turn.get("function_calls") or []
                if fcs:
                    ids = [f"call_{i}_{j}" for j in range(len(fcs))]
                    model_call_ids[i] = ids
                    messages.append({
                        "role": "assistant",
                        "content": turn.get("text") or None,
                        "tool_calls": [
                            {
                                "id": ids[j],
                                "type": "function",
                                "function": {
                                    "name": fc["name"],
                                    "arguments": json.dumps(fc.get("args") or {}),
                                },
                            }
                            for j, fc in enumerate(fcs)
                        ],
                    })
                else:
                    messages.append({"role": "assistant", "content": turn.get("text") or ""})

            elif role == "tool":
                # find the most recent model turn's call IDs
                prev_ids: list = []
                for j in range(i - 1, -1, -1):
                    if history[j]["role"] == "model" and j in model_call_ids:
                        prev_ids = model_call_ids[j]
                        break
                for k, resp in enumerate(turn["responses"]):
                    call_id = prev_ids[k] if k < len(prev_ids) else f"call_unk_{i}_{k}"
                    result = resp["result"]
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json.dumps(
                            result if isinstance(result, dict) else {"result": result}
                        ),
                    })

        return messages

    def generate(self, history: list, system_instruction: str, declarations: list) -> dict:
        messages = self._to_messages(history, system_instruction)
        tools = self._declarations_to_tools(declarations)

        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=self.temperature,
        )

        msg = resp.choices[0].message
        function_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                function_calls.append({"name": tc.function.name, "args": args})

        return {
            "text": msg.content or None,
            "function_calls": function_calls,
        }


class SarvamBackend(LLMBackend):
    """Sarvam AI backend (OpenAI-compatible API) — sarvam-m, sarvam-30b, sarvam-105b.

    Uses the openai package pointed at Sarvam's base URL. Authentication is via
    Bearer token (openai SDK sends Authorization: Bearer <key> automatically).
    Supports OpenAI-style function calling — same conversion as GroqBackend.
    """

    def __init__(self, api_key: str = None, model: str = None, temperature: float = 0.0):
        from openai import OpenAI

        api_key = api_key or get_env("SARVAM_API_KEY")
        if not api_key or "<" in api_key:
            raise RuntimeError(
                "SARVAM_API_KEY is unset or still a placeholder in .env. "
                "Add your Sarvam AI key to use this backend."
            )
        http_client = _make_httpx_client()
        self.client = OpenAI(api_key=api_key, base_url=SARVAM_BASE_URL,
                             **({'http_client': http_client} if http_client else {}))
        self.model = model or get_env("SARVAM_MODEL") or DEFAULT_SARVAM_MODEL
        self.temperature = temperature

    def generate(self, history: list, system_instruction: str, declarations: list) -> dict:
        messages = GroqBackend._to_messages(self, history, system_instruction)
        tools = GroqBackend._declarations_to_tools(self, declarations)

        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=self.temperature,
        )

        msg = resp.choices[0].message
        function_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                function_calls.append({"name": tc.function.name, "args": args})

        return {
            "text": msg.content or None,
            "function_calls": function_calls,
        }


class ScriptedBackend(LLMBackend):
    """Deterministic backend for tests/offline runs — no network, no API key.

    Construct with a list of steps; each generate() call returns the next step. A step is
    either a dict (returned verbatim) or a callable(history)->dict for history-aware
    scripting. Raises if the orchestrator asks for more steps than were scripted.
    """

    def __init__(self, steps: list):
        self._steps = list(steps)
        self._i = 0

    def generate(self, history: list, system_instruction: str, declarations: list) -> dict:
        if self._i >= len(self._steps):
            raise AssertionError("ScriptedBackend ran out of scripted steps.")
        step = self._steps[self._i]
        self._i += 1
        out = step(history) if callable(step) else step
        out.setdefault("text", None)
        out.setdefault("function_calls", [])
        return out
