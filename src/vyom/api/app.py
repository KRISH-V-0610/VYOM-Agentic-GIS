"""
FastAPI REST wrapper around the VYOM agent + catalog tools.

Endpoints:
  GET  /health              — liveness + DB connectivity + Gemini-key presence
  GET  /events              — registered disaster events (key, bbox, primary index)
  GET  /scenes              — catalogued scenes (proxies postgis_server.list_scenes)
  POST /query               — run the agent on a natural-language question
  GET  /exports/{filename}  — download a PNG produced by the agent's export_png tool

The server never needs a live LLM key to start — /health, /events, and /scenes work without
one. Only /query requires a key (Groq or Gemini, auto-detected from .env).
"""

import asyncio
import json
import threading
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, HumanMessage

from ..config import PROJECT_ROOT, get_db_url, get_env
from ..agent.orchestrator import VyomAgent
from ..agent import tools
from . import models

app = FastAPI(
    title="VYOM — Agentic GIS for Indian Disaster Analysis",
    description="Natural-language disaster analysis over ISRO ResourceSat imagery.",
    version="0.1.0",
)

# Allow the Vite dev server (web/) to call the API directly. Belt-and-suspenders: the
# Vite proxy already keeps the browser same-origin in dev, but this covers direct calls.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:4173", "http://127.0.0.1:4173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

EXPORTS_DIR = PROJECT_ROOT / "data" / "exports"
ARD_DIR = PROJECT_ROOT / "data" / "ard"

# Serve preprocessed ARD COGs for client-side rendering. Starlette's StaticFiles honours
# HTTP Range requests, which geotiff.js needs to read COG overviews/tiles incrementally.
if ARD_DIR.exists():
    app.mount("/ard", StaticFiles(directory=ARD_DIR), name="ard")


# ── Dependencies ─────────────────────────────────────────────────────────────────


def get_agent() -> VyomAgent:
    """Legacy zero-arg agent dependency (kept for import back-compat)."""
    try:
        return VyomAgent()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def get_agent_factory():
    """Overridable factory dependency for /query.

    Returns a callable ``(max_steps, model) -> VyomAgent`` so per-request max_steps and
    model still flow through, while tests can inject a scripted agent via
    ``app.dependency_overrides[get_agent_factory]``. The agent is built lazily inside
    the endpoint (not at dependency-resolution time), so request-validation failures
    return 422 without ever touching the LLM backend.
    """
    def _make(max_steps: int = 12, model: str | None = None) -> VyomAgent:
        return VyomAgent(max_steps=max_steps, model=model)
    return _make


# ── Routes ─────────────────────────────────────────────────────────────────────────


@app.get("/health", response_model=models.HealthResponse)
def health() -> models.HealthResponse:
    import psycopg2

    from ..config import get_db_url

    db_ok, detail = False, None
    try:
        with psycopg2.connect(get_db_url()) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            db_ok = cur.fetchone()[0] == 1
    except Exception as exc:  # report, don't crash the health endpoint
        detail = f"DB error: {exc}"

    sarvam_key = get_env("SARVAM_API_KEY")
    groq_key = get_env("GROQ_API_KEY")
    if sarvam_key and "<" not in sarvam_key:
        provider = "sarvam"
        key_ok = True
        model = get_env("SARVAM_MODEL") or "sarvam-m"
    elif groq_key and "<" not in groq_key:
        provider = "groq"
        key_ok = True
        model = get_env("GROQ_MODEL") or "gemma-4-27b-it"
    else:
        provider = "gemini"
        gemini_key = get_env("GEMINI_API_KEY")
        key_ok = bool(gemini_key) and "<" not in gemini_key
        model = get_env("GEMINI_MODEL") or "gemini-2.5-flash"

    return models.HealthResponse(
        status="ok" if db_ok else "degraded",
        db_ok=db_ok,
        llm_provider=provider,
        llm_key_configured=key_ok,
        llm_model=model,
        detail=detail,
    )


@app.get("/events", response_model=models.EventsResponse)
def events() -> models.EventsResponse:
    return models.EventsResponse(**tools.list_events())


