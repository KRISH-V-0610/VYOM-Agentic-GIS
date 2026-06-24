"""
Batch ingest benchmark events through the ARD pipeline.

Idempotent: scenes already ingested at the current PIPELINE_VERSION are skipped,
so re-running safely resumes. A scene that raises is caught and counted as FAIL —
one bad scene never kills the whole run.

Examples:
    # all default events
    myenv/Scripts/python.exe -u run_ingest_all.py 2>&1 | tee -a logs/ingest_all.log

    # one event
    myenv/Scripts/python.exe -u run_ingest_all.py kerala_periyar_2018

    # several events, in the order given
    myenv/Scripts/python.exe -u run_ingest_all.py assam_brahmaputra_2022 bihar_kosi_ganga_2019

    # list the known events and exit
    myenv/Scripts/python.exe -u run_ingest_all.py --list
"""
import argparse
import sys
import time

sys.path.insert(0, "src")

from vyom.ard.pipeline import read_manifest, process_scene
from vyom.config import PIPELINE_VERSION, DATA_MANIFESTS

# Default set (the 6 benchmark events) when no event is given on the command line.
DEFAULT_EVENTS = [
    "kerala_periyar_2018",
    "assam_brahmaputra_2022",
    "bihar_kosi_ganga_2019",
    "wildfire_uttarakhand_2016",
    "landslide_sikkim_glof_2023",
    "drought_marathwada_2016",
]


def known_events() -> list[str]:
    """Every event that has a manifest CSV on disk, sorted."""
    return sorted(p.stem for p in DATA_MANIFESTS.glob("*.csv"))


def ingest_event(event: str) -> tuple[int, int, int, int]:
    """Process every scene in one event's manifest. Returns (total, ok, skip, fail)."""
    rows = read_manifest(event)
    print(f"\n{'='*60}", flush=True)
    print(f"EVENT: {event}  ({len(rows)} scenes)", flush=True)
    print(f"{'='*60}", flush=True)
    ok = skip = fail = 0
    t0 = time.time()
    for i, r in enumerate(rows, 1):
        print(f"  [{i:>3}/{len(rows)}] processing {r.get('scene_id', '?')} ...", flush=True, end="\r")
        try:
            status = process_scene(r)
        except Exception as e:
            status = f"FAIL (exception): {r.get('scene_id', '?')} -> {type(e).__name__}: {e}"
        tag = status[:2]
        if tag == "OK":
            ok += 1
        elif tag == "SK":
            skip += 1
        else:
            fail += 1
        print(f"  [{i:>3}/{len(rows)}] {status}", flush=True)
    elapsed = time.time() - t0
    print(f"\n  => {ok} processed, {skip} skipped, {fail} failed in {elapsed:.0f}s", flush=True)
    return len(rows), ok, skip, fail


def main() -> int:
    ap = argparse.ArgumentParser(description="Batch-ingest benchmark events through the ARD pipeline")
    ap.add_argument("events", nargs="*", help="event_key(s) to ingest (default: the 6 benchmark events)")
    ap.add_argument("--list", action="store_true", help="list known events (manifests on disk) and exit")
    args = ap.parse_args()

    available = known_events()
    if args.list:
        print(f"Known events ({len(available)}):")
        for e in available:
            print(f"  {e}")
        return 0

    events = args.events or DEFAULT_EVENTS

    # Validate up front so a typo fails immediately instead of mid-run.
    unknown = [e for e in events if e not in available]
    if unknown:
        print(f"ERROR: unknown event(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"Known events: {', '.join(available)}", file=sys.stderr)
        return 2

    print(f"Ingesting {len(events)} event(s) @ {PIPELINE_VERSION}: {', '.join(events)}", flush=True)

    grand_total = grand_ok = grand_skip = grand_fail = 0
    run_start = time.time()
    for event in events:
        total, ok, skip, fail = ingest_event(event)
        grand_total += total
        grand_ok += ok
        grand_skip += skip
        grand_fail += fail

    print(f"\n{'='*60}", flush=True)
    print(f"ALL DONE: {grand_ok} processed, {grand_skip} skipped, {grand_fail} failed "
          f"({grand_total} total) in {(time.time()-run_start)/60:.1f} min", flush=True)
    return 1 if grand_fail else 0


if __name__ == "__main__":
    sys.exit(main())
