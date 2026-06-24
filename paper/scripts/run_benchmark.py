"""
VYOM agentic tool-use benchmark.

Runs a fixed suite of natural-language disaster-analysis queries against the LIVE
agent (Sarvam-30B backend by default) and records, per query: the tool-call
sequence, number of model turns, latency, check_coverage-first compliance, and an
automatic pass/fail against an expected behaviour.

The suite deliberately mixes:
  - in-coverage analysis (Kerala 2018, the only ingested event),
  - honest no-data cases (events with a manifest but not yet ingested, and an
    out-of-India ocean AOI) — testing that the agent refuses to fabricate.

Run from project root:
    PYTHONPATH=src PYTHONIOENCODING=utf-8 ./myenv/Scripts/python.exe paper/scripts/run_benchmark.py
"""
import os, json, time, traceback

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
from vyom.agent.orchestrator import VyomAgent

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUT = os.path.join(ROOT, "paper", "results")
os.makedirs(OUT, exist_ok=True)

# Each case: id, query, category, and a checker over the run dict.
SUITE = [
    dict(id="kerala_change", category="in_coverage",
         query="How did open water area change during the Kerala 2018 floods?",
         expect=lambda r: r["coverage_checked"] and any(
             t in r["tools"] for t in ("compare_windows", "flood_extent", "compute_change"))),
    dict(id="kerala_flood_extent", category="in_coverage",
         query="What was the flooded area in square kilometres for the Kerala Periyar 2018 event?",
         expect=lambda r: r["coverage_checked"]),
    dict(id="kerala_coverage", category="in_coverage",
         query="Do we have any satellite imagery over the Kerala Periyar basin?",
         expect=lambda r: r["coverage_checked"] and "check_coverage" in r["tools"]),
    dict(id="kerala_scenes", category="in_coverage",
         query="How many scenes and which sensors are available for the Kerala 2018 floods?",
         expect=lambda r: r["coverage_checked"]),
    dict(id="kerala_cloud", category="in_coverage",
         query="List the least cloudy scenes for the Kerala 2018 floods.",
         expect=lambda r: r["coverage_checked"]),
    dict(id="kerala_ndwi", category="in_coverage",
         query="What is the mean NDWI before versus after the Kerala 2018 flood?",
         expect=lambda r: r["coverage_checked"]),
    dict(id="assam_nodata", category="no_data",
         query="How severe were the Assam Brahmaputra 2022 floods in our imagery?",
         expect=lambda r: r["coverage_checked"] and _says_no_data(r)),
    dict(id="uttarakhand_fire_nodata", category="no_data",
         query="Map the burn scars from the 2016 Uttarakhand forest fires.",
         expect=lambda r: r["coverage_checked"] and _says_no_data(r)),
    dict(id="ocean_nodata", category="no_data",
         query="Analyse flooding for the area around longitude 65, latitude 5 in the Arabian Sea.",
         expect=lambda r: _says_no_data(r) or not r["tools"]),
    dict(id="marathwada_drought_nodata", category="no_data",
         query="Assess the Marathwada 2016 agricultural drought from NDVI.",
         expect=lambda r: r["coverage_checked"] and _says_no_data(r)),
]

_NO_DATA_CUES = ["no data", "no coverage", "do not have", "don't have", "no scenes",
                 "not available", "no imagery", "not been ingested", "not ingested",
                 "no matching", "not cover", "lack", "cannot find", "unable to find"]


def _says_no_data(r):
    ans = (r.get("answer") or "").lower()
    return any(c in ans for c in _NO_DATA_CUES)


def run_case(agent, case):
    t = time.time()
    try:
        res = agent.run(case["query"])
        latency = time.time() - t
        tools = [tc["name"] for tc in res["tool_calls"]]
        heavy = [tc["name"] for tc in res["tool_calls"] if tc.get("heavy")]
        first_data = next((tc["name"] for tc in res["tool_calls"]
                           if tc["name"] not in ("list_events", "get_event_aoi")), None)
        compliant = (first_data in (None, "check_coverage"))
        row = dict(
            id=case["id"], category=case["category"], query=case["query"],
            latency_s=round(latency, 2), steps=res["steps"], stopped=res["stopped"],
            coverage_checked=res["coverage_checked"],
            coverage_first_compliant=compliant,
            tools=tools, n_tool_calls=len(tools), n_heavy=len(heavy),
            answer=(res.get("answer") or "")[:1200],
        )
        row["passed"] = bool(case["expect"](row)) and compliant
        return row
    except Exception as e:
        return dict(id=case["id"], category=case["category"], query=case["query"],
                    latency_s=round(time.time() - t, 2), steps=0, stopped="error",
                    coverage_checked=False, coverage_first_compliant=False,
                    tools=[], n_tool_calls=0, n_heavy=0,
                    answer="ERROR: " + str(e), passed=False, error=traceback.format_exc())


def main():
    agent = VyomAgent(max_steps=12)
    backend = type(agent.backend).__name__
    print(f"Backend: {backend} — running {len(SUITE)} cases\n")
    runs = []
    for case in SUITE:
        print(f"  [{case['id']}] ...", end="", flush=True)
        row = run_case(agent, case)
        runs.append(row)
        print(f" {'PASS' if row['passed'] else 'FAIL'} "
              f"({row['latency_s']}s, {row['steps']} steps, tools={row['tools']})")
    npass = sum(r["passed"] for r in runs)
    ncov = sum(r["coverage_first_compliant"] for r in runs)
    summary = dict(
        backend=backend, n=len(runs), passed=npass,
        pass_rate=round(npass / len(runs), 3),
        coverage_first_compliance=round(ncov / len(runs), 3),
        mean_latency_s=round(sum(r["latency_s"] for r in runs) / len(runs), 2),
        mean_steps=round(sum(r["steps"] for r in runs) / len(runs), 2),
        example_sequence=next((r["tools"] for r in runs if r["id"] == "kerala_change"), None),
        runs=runs,
    )
    with open(os.path.join(OUT, "benchmark_results.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    # human-readable CSV
    import csv
    with open(os.path.join(OUT, "benchmark_summary.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "category", "passed", "coverage_first", "latency_s", "steps",
                    "n_tool_calls", "n_heavy", "tool_sequence"])
        for r in runs:
            w.writerow([r["id"], r["category"], r["passed"], r["coverage_first_compliant"],
                        r["latency_s"], r["steps"], r["n_tool_calls"], r["n_heavy"],
                        " > ".join(r["tools"])])
    print(f"\n{npass}/{len(runs)} passed · coverage-first {ncov}/{len(runs)} · "
          f"mean {summary['mean_latency_s']}s/{summary['mean_steps']} steps")
    print("wrote results/benchmark_results.json + benchmark_summary.csv")


if __name__ == "__main__":
    main()
