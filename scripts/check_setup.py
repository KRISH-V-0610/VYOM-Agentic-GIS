"""
VYOM setup pre-flight checker.

Run via Check-VYOM-Setup.bat (double-click) or:
    set PYTHONPATH=src && python scripts/check_setup.py

Prints a green/red checklist verifying the machine is ready to serve VYOM:
  - Python version
  - required packages (the full server import chain)
  - .env file + required keys
  - PostgreSQL / PostGIS database connection + ingested data
  - config registries (events / bands)

Exit code 0 = all critical checks passed, 1 = at least one critical failure.
"""
import os
import sys

# Force UTF-8 so the check marks render in the Windows console.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OK = "[ OK ]"
FAIL = "[FAIL]"
WARN = "[WARN]"

# Tally: critical failures flip the final exit code; warnings do not.
_failures = 0
_warnings = 0


def line(status, label, detail=""):
    msg = f"  {status}  {label}"
    if detail:
        msg += f"\n           -> {detail}"
    print(msg)


def crit(passed, label, detail_ok="", detail_fail=""):
    global _failures
    if passed:
        line(OK, label, detail_ok)
    else:
        _failures += 1
        line(FAIL, label, detail_fail)
    return passed


def warn(passed, label, detail_ok="", detail_warn=""):
    global _warnings
    if passed:
        line(OK, label, detail_ok)
    else:
        _warnings += 1
        line(WARN, label, detail_warn)
    return passed


def header(text):
    print()
    print("-" * 67)
    print(f"  {text}")
    print("-" * 67)


def main():
    print("=" * 67)
    print("  VYOM - Setup Pre-flight Check")
    print("=" * 67)

    # ---- 1. Python ----------------------------------------------------
    header("1. Python environment")
    v = sys.version_info
    crit(v >= (3, 9), f"Python version: {v.major}.{v.minor}.{v.micro}",
         detail_fail="Python 3.9+ required (3.11 recommended).")

    # ---- 2. Packages (single import loads the whole server chain) -----
    header("2. Required packages")
    # Ensure src is importable even if PYTHONPATH was not set.
    src = os.path.join(ROOT, "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    try:
        import vyom.api.app  # noqa: F401  (loads fastapi, langgraph, rasterio, ...)
        crit(True, "All server dependencies importable")
    except Exception as exc:
        crit(False, "Server dependencies",
             detail_fail=f"{type(exc).__name__}: {exc}  "
                         f"(run: pip install -r requirements.txt)")

    # Report a few key packages individually for clarity.
    for pkg in ("fastapi", "uvicorn", "rasterio", "psycopg2",
                "langgraph", "openai", "matplotlib"):
        try:
            __import__(pkg)
            line(OK, f"package: {pkg}")
        except Exception:
            _bump_fail()
            line(FAIL, f"package: {pkg}", "missing")

    # ---- 3. .env file + keys -----------------------------------------
    header("3. Configuration (.env)")
    env_path = os.path.join(ROOT, ".env")
    has_env = os.path.isfile(env_path)
    crit(has_env, ".env file present",
         detail_fail="Create a .env in the project root (see README).")

    env = {}
    if has_env:
        for ln in open(env_path, encoding="utf-8"):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, val = ln.split("=", 1)
                env[k.strip()] = val.strip().strip('"').strip("'")

    db_set = bool(env.get("DB_URL")) and "user:pass" not in env.get("DB_URL", "")
    crit(db_set, "DB_URL configured",
         detail_fail="DB_URL is missing or still a template in .env.")

    sarvam = bool(env.get("SARVAM_API_KEY"))
    gemini = bool(env.get("GEMINI_API_KEY"))
    groq = bool(env.get("GROQ_API_KEY"))
    any_llm = sarvam or gemini or groq
    provider = ("Sarvam" if sarvam else "Gemini" if gemini else
                "Groq" if groq else "none")
    crit(any_llm, f"LLM API key configured (active: {provider})",
         detail_fail="Set SARVAM_API_KEY (or GEMINI/GROQ) in .env - "
                     "the /query endpoint needs it.")

    # ---- 4. Database connection --------------------------------------
    header("4. Database (PostgreSQL + PostGIS)")
    if db_set:
        try:
            import psycopg2
            conn = psycopg2.connect(env["DB_URL"], connect_timeout=5)
            cur = conn.cursor()
            # PostGIS present?
            cur.execute("SELECT extname FROM pg_extension WHERE extname='postgis'")
            postgis = cur.fetchone() is not None
            warn(postgis, "PostGIS extension installed",
                 detail_warn="PostGIS not found - raster/geometry queries will fail.")
            # Ingested scenes?
            try:
                cur.execute("SELECT count(*), count(distinct event_key) FROM scenes")
                nscenes, nevents = cur.fetchone()
                warn(nscenes and nscenes > 0,
                     f"Scenes ingested: {nscenes} across {nevents} event(s)",
                     detail_warn="No scenes in the catalogue - queries return no data.")
            except Exception as exc:
                warn(False, "scenes table",
                     detail_warn=f"Could not read scenes table: {exc}")
            conn.close()
            crit(True, "Database connection")
        except Exception as exc:
            crit(False, "Database connection",
                 detail_fail=f"{type(exc).__name__}: {exc}  "
                             f"(is PostgreSQL running? is DB_URL correct?)")
    else:
        crit(False, "Database connection",
             detail_fail="Skipped - DB_URL not configured.")

    # ---- 5. Config registries ----------------------------------------
    header("5. Config registries")
    for name in ("events_registry.yaml", "band_registry.yaml"):
        p = os.path.join(ROOT, "config", name)
        crit(os.path.isfile(p), f"config/{name}",
             detail_fail=f"Missing {name} - the agent cannot resolve events.")

    # ---- summary ------------------------------------------------------
    print()
    print("=" * 67)
    if _failures == 0 and _warnings == 0:
        print("  RESULT:  ALL CHECKS PASSED - VYOM is ready to start.")
    elif _failures == 0:
        print(f"  RESULT:  READY (with {_warnings} warning(s) - see [WARN] above).")
        print("           The server will start; some data may be unavailable.")
    else:
        print(f"  RESULT:  NOT READY - {_failures} critical problem(s) found.")
        print("           Fix the [FAIL] items above, then run this check again.")
    print("=" * 67)
    return 1 if _failures else 0


def _bump_fail():
    global _failures
    _failures += 1


if __name__ == "__main__":
    sys.exit(main())
