"""
VYOM agent orchestrator — LangGraph-based, 5-node intent-routed graph.

Topology:
  classify ─┬─ conversational / out_of_scope → respond → END   (NO tools bound)
            └─ discover / analyze            → agent ⇄ tools → synthesize → END

Nodes:
  • classify   — one cheap LLM call (no tools) → {intent, hazard, event_key}. Robust
                 JSON parse with an "analyze" fallback, so a bad classify never breaks.
  • respond    — conversational / honest-refusal replies. Uses the base model with NO
                 tools bound, so greetings can NEVER trigger a tool chain.
  • agent      — the worker LLM (tools bound). The classified hazard/intent is injected
                 as a hint into the system prompt.
  • tools      — enforces coverage-first (guarded on enforce_coverage_first for the
                 ablation arm) and dispatches the REGISTRY.
  • synthesize — deterministic: guarantees a final answer (fallback from tool results
                 if the agent produced none) and strips stray boilerplate. No LLM call,
                 so it cannot fabricate numbers.

``VyomAgent`` keeps backward compatibility: passing a legacy ``LLMBackend`` (e.g.
ScriptedBackend used in tests) falls back to the original manual loop unchanged.
"""

import json
import re
from typing import Annotated, Literal

from typing_extensions import TypedDict

from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langchain_core.messages import (
    AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage,
)

from . import tools
from .prompts import (
    SYSTEM_PROMPT, CLASSIFY_PROMPT, CONVERSATIONAL_PROMPT, OUT_OF_SCOPE_PROMPT,
    context_hint,
)
from ..config import get_env

_PRE_COVERAGE_ALLOWED = {"list_events", "get_event_aoi", "check_coverage"}
_INTENTS = {"conversational", "discover", "analyze", "out_of_scope"}
_NO_TOOL_INTENTS = {"conversational", "out_of_scope"}


# ── State ────────────────────────────────────────────────────────────────────────

class _State(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    coverage_checked: bool
    tool_calls_log: list[dict]
    step_count: int
    intent: str
    hazard: str
    event_key: str


# ── Tool specs (convert Gemini declarations → OpenAI format for bind_tools) ──────

def _build_tool_specs() -> list[dict]:
    from .llm import _gemini_schema_to_openai
    specs = []
    for decl in tools.DECLARATIONS:
        params = _gemini_schema_to_openai(decl.get("parameters") or {})
        params.setdefault("type", "object")
        params.setdefault("properties", {})
        params.setdefault("additionalProperties", False)
        specs.append({
            "type": "function",
            "strict": True,   # enforced server-side on OpenAI-compatible backends
            "function": {
                "name": decl["name"],
                "description": decl.get("description", ""),
                "parameters": params,
                "strict": True,
            },
        })
    return specs


# ── classify helpers ─────────────────────────────────────────────────────────────

def _parse_classify(text: str) -> dict:
    """Parse the classifier's JSON output robustly. Defaults to 'analyze' on any doubt."""
    default = {"intent": "analyze", "hazard": "", "event_key": ""}
    if not text:
        return default
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group(0))
            intent = str(obj.get("intent", "")).strip().lower()
            if intent not in _INTENTS:
                intent = "analyze"
            return {
                "intent": intent,
                "hazard": str(obj.get("hazard", "") or "").strip().lower(),
                "event_key": str(obj.get("event_key", "") or "").strip(),
            }
        except Exception:
            pass
    # Keyword fallback if the model didn't emit clean JSON.
    low = text.lower()
    for kw in _INTENTS:
        if kw in low:
            return {"intent": kw, "hazard": "", "event_key": ""}
    return default


def _first_human_text(messages: list[BaseMessage]) -> str:
    for m in messages:
        if isinstance(m, HumanMessage):
            return m.content if isinstance(m.content, str) else str(m.content)
    return ""


# ── synthesize helpers (deterministic — no LLM, no fabrication) ──────────────────

_BOILERPLATE = (
    "next step", "next steps", "if you'd like", "if you would like", "let me know",
    "feel free to", "i'm happy to", "i am happy to", "would you like",
)


def _clean_answer(text: str) -> str:
    """Backstop formatting: drop lines that START with known boilerplate; trim blanks."""
    if not text:
        return text or ""
    kept = []
    for line in text.splitlines():
        stripped = line.strip().lower().lstrip("-•* ").strip()
        if any(stripped.startswith(b) for b in _BOILERPLATE):
            continue
        kept.append(line)
    # Collapse 3+ consecutive blank lines.
    out = re.sub(r"\n\s*\n\s*\n+", "\n\n", "\n".join(kept)).strip()
    return out