@app.get("/scenes")
def scenes(
    event_key: str | None = Query(None),
    window_type: str | None = Query(None),
    sensor: str | None = Query(None),
    max_cloud: float | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
) -> dict:
    try:
        return tools.REGISTRY["list_scenes"](
            event_key=event_key, window_type=window_type, sensor=sensor,
            max_cloud=max_cloud, limit=limit,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


@app.get("/scenes/{scene_id}/assets", response_model=models.SceneAssetsResponse)
def scene_assets(scene_id: str) -> models.SceneAssetsResponse:
    """Return a scene's COG asset URLs (under /ard) + WGS84 footprint bbox.

    The web client uses this to place band/index layers on the map and to set a
    default view. Asset paths stored in the DB are relative (``data/ard/...``); we
    strip the leading ``data/`` so they resolve under the ``/ard`` static mount.
    """
    import psycopg2

    sql = """
        SELECT sensor, satellite, gsd_m, acq_datetime, window_type, event_key,
               assets, ST_AsGeoJSON(ST_Envelope(geometry))
        FROM scenes WHERE id = %s
    """
    try:
        with psycopg2.connect(get_db_url()) as conn, conn.cursor() as cur:
            cur.execute(sql, (scene_id,))
            row = cur.fetchone()
    except Exception as exc:
        raise HTTPException(status_code=500,
                            detail=f"{type(exc).__name__}: {exc}") from exc

    if row is None:
        raise HTTPException(status_code=404, detail=f"No such scene: {scene_id}")

    sensor, satellite, gsd_m, acq_dt, window_type, event_key, assets, envelope = row

    # Relative path (data/ard/...) -> /ard/... URL under the static mount.
    asset_urls: dict[str, str] = {}
    if isinstance(assets, dict):
        for key, rel in assets.items():
            if not isinstance(rel, str):
                continue
            rel_norm = rel.replace("\\", "/").lstrip("/")
            if rel_norm.startswith("data/"):
                rel_norm = rel_norm[len("data/"):]
            asset_urls[key] = "/" + rel_norm if not rel_norm.startswith("/") else rel_norm

    # ST_Envelope returns a polygon ring; flatten to [minlon, minlat, maxlon, maxlat].
    bbox = None
    if envelope:
        import json as _json
        coords = _json.loads(envelope).get("coordinates")
        if coords:
            ring = coords[0]
            lons = [pt[0] for pt in ring]
            lats = [pt[1] for pt in ring]
            bbox = [min(lons), min(lats), max(lons), max(lats)]

    return models.SceneAssetsResponse(
        scene_id=scene_id,
        sensor=sensor,
        satellite=satellite,
        gsd_m=float(gsd_m) if gsd_m is not None else None,
        acq_datetime=acq_dt.isoformat() if acq_dt is not None else None,
        window_type=window_type,
        event_key=event_key,
        bbox_wgs84=bbox,
        assets=asset_urls,
    )


@app.post("/query", response_model=models.QueryResponse)
def query(req: models.QueryRequest,
          make_agent=Depends(get_agent_factory)) -> models.QueryResponse:
    # VyomAgent auto-detects the active backend (Sarvam > Groq > Gemini) via the
    # LangGraph path; model override is forwarded for per-request model selection.
    # The factory is overridable in tests (scripted backend).
    try:
        agent = make_agent(req.max_steps, req.model)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # If the user drew an AOI in QGIS, instruct the agent to use that exact polygon
    # for every spatial tool instead of resolving the event's default bounding box.
    run_query = req.query
    if req.aoi_geojson and req.aoi_geojson.strip():
        run_query = (
            f"{req.query}\n\n[User-drawn area of interest — use this EXACT GeoJSON "
            f"string as the aoi_geojson argument for check_coverage and all spatial "
            f"tools; do NOT call get_event_aoi: {req.aoi_geojson.strip()}]"
        )

    try:
        result = agent.run(run_query)
    except Exception as exc:
        msg = str(exc)
        if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "rate_limit" in msg.lower():
            raise HTTPException(
                status_code=429,
                detail=f"LLM API rate limit hit. Wait a moment and retry. [{msg[:200]}]",
            ) from exc
        if "503" in msg or "UNAVAILABLE" in msg:
            raise HTTPException(
                status_code=503,
                detail=f"LLM model temporarily unavailable. Wait 30s and retry. [{msg[:200]}]",
            ) from exc
        raise

    # Render deterministic server-side charts from the tool calls (independent of the LLM).
    try:
        from .charts import render_charts
        result["charts"] = render_charts(result.get("tool_calls"), EXPORTS_DIR)
    except Exception:
        result["charts"] = []

    if not req.include_history:
        result = {k: v for k, v in result.items() if k != "history"}
    return models.QueryResponse(**result)


@app.post("/query/stream")
async def query_stream(req: models.QueryRequest) -> StreamingResponse:
    """POST /query/stream — SSE streaming version of /query.

    Yields Server-Sent Events (text/event-stream):
      data: {"type":"tool",  "step":N, "name":"...", "result":{...}}
      data: {"type":"answer","text":"..."}
      data: {"type":"done",  "answer":"...", "tool_calls":[...], "charts":[...], ...}
      data: {"type":"error", "message":"..."}
    """
    run_query = req.query
    if req.aoi_geojson and req.aoi_geojson.strip():
        run_query = (
            f"{req.query}\n\n[User-drawn area of interest — use this EXACT GeoJSON "
            f"string as the aoi_geojson argument for check_coverage and all spatial "
            f"tools; do NOT call get_event_aoi: {req.aoi_geojson.strip()}]"
        )

    try:
        agent = VyomAgent(max_steps=req.max_steps, model=req.model)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    async def event_gen():
        from langchain_core.callbacks import BaseCallbackHandler

        loop = asyncio.get_event_loop()
        q: asyncio.Queue = asyncio.Queue()

        class _TokenCallback(BaseCallbackHandler):
            """Intercepts LLM token stream and sends each token to the SSE queue."""
            def on_llm_new_token(self, token: str, **_kw) -> None:
                if token:
                    asyncio.run_coroutine_threadsafe(
                        q.put({"type": "token", "text": token}), loop)

        def stream_thread():
            tool_calls_log = []
            coverage_checked = False
            answer = None
            try:
                from langgraph.errors import GraphRecursionError
                initial = {
                    "messages": [HumanMessage(content=run_query)],
                    "coverage_checked": False,
                    "tool_calls_log": [],
                    "step_count": 0,
                }
                try:
                    for chunk in agent._graph.stream(
                        initial,
                        config={
                            "recursion_limit": agent._recursion_limit,
                            "callbacks": [_TokenCallback()],
                        },
                    ):
                        if "tools" in chunk:
                            state = chunk["tools"]
                            coverage_checked = state.get(
                                "coverage_checked", coverage_checked)
                            new_entries = state.get("tool_calls_log", [])
                            new_count = len(new_entries) - len(tool_calls_log)
                            for entry in new_entries[-max(new_count, 0):]:
                                tool_calls_log.append(entry)
                                asyncio.run_coroutine_threadsafe(
                                    q.put({
                                        "type": "tool",
                                        "step": len(tool_calls_log),
                                        "name": entry["name"],
                                        "heavy": entry["heavy"],
                                        "result": entry["result"],
                                    }),
                                    loop,
                                )
                        else:
                            # Answer is emitted by agent (pre-synthesis), respond
                            # (conversational/refusal) or synthesize (final cleaned
                            # text). Later nodes overwrite earlier → synthesize wins.
                            for node_key in ("agent", "respond", "synthesize"):
                                if node_key not in chunk:
                                    continue
                                for msg in chunk[node_key].get("messages", []):
                                    if (isinstance(msg, AIMessage)
                                            and not getattr(msg, "tool_calls", None)
                                            and msg.content):
                                        answer = msg.content
                except GraphRecursionError:
                    pass
            except Exception as exc:
                asyncio.run_coroutine_threadsafe(
                    q.put({"type": "error", "message": str(exc)}), loop)

            charts = []
            try:
                from .charts import render_charts
                tc_log = [{"step": i + 1, **e}
                          for i, e in enumerate(tool_calls_log)]
                charts = render_charts(tc_log, EXPORTS_DIR)
            except Exception:
                pass
            asyncio.run_coroutine_threadsafe(
                q.put({
                    "type": "done",
                    "answer": answer,
                    "tool_calls": [{"step": i + 1, **e}
                                   for i, e in enumerate(tool_calls_log)],
                    "coverage_checked": coverage_checked,
                    "charts": charts,
                    "steps": len(tool_calls_log),
                    "stopped": "answer" if answer else "max_steps",
                }),
                loop,
            )

        threading.Thread(target=stream_thread, daemon=True).start()

        while True:
            event = await q.get()
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("type") in ("done", "error"):
                break

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/exports/{filename}")
def export(filename: str) -> FileResponse:
    # Guard against path traversal — only a bare filename inside data/exports is allowed.
    if Path(filename).name != filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    path = EXPORTS_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"No such export: {filename}")
    media = {".png": "image/png", ".tif": "image/tiff",
             ".tiff": "image/tiff"}.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media, filename=filename)
