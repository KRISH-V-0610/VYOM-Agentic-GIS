"""
VYOM agent orchestrator — the natural-language → tools → answer loop.

Owns a neutral conversation history (see llm.py) and drives an injected LLMBackend through
a manual function-calling loop:

    user query
      → backend proposes tool calls
      → orchestrator dispatches them against the live MCP tool functions
      → results fed back
      → repeat until the model returns a text-only answer (or max_steps hit)

The backend is injectable so the same loop runs against Gemini in production and a scripted
backend in tests. The check_coverage-first rule is enforced here, not just suggested in the
prompt: the first tool the model calls in a run must be check_coverage (the AOI-resolving
list_events / get_event_aoi helpers are exempt, since the agent needs them to build an AOI).
"""

from . import tools
from .prompts import SYSTEM_PROMPT
from ..config import get_env

# Tools allowed before check_coverage — they only resolve an AOI, they don't read data.
_PRE_COVERAGE_ALLOWED = {"list_events", "get_event_aoi", "check_coverage"}


def _default_backend():
    """Pick the active LLM backend from .env: Sarvam > Groq > Gemini."""
    if get_env("SARVAM_API_KEY"):
        from .llm import SarvamBackend
        return SarvamBackend()
    if get_env("GROQ_API_KEY"):
        from .llm import GroqBackend
        return GroqBackend()
    from .llm import GeminiBackend
    return GeminiBackend()


class VyomAgent:
    """Drives a single natural-language query to an evidence-grounded answer."""

    def __init__(self, backend=None, max_steps: int = 12):
        self.backend = backend or _default_backend()
        self.max_steps = max_steps

    def run(self, query: str) -> dict:
        """Answer one query. Returns a structured result with full provenance.

        Returns:
            {
              "answer": str|None,            # final natural-language answer
              "tool_calls": [{step, name, args, result}, ...],
              "coverage_checked": bool,      # was check_coverage called this run?
              "steps": int,                  # model turns taken
              "stopped": "answer"|"max_steps",
              "history": [...],              # full neutral history for debugging
            }
        """
        history = [{"role": "user", "text": query}]
        tool_calls = []
        coverage_checked = False
        stopped = "max_steps"

        for step in range(1, self.max_steps + 1):
            out = self.backend.generate(history, SYSTEM_PROMPT, tools.DECLARATIONS)
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

                if name not in _PRE_COVERAGE_ALLOWED and not coverage_checked:
                    result = {
                        "error": "Policy: call check_coverage for the AOI before any "
                                 "data/raster tool. Use list_events + get_event_aoi to "
                                 "build the AOI, then check_coverage.",
                    }
                else:
                    result = tools.dispatch(name, args)

                responses.append({"name": name, "result": result})
                tool_calls.append({
                    "step": step, "name": name, "args": args,
                    "heavy": name in tools.HEAVY_TOOLS, "result": result,
                })

            history.append({"role": "tool", "responses": responses})

        answer = None
        if history and history[-1]["role"] == "model":
            answer = history[-1].get("text")

        return {
            "answer": answer,
            "tool_calls": tool_calls,
            "coverage_checked": coverage_checked,
            "steps": step,
            "stopped": stopped,
            "history": history,
        }


def run_query(query: str, backend=None, max_steps: int = 12) -> dict:
    """Convenience wrapper: build an agent and answer one query."""
    return VyomAgent(backend=backend, max_steps=max_steps).run(query)