def _fallback_answer(tool_calls_log: list[dict]) -> str:
    """Factual fallback when the agent stopped without a textual answer."""
    if not tool_calls_log:
        return ("I couldn't complete the analysis for this query. Please try "
                "rephrasing or naming a specific disaster event.")
    names = list(dict.fromkeys(e.get("name") for e in tool_calls_log if e.get("name")))
    return ("I ran " + ", ".join(names) + " but did not produce a final written "
            "summary. The tool trace, map layers and charts show the results.")


# ── Graph nodes ───────────────────────────────────────────────────────────────────

def _make_classify_node(base_llm):
    def classify_node(state: _State) -> dict:
        query = _first_human_text(state["messages"])
        try:
            resp = base_llm.invoke([
                SystemMessage(content=CLASSIFY_PROMPT),
                HumanMessage(content=query),
            ])
            parsed = _parse_classify(resp.content)
        except Exception:
            parsed = {"intent": "analyze", "hazard": "", "event_key": ""}
        return {
            "intent": parsed["intent"],
            "hazard": parsed["hazard"],
            "event_key": parsed["event_key"],
            "step_count": state["step_count"] + 1,
        }
    return classify_node


# Defaults used when the backend returns empty content (Sarvam occasionally does).
_DEFAULT_CONVERSATIONAL = (
    "Hi! I'm VYOM, a GIS analyst for Indian disaster events. I can analyse flood water "
    "extent, burn severity, drought vegetation stress and landslide impact for "
    "registered events — and produce maps and charts. Which event shall we look at?"
)
_DEFAULT_OUT_OF_SCOPE = (
    "I can't help with that — I only do retrospective analysis of archived ISRO "
    "ResourceSat scenes for a fixed set of past disaster events (no forecasting or "
    "live data). Ask me about a registered flood, wildfire, drought or landslide event."
)


def _make_respond_node(base_llm):
    """Conversational / out-of-scope replies. base_llm has NO tools bound.

    Guarded against empty completions (the active backend intermittently returns
    empty content): one retry, then a sensible static default — never an empty reply.
    """
    def respond_node(state: _State) -> dict:
        out_of_scope = state.get("intent") == "out_of_scope"
        prompt = OUT_OF_SCOPE_PROMPT if out_of_scope else CONVERSATIONAL_PROMPT
        msgs = [SystemMessage(content=prompt)] + list(state["messages"])

        content = ""
        for _ in range(2):  # one retry on empty/error
            try:
                resp = base_llm.invoke(msgs)
                content = (resp.content or "").strip()
                if content:
                    break
            except Exception:
                content = ""
        if not content:
            content = _DEFAULT_OUT_OF_SCOPE if out_of_scope else _DEFAULT_CONVERSATIONAL
        return {"messages": [AIMessage(content=content)],
                "step_count": state["step_count"] + 1}
    return respond_node


def _make_agent_node(llm_with_tools):
    def agent_node(state: _State) -> dict:
        hint = context_hint(state.get("intent", ""), state.get("hazard", ""),
                            state.get("event_key", ""))
        msgs = [SystemMessage(content=SYSTEM_PROMPT + hint)] + list(state["messages"])
        response = llm_with_tools.invoke(msgs)
        return {"messages": [response], "step_count": state["step_count"] + 1}
    return agent_node


def _make_tool_node(enforce: bool):
    def tool_node(state: _State) -> dict:
        last: AIMessage = state["messages"][-1]
        coverage_checked = state["coverage_checked"]
        tool_messages: list[ToolMessage] = []
        log_entries: list[dict] = []

        for tc in last.tool_calls:
            name = tc["name"]
            args = tc.get("args") or {}

            if name == "check_coverage":
                coverage_checked = True

            if enforce and name not in _PRE_COVERAGE_ALLOWED and not coverage_checked:
                result: dict = {
                    "error": (
                        "Policy: call check_coverage before any data/raster tool. "
                        "Sequence: list_events() → get_event_aoi(event_key) → "
                        "check_coverage(aoi_geojson) → then data tools."
                    )
                }
            else:
                raw = tools.dispatch(name, args)
                result = raw if isinstance(raw, dict) else {"result": raw}

            tool_messages.append(ToolMessage(
                content=json.dumps(result),
                tool_call_id=tc["id"],
                name=name,
            ))
            log_entries.append({
                "name": name,
                "args": args,
                "heavy": name in tools.HEAVY_TOOLS,
                "result": result,
            })

        return {
            "messages": tool_messages,
            "coverage_checked": coverage_checked,
            "tool_calls_log": state["tool_calls_log"] + log_entries,
        }
    return tool_node


