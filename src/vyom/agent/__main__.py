"""
CLI for the VYOM agent.

  python -m vyom.agent "How much did water area grow in the Kerala 2018 floods?"
  python -m vyom.agent --show-tools "..."   # also print the tool-call trace
  python -m vyom.agent --max-steps 8 "..."

Requires a real GEMINI_API_KEY in .env. For offline development, drive VyomAgent with a
ScriptedBackend from Python instead of this CLI.
"""

import argparse
import json
import sys

from .orchestrator import VyomAgent


def main():
    ap = argparse.ArgumentParser(description="VYOM agentic GIS — ask a disaster question")
    ap.add_argument("query", help="natural-language question")
    ap.add_argument("--max-steps", type=int, default=12, help="max model turns")
    ap.add_argument("--show-tools", action="store_true", help="print the tool-call trace")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = ap.parse_args()

    try:
        agent = VyomAgent(max_steps=args.max_steps)
    except RuntimeError as exc:
        sys.exit(f"ERROR: {exc}")

    result = agent.run(args.query)

    if args.json:
        # history holds non-serialisable nothing — it's plain dicts — so this is safe.
        print(json.dumps({k: v for k, v in result.items() if k != "history"}, indent=2,
                         default=str))
        return

    if args.show_tools:
        print("── Tool calls ───────────────────────────────────────────")
        for c in result["tool_calls"]:
            tag = " [heavy]" if c["heavy"] else ""
            print(f"  {c['step']:>2}. {c['name']}{tag}  args={c['args']}")
        print(f"  coverage_checked={result['coverage_checked']}  "
              f"steps={result['steps']}  stopped={result['stopped']}")
        print("─────────────────────────────────────────────────────────\n")

    print(result["answer"] or "(no answer produced)")


if __name__ == "__main__":
    main()