def _synthesize_node(state: _State) -> dict:
    """Deterministic final pass: guarantee a clean, non-empty answer."""
    answer = None
    for msg in reversed(state["messages"]):
        if (isinstance(msg, AIMessage) and not getattr(msg, "tool_calls", None)
                and msg.content):
            answer = msg.content
            break
    answer = _clean_answer(answer)
    if not answer:
        answer = _fallback_answer(state.get("tool_calls_log") or [])
    return {"messages": [AIMessage(content=answer)], "step_count": state["step_count"]}


# ── Routing ────────────────────────────────────────────────────────────────────

def _route_intent(state: _State) -> Literal["respond", "agent"]:
    return "respond" if state.get("intent") in _NO_TOOL_INTENTS else "agent"


def _route_agent(state: _State) -> Literal["tools", "synthesize"]:
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
        return "tools"
    return "synthesize"


# ── Graph builder ────────────────────────────────────────────────────────────────

def _build_graph(base_llm, llm_with_tools, enforce: bool = True):
    builder = StateGraph(_State)
    builder.add_node("classify", _make_classify_node(base_llm))
    builder.add_node("respond", _make_respond_node(base_llm))
    builder.add_node("agent", _make_agent_node(llm_with_tools))
    builder.add_node("tools", _make_tool_node(enforce))
    builder.add_node("synthesize", _synthesize_node)

    builder.set_entry_point("classify")
    builder.add_conditional_edges("classify", _route_intent)
    builder.add_edge("respond", END)
    builder.add_conditional_edges("agent", _route_agent)
    builder.add_edge("tools", "agent")
    builder.add_edge("synthesize", END)
    return builder.compile()


# ── LangChain model factory ───────────────────────────────────────────────────────

def _get_langchain_model(model_override: str = None):
    """Return a LangChain ChatModel for the active backend (Sarvam > Groq > Gemini)."""
    from .llm import (
        SARVAM_BASE_URL, DEFAULT_SARVAM_MODEL, DEFAULT_GROQ_MODEL, DEFAULT_MODEL,
        _make_httpx_client,
    )

    sarvam_key = get_env("SARVAM_API_KEY")
    groq_key = get_env("GROQ_API_KEY")

    if sarvam_key and "<" not in sarvam_key:
        from langchain_openai import ChatOpenAI
        http = _make_httpx_client()
        kw: dict = {}
        if http is not None:
            kw["http_client"] = http
        return ChatOpenAI(
            api_key=sarvam_key,
            base_url=SARVAM_BASE_URL,
            model=model_override or get_env("SARVAM_MODEL") or DEFAULT_SARVAM_MODEL,
            temperature=0,
            **kw,
        )

    if groq_key and "<" not in groq_key:
        from langchain_openai import ChatOpenAI
        http = _make_httpx_client()
        kw = {}
        if http is not None:
            kw["http_client"] = http
        return ChatOpenAI(
            api_key=groq_key,
            base_url="https://api.groq.com/openai/v1",
            model=model_override or get_env("GROQ_MODEL") or DEFAULT_GROQ_MODEL,
            temperature=0,
            **kw,
        )

    gemini_key = get_env("GEMINI_API_KEY")
    if not gemini_key or "<" in gemini_key:
        raise RuntimeError("No LLM API key found. Set SARVAM_API_KEY, GROQ_API_KEY, or GEMINI_API_KEY in .env.")
    from langchain_google_genai import ChatGoogleGenerativeAI
    return ChatGoogleGenerativeAI(
        model=model_override or get_env("GEMINI_MODEL") or DEFAULT_MODEL,
        google_api_key=gemini_key,
        temperature=0,
    )


# ── Result helpers ─────────────────────────────────────────────────────────────

def _msg_to_dict(msg: BaseMessage) -> dict:
    """Convert a LangChain message to a JSON-serialisable dict for the history field."""
    if isinstance(msg, HumanMessage):
        return {"role": "user", "text": msg.content}
    if isinstance(msg, AIMessage):
        fcs = [{"name": tc["name"], "args": tc.get("args", {})}
               for tc in (msg.tool_calls or [])]
        return {"role": "model", "text": msg.content or None, "function_calls": fcs}
    if isinstance(msg, ToolMessage):
        try:
            content = json.loads(msg.content)
        except Exception:
            content = msg.content
        return {"role": "tool", "name": msg.name, "result": content}
    return {"role": "system", "text": str(msg.content)}


# ── Public agent class ────────────────────────────────────────────────────────────

class VyomAgent:
    """Drives a single natural-language query to an evidence-grounded answer.

    Usage::
        agent = VyomAgent(max_steps=12)
        result = agent.run("How did water area change in the Kerala 2018 floods?")

    ``backend`` is accepted for backward compatibility (tests use ScriptedBackend).
    When a legacy LLMBackend is passed the old manual loop is used instead of LangGraph.
    """

    def __init__(self, backend=None, max_steps: int = 12,
                 enforce_coverage_first: bool = True, model: str = None):
        self.max_steps = max_steps
        self._enforce = enforce_coverage_first

        # Detect legacy backend (ScriptedBackend / old LLMBackend) → old loop
        from .llm import LLMBackend
        if backend is not None and isinstance(backend, LLMBackend):
            self._backend = backend
            self._use_legacy = True
            return

        self._use_legacy = False
        lc_model = _get_langchain_model(model_override=model)
        self._base_llm = lc_model
        tool_specs = _build_tool_specs()
        llm_with_tools = lc_model.bind_tools(tool_specs)
        # recursion budget: classify(1) + agent⇄tools loop + synthesize(1) + buffer.
        self._recursion_limit = max_steps * 2 + 8
        self._graph = _build_graph(lc_model, llm_with_tools, enforce_coverage_first)

    def run(self, query: str) -> dict:
        if self._use_legacy:
            return self._run_legacy(query)
        return self._run_langgraph(query)

    # ── LangGraph path ──────────────────────────────────────────────────────────

    def _run_langgraph(self, query: str) -> dict:
        from langgraph.errors import GraphRecursionError

        initial: _State = {
            "messages": [HumanMessage(content=query)],
            "coverage_checked": False,
            "tool_calls_log": [],
            "step_count": 0,
            "intent": "",
            "hazard": "",
            "event_key": "",
        }

        stopped = "max_steps"
        try:
            final = self._graph.invoke(
                initial,
                config={"recursion_limit": self._recursion_limit},
            )
            stopped = "answer"
        except GraphRecursionError:
            final = initial

        # Last non-tool-calling AI message is the answer (synthesize / respond emit it).
        answer = None
        for msg in reversed(final.get("messages", [])):
            if isinstance(msg, AIMessage) and not getattr(msg, "tool_calls", None):
                answer = msg.content
                break

        tool_calls = [
            {"step": i + 1, **entry}
            for i, entry in enumerate(final.get("tool_calls_log", []))
        ]

        return {
            "answer": answer,
            "tool_calls": tool_calls,
            "coverage_checked": final.get("coverage_checked", False),
            "steps": final.get("step_count", 0),
            "stopped": stopped,
            "intent": final.get("intent", ""),
            "hazard": final.get("hazard", ""),
            "history": [_msg_to_dict(m) for m in final.get("messages", [])],
        }

    # ── Legacy path (for ScriptedBackend / tests) ───────────────────────────────

    def _run_legacy(self, query: str) -> dict:
        history = [{"role": "user", "text": query}]
        tool_calls_log = []
        coverage_checked = False
        stopped = "max_steps"
        step = 1

        for step in range(1, self.max_steps + 1):
            out = self._backend.generate(history, SYSTEM_PROMPT, tools.DECLARATIONS)
            fcs = out.get("function_calls") or []

            if not fcs:
                history.append({"role": "model", "text": out.get("text")})
                stopped = "answer"
                break

            history.append({
                "role": "model",
                "text": out.get("text"),
                "function_calls": fcs,
            })
            responses = []
            for fc in fcs:
                name, args = fc["name"], fc.get("args") or {}
                if name == "check_coverage":
                    coverage_checked = True
                if name not in _PRE_COVERAGE_ALLOWED and not coverage_checked and self._enforce:
                    result = {"error": "Policy: call check_coverage before any data tool."}
                else:
                    result = tools.dispatch(name, args)
                responses.append({"name": name, "result": result})
                tool_calls_log.append({
                    "step": step, "name": name, "args": args,
                    "heavy": name in tools.HEAVY_TOOLS, "result": result,
                })
            history.append({"role": "tool", "responses": responses})

        answer = None
        if history and history[-1]["role"] == "model":
            answer = history[-1].get("text")

        return {
            "answer": answer,
            "tool_calls": tool_calls_log,
            "coverage_checked": coverage_checked,
            "steps": step,
            "stopped": stopped,
            "history": history,
        }


def run_query(query: str, backend=None, max_steps: int = 12) -> dict:
    """Convenience wrapper: build an agent and answer one query."""
    return VyomAgent(backend=backend, max_steps=max_steps).run(query)
