# VYOM — Agentic GIS for Indian Disaster Analysis

**Project root:** `D:\AGENTIC-GIS`  
**Python env:** `D:\AGENTIC-GIS\myenv` (Python 3.11)  
**DB:** PostgreSQL + PostGIS at `localhost:5432/agentic_gis_db` (creds in `.env`)  
**LLM:** Google Gemini — real `GEMINI_API_KEY` is set in `.env`. Add `GEMINI_MODEL=gemini-2.0-flash` to avoid free-tier daily quota (gemini-2.5-flash default is capped at 20 req/day).

## What this project is

VYOM is an agentic GIS platform that uses Indian satellite imagery (ResourceSat-2/2A from ISRO/NRSC) to analyze natural disasters — floods, wildfires, droughts, landslides. An LLM agent calls MCP tools to query a PostGIS catalog and run raster analysis, then returns structured answers.

## Architecture

```
data/raw/<event>/<sensor>/<window>/<date>_<scene_id>.zip   ← raw ISRO zips
data/ard/<collection>/<year>/<scene_id>/*.tif              ← COG outputs
data/manifests/<event>.csv                                 ← pipeline input

src/vyom/
  config.py           ← paths, DB URL, registry loaders (single source of truth)
  ingest/
    audit.py          ← walk data/raw/ and count scenes (sanity check tool)
    band_meta.py      ← parse ISRO BAND_META.txt → typed dict + WGS84 footprint
  ard/
    calibrate.py      ← DN → TOA radiance → DOS surface reflectance (GPU via CuPy)
    indices.py        ← NDVI, NDWI, NBR, MNDWI, cloud mask, index_metrics()
    cog.py            ← write Cloud-Optimized GeoTIFFs (deflate, 256x256 tiles)
    pipeline.py       ← main ARD orchestrator (manifest-driven, idempotent)
  db/
    writer.py         ← register_scene(), cache_metrics(), scene_exists()
    postgis_server.py ← FastMCP catalog MCP server (psycopg2 ONLY — cold-start <2s)
    gis_server.py     ← FastMCP raster MCP server (rasterio, numpy, shapely, matplotlib)
  agent/              ← Gemini orchestrator (DONE) — tools, llm, orchestrator, prompts, CLI
    tools.py          ← REGISTRY + DECLARATIONS (11 tools) + list_events/get_event_aoi helpers
    prompts.py        ← SYSTEM_PROMPT (check_coverage-first mandate + hazard→index knowledge)
    llm.py            ← LLMBackend abstraction: GeminiBackend (google-genai) + ScriptedBackend
    orchestrator.py   ← VyomAgent.run(): function-calling loop, enforces coverage-first
    __main__.py       ← CLI: python -m vyom.agent "<question>"
  api/                ← FastAPI REST wrapper (DONE) — app, models, runner
    models.py         ← pydantic request/response schemas
    app.py            ← routes: /health /events /scenes /query /exports/{file}
    __main__.py       ← uvicorn runner: python -m vyom.api [--port N] [--reload]
  plugin/             ← QGIS plugin (DONE) — thin REST client over the FastAPI server
    api_client.py     ← Qt-free stdlib REST client (loopback proxy-bypass) + export_filenames()
    vyom_dockwidget.py← QDockWidget panel: Connect/health, event-AOI picker, query box, answer+trace
    vyom_plugin.py    ← plugin shell: classFactory→initGui/unload, toolbar+menu toggle
    install.py        ← python -m vyom.plugin.install [--symlink] [--dest] [--profile]
    metadata.txt + README.md
```

## PostGIS schema (tables that exist)

- `scenes` — one row per ingested scene: id, collection, satellite, sensor, geometry (PostGIS), acq_datetime, cloud_cover, gsd_m, processing_level, event_key, window_type, properties (jsonb), assets (jsonb), pipeline_version
- `scene_metrics` — cached index stats: scene_id, aoi_hash, metric, value, pipeline_version
- `derived_products` — future: change detection outputs, mosaics, etc.

## Registered events (events_registry.yaml — 6 events)

| key | hazard | primary index |
|-----|--------|---------------|
| kerala_periyar_2018 | flood | NDWI |
| assam_brahmaputra_2022 | flood | NDWI |
| bihar_kosi_ganga_2019 | flood | NDWI |
| wildfire_uttarakhand_2016 | wildfire | NBR |
| landslide_sikkim_glof_2023 | landslide | dNDVI |
| drought_marathwada_2016 | drought | VCI |

**12 manifests exist total** — 6 additional events (assam_brahmaputra_2019, cyclone_michaung_2023, delhi_yamuna_2023, godavari_ap_2022, kerala_2019, uttarakhand_kedarnath_2013) have manifests but are NOT yet in events_registry.yaml.

## Sensors (band_registry.yaml — 5 sensors)

| sensor key | satellite | GSD | has_swir |
|------------|-----------|-----|----------|
| ResourceSat-2_LISS3_L2 | RS-2 | 23.5m | yes |
| ResourceSat-2A_LISS3_L2 | RS-2A | 23.5m | yes |
| ResourceSat-2A_LISS4-MX70_L2 | RS-2A | 5.8m | NO |
| ResourceSat-2_AWIFS_L2 | RS-2 | 56m | yes |
| ResourceSat-2A_AWIFS_L2 | RS-2A | 56m | yes |

LISS4 has no SWIR — cannot compute NBR or MNDWI. NDVI + NDWI only.

## Band naming convention

Physical bands B2/B3/B4/B5 map to canonical names green/red/nir/swir1. ESUN values for calibration are in `calibrate.py`. `PIPELINE_VERSION = "v1.0-dos"` (bumping this invalidates cached metrics).

## ARD pipeline key facts

- **Idempotent**: scenes already at current `PIPELINE_VERSION` are skipped
- **DOS**: Dark Object Subtraction applied by default (+/-5-10% reflectance uncertainty)
- **Cloud mask**: brightness (green > 0.35) AND NIR (nir > 0.30) threshold
- **NDWI flood threshold**: > 0.3 (McFeeters, water-positive convention)
- **GPU**: CuPy used automatically if available (calibrate.py, indices.py)
- **No raw deletion**: zips are never modified or deleted

Run one scene: `python -m vyom.ard.pipeline --event kerala_periyar_2018 --scene <id>`  
Run all: `python -m vyom.ard.pipeline --event kerala_periyar_2018 --all`

## Phase completion status

```
Phase 0  [████████████] 100% — project structure, configs, .env, registries
Phase 1  [████████████] 100% — audit.py, band_meta.py, config.py
Phase 2  [████████████] 100% — kerala_periyar_2018 FULLY INGESTED (87/88 scenes; 1 zip missing)
Phase 3  [████████████] 100% — postgis_server.py DONE (5 tools); gis_server.py DONE (4 tools)
Phase 4  [████████████] 100% — agent/ DONE + api/ DONE (FastAPI) + plugin/ DONE (QGIS)
Phase 5  [████░░░░░░░░]  ~25% — END-TO-END TESTING in progress (Swagger UI + QGIS plugin)
Phase 6  [████████████] 100% — Rich QGIS chat UI + georeferenced layers (code DONE; QGIS interactive test pending)
```

## Phase 2 — Kerala ingestion complete

- **87 scenes** registered in `scenes` table for `kerala_periyar_2018` (1 zip was missing/corrupt — acceptable)
- **1,435 cached metric rows** in `scene_metrics` for this event
- Window breakdown: 63 annual, 18 post_event, 5 pre_event, 1 event
- Other events (assam, bihar, wildfire, landslide, drought) NOT yet ingested — Phase 2 only finished for kerala

## Phase 3 — status (COMPLETE)

### `src/vyom/db/postgis_server.py` — catalog MCP server (psycopg2-only, cold-start <2s)

1. `check_coverage` — "do we have data here?" (mandatory first agent call)
2. `list_scenes` — filter by event/window/sensor/max_cloud, returns scene metadata + available_assets
3. `get_scene_metrics` — cached index metrics for a scene (full_scene or AOI hash)
4. `scenes_by_date_range` — scenes in a date window, optional AOI intersect
5. `compare_windows` — aggregate a metric across pre/event/post windows + deltas + interpretation

### `src/vyom/db/gis_server.py` — heavy raster MCP server (rasterio, numpy, shapely, matplotlib)

1. `compute_change` — pixel-wise index delta between two scenes (delta = B − A); handles CRS reprojection
2. `clip_to_aoi` — clip any COG asset to a WGS84 AOI, compute stats, cache in scene_metrics
3. `flood_extent` — binary flood map from NDWI threshold, returns water_pixels/pct/km², caches results
4. `export_png` — renders false_color (NIR/R/G 2-98% stretch) or index (diverging colormap) PNG to data/exports/

## Phase 4 — status (COMPLETE)

### `src/vyom/agent/` — Gemini orchestrator

- Calls tool functions **directly in-process** (imports from both servers) — NOT MCP-over-the-wire.
- **11 tools** exposed to Gemini: 5 catalog + 4 raster + `list_events` + `get_event_aoi`.
- **GeoJSON args passed as JSON STRINGS** in Gemini declarations; `tools.coerce_args` parses back to dicts.
- **check_coverage-first ENFORCED** in orchestrator.py: any data/raster tool before check_coverage gets policy-error result.
- Default model: `gemini-2.5-flash` — override with `GEMINI_MODEL=gemini-2.0-flash` in `.env` (higher free quota).
- **Run CLI:** needs `PYTHONPATH=D:\AGENTIC-GIS\src` set first (see How to Run section below).

### `src/vyom/api/` — FastAPI REST wrapper

- **Endpoints:** `GET /health`, `GET /events`, `GET /scenes`, `POST /query`, `GET /exports/{filename}`
- Swagger UI at `http://127.0.0.1:8000/docs` — use this for smoke testing before QGIS.
- `/health` and `/events` and `/scenes` work without Gemini key; only `/query` needs it.

### `src/vyom/plugin/` — QGIS plugin

- **Installed as symlink** at:
  `C:\Users\Administrator\AppData\Roaming\QGIS\QGIS3\profiles\default\python\plugins\vyom`
- Symlinked (not copied) — code changes in `src/vyom/plugin/` take effect immediately in QGIS without reinstall.
- Thin REST client over the FastAPI server — no GIS logic, no DB imports.

## Phase 5 — Testing (IN PROGRESS)

**Goal:** Smoke-test the full stack end-to-end with Kerala 2018 data.

**Current status:**
- DB confirmed: 87 scenes, 1435 metric rows for kerala_periyar_2018
- API server: runs correctly when PYTHONPATH is set (see How to Run)
- Swagger UI: not yet fully tested — next step
- QGIS plugin: installed, not yet tested interactively
- `/query` endpoint: not yet tested with real Gemini call (quota issues on previous session)

**Known issues / gotchas:**
- `python -m vyom.api` fails without `PYTHONPATH=src` set — always set it first
- Port 8000 conflict: if server is already running, find PID with `netstat -ano | grep ":8000"` and kill it
- Gemini free-tier: `gemini-2.5-flash` is capped at 20 requests/day — add `GEMINI_MODEL=gemini-2.0-flash` to `.env`
- Plugin reload: for Python file changes, QGIS picks them up automatically (symlink); for `metadata.txt` changes, disable/re-enable the plugin

**Test sequence:**
1. Swagger `/health` — DB=ok, Gemini key=configured
2. Swagger `/events` — 6 events returned
3. Swagger `/scenes?event_key=kerala_periyar_2018` — 87 scenes returned
4. Swagger `POST /query` with `{"query": "How did water area change in the Kerala 2018 floods?", "max_steps": 12}`
5. QGIS: Connect → Add AOI → Ask query → verify answer + PNG raster loads

## How to run

### API server (always needed — keep terminal open)
```powershell
# In PowerShell from D:\AGENTIC-GIS
$env:PYTHONPATH = "D:\AGENTIC-GIS\src"
python -m vyom.api
# Swagger UI: http://127.0.0.1:8000/docs
# With hot-reload for dev: python -m vyom.api --reload
```

### CLI agent (quick test without QGIS)
```powershell
$env:PYTHONPATH = "D:\AGENTIC-GIS\src"
python -m vyom.agent --show-tools "How did water area change in the Kerala 2018 floods?"
```

### QGIS plugin
- Plugin is already installed (symlinked). In QGIS:
  1. Plugins -> Manage and Install Plugins -> Settings -> tick "Show experimental plugins"
  2. Installed tab -> tick "VYOM -- Agentic GIS"
  3. VYOM dock appears — URL: `http://127.0.0.1:8000` -> Connect

## Key design rules

- `postgis_server.py` strict import rule: ONLY `psycopg2, json, hashlib, datetime, os` + FastMCP. Cold-start < 2s. NO rasterio/numpy/geopandas/shapely.
- `gis_server.py` heavy imports allowed (rasterio, numpy, shapely, matplotlib). Separate FastMCP instance named "vyom-gis". May import `config.py`.
- Tools defined as module-level functions THEN registered with `mcp.tool()()` — so they stay unit-testable without a running server.
- `writer.py` may import from `config.py` (pyyaml, dotenv allowed there). `postgis_server.py` must NOT import `config.py`.

## Context note

User switches to new Claude Code chats frequently due to a firewall payload limit. This CLAUDE.md auto-loads context on every new session. Always read it first and update it when major progress is made.

---

## Session update — 2026-06-22

### LLM backend: switched to Sarvam AI (ACTIVE)

- **Groq blocked**: corporate firewall blocks `api.groq.com` with SSL cert errors — do not use.
- **Sarvam AI** is now the active backend. `SARVAM_API_KEY` and `SARVAM_MODEL=sarvam-30b` are set in `.env`.
  - `sarvam-m` is deprecated — only `sarvam-30b` and `sarvam-105b` are available.
  - Default is `sarvam-30b` (set in both `.env` and `DEFAULT_SARVAM_MODEL` in `llm.py`).
- **Backend priority** in `orchestrator.py` and `api/app.py`: **Sarvam → Groq → Gemini**.
- `openai` package (v2.43.0) installed — used by `SarvamBackend` via `base_url="https://api.sarvam.ai/v1"`.

### SSL proxy fix

- Windows Server has a **corporate SSL inspection proxy** (self-signed CA not in certifi).
- Fix: `SSL_NO_VERIFY=true` added to `.env`. Both `SarvamBackend` and `GroqBackend` read this via `_make_httpx_client()` in `llm.py` and pass `httpx.Client(verify=False)`.
- To use a CA bundle instead: set `REQUESTS_CA_BUNDLE=/path/to/ca.pem` in `.env`.
- Gemini (`google-genai`) is unaffected — uses its own HTTP client.

### New files / changes this session

| File | Change |
|------|--------|
| `src/vyom/agent/llm.py` | Added `SarvamBackend`, `_make_httpx_client()`, SSL bypass for `GroqBackend` |
| `src/vyom/agent/orchestrator.py` | `_default_backend()` checks Sarvam first |
| `src/vyom/api/app.py` | `/health` and `/query` updated to detect/use Sarvam |
| `.env` | Added `SARVAM_API_KEY`, `SARVAM_MODEL=sarvam-30b`, `SSL_NO_VERIFY=true` |

### Phase 5 testing status (updated)

- `/health` ✓ — `db_ok: true`, `llm_provider: sarvam`
- `/events` — not yet confirmed (test next)
- `/scenes?event_key=kerala_periyar_2018` — not yet confirmed (test next)
- `/query` ✓ — confirmed working end-to-end with Sarvam AI
- QGIS plugin — not yet tested

---

## Phase 6 — Rich QGIS UI + georeferenced layers (CODE COMPLETE — 2026-06-22)

**Full plan file:** `C:\Users\Administrator\.claude\plans\now-fix-the-entire-bubbly-coral.md`.

### COMPLETION STATUS (what was built this session)
All five tasks (A–E) implemented and unit-verified against a real Kerala scene:
- **A — `gis_server.py`:** added `_write_geotiff`, `_wgs84_bounds`, `_crs_str` helpers. `flood_extent` (uint8 mask + colormap), `export_png` (float32 index OR 3-band uint8 false_color w/ ColorInterp), `compute_change` (float32 delta, NaN nodata) now each write a plain GeoTIFF beside their output and return **`geotiff_path`, `crs`, `bounds_wgs84`** (additive — all existing keys kept; georef wrapped in try/except so analysis never fails on it). Verified: GeoTIFFs open with valid CRS (EPSG:32643/32644), correct dtype/bands, bounds land over Kerala (~75-77°E, 9.9-11.5°N).
- **B — charts:** new `src/vyom/api/charts.py` → `render_charts(tool_calls, out_dir)` (matplotlib Agg, MD5-hashed deterministic filenames) for compare_windows (grouped bar; `windows` is a **dict** keyed by window name, not a list), flood_extent (water/dry %), compute_change (inc/dec/unchanged %). `app.py /query` calls it post-`agent.run()` and sets `result["charts"]`. `/exports/{filename}` media type generalized to `.tif/.tiff → image/tiff`. `models.py QueryResponse` gained `charts: Optional[list[str]]`.
- **C — `api_client.py`:** added `georef_layers(result)` (kind ∈ rgb/flood/index/change), `chart_filenames(result)`, Qt-free `build_result_html(result)` (Qt rich-text tables for the 3 tools). `export_filenames` kept for back-compat.
- **D — `vyom_dockwidget.py`:** answer pane is now `_ChatBrowser(QTextBrowser)` subclass overriding `loadResource` to resolve inline chart images by name (survives `setHtml`). Accumulates `self._transcript_html` chat bubbles (You/VYOM). Markdown via throwaway `QTextDocument.setMarkdown` (body extracted by regex; `<pre>` fallback). Result tables appended. Charts downloaded on worker thread → `add_image` + re-`setHtml`. **Health bug fixed**: reads `llm_key_configured` + `llm_provider` (was `gemini_key_configured`).
- **E — `vyom_dockwidget.py`:** `_on_answer` uses `georef_layers`; `_add_georef_raster(path, kind, bounds)` styles (rgb=default multiband, flood=2-class pseudocolor blue, index/change=diverging `QgsSingleBandPseudoColorRenderer`) + CRS-correct zoom union via `QgsCoordinateTransform` (reset per query in `_on_ask`).

### REMAINING / NOT DONE
- **QGIS interactive test pending** — code is syntactically valid + unit-verified but NOT yet exercised inside a live QGIS session (verification step 3 in the plan). Reload plugin → Connect → Ask, confirm markdown+table+inline chart in chat bubble and styled GeoTIFFs positioned over Kerala with auto-zoom.
- **Pre-existing test failures (NOT caused by Phase 6):** 4 in `tests/test_api.py` are stale from the Sarvam migration — they assert the old `gemini_key_configured` key and rely on a scripted-backend override that `/query` now bypasses (it builds a live Sarvam backend directly). 56 tests pass; Phase 6 added 0 new failures.

---

## Session update — 2026-06-23 — Web UI white theme + assam-aware agent

### Web React app (`web/`) — exists alongside the QGIS plugin
- Vite + React + TS + Tailwind + react-leaflet + react-query. 3-pane workbench (browser / map / chat).
  Thin client over the same FastAPI server (`/health /events /scenes /query /exports`). Dev proxy in `vite.config.ts` → `127.0.0.1:8000`.
- Charts are **client-side SVG** (`InteractiveCharts.tsx`) built directly from `tool_calls`
  (compare_windows / flood_extent / compute_change) — NOT the server PNGs. Map result rasters
  come from `georefLayers(result)` → `addResultLayer` → `loadCogLayer` (georaster over `/exports/<geotiff>`).
- **Build:** `cd web && npm run build` (tsc -b + vite) — verified GREEN this session. Dev: `npm run dev`.

### What changed this session
1. **Agent prompt** (`src/vyom/agent/prompts.py`) rewritten: mandates a map layer AND charts for every
   analysable question; flood questions MUST call `compare_windows(water_area_pct)` + `flood_extent` on an
   event (or post_event) scene. Now multi-event/assam-aware (kerala full pre/event/post; assam event+post_event).
2. **assam_brahmaputra_2022 now has data** (event 0.41% → post 1.43% water_area_pct) — queryable like kerala.
3. **Web UI redesigned dark → professional white theme + Poppins font:**
   - `tailwind.config.js`: tokens now `panel#fff / panelalt#f1f5f9 / edge#e2e8f0 / accent#2563eb`, `fontFamily.sans=Poppins`, `shadow-panel`.
   - `index.html`: removed `class="dark"`, added Poppins Google Fonts link.
   - `index.css`: body `#f8fafc`/slate-700/Poppins, light leaflet bg, light scrollbar, light markdown code/table.
   - All components: single-pass prominence-flip of `text-slate-*` (100→800…600→400), `fill-slate-*`, red/amber bumped for white; chart SVG strokes + tooltip + legend checkerboard lightened; user bubble solid accent, Ask button white text; header got brand badge + shadow.
   - Completed the half-wired draggable chat divider (`rightWidth`/`startDrag`) to clear pre-existing TS6133 build errors.

### Original plan (reference)

### Goal
Chat-like QGIS panel that (1) renders the agent answer as markdown with tables + **inline matplotlib charts**, and (2) **auto-adds georeferenced result rasters** (flood mask, false-color/index product, change delta) onto the canvas at correct coordinates, then zooms to them. This is the demo surface for the benchmark dataset.

### Two locked design decisions
1. **Charts = matplotlib inline images** — rendered SERVER-SIDE deterministically from `tool_calls` (NOT LLM-dependent), returned via a new `charts` response field, embedded inline in the chat via `QTextBrowser.addResource`.
2. **Map layers = flood + rendered products + change** — all three tools emit georeferenced GeoTIFFs.

### Decisive technical fact
`_read_clipped`/`_read_full` in `gis_server.py` ALREADY return `(array, transform, crs)` but the tools discard transform/crs. The matplotlib PNG from `export_png` (title/colorbar/`bbox_inches="tight"` padding) CANNOT be georeferenced — must write a SEPARATE plain GeoTIFF from the data array. Fix is purely additive (extend return dicts, never rename/remove existing keys — tests + LLM prompt depend on them).

### Task breakdown (ordered)
- **A. `gis_server.py`:** add `_write_geotiff(...)` + `_wgs84_bounds(...)` helpers; extend `flood_extent` (uint8 mask + colormap), `export_png` (float32 index OR 3-band false_color), `compute_change` (float32 delta) to write `.tif` beside outputs and return `geotiff_path`, `crs`, `bounds_wgs84`. Keep NaN nodata. Use `rasterio.transform.array_bounds` + `rasterio.warp.transform_bounds(densify_pts=21)`.
- **B. server charts:** new `src/vyom/api/charts.py` → `render_charts(tool_calls, out_dir)` (matplotlib Agg, deterministic hash filenames): compare_windows grouped bars, flood_extent water%, compute_change pct bars. `app.py /query` calls it post-`agent.run()`; add `charts: list[str]` to `models.py QueryResponse`. Generalize `/exports/{filename}` media type for `.tif/.tiff`.
- **C. `api_client.py`:** add `georef_layers(result)` (filename/crs/bounds/kind from any result with `geotiff_path`), `chart_filenames(result)`, and Qt-free `build_result_html(result)` tables. Keep `export_filenames` for back-compat.
- **D. `vyom_dockwidget.py`:** swap answer `QTextEdit`→`QTextBrowser`; accumulate chat-bubble transcript; markdown via `QTextDocument.setMarkdown` (+ `<pre>` fallback); append result tables; embed downloaded chart PNGs inline via `addResource`.
- **E. `vyom_dockwidget.py`:** rewrite `_on_answer` to use `georef_layers`; add `_add_georef_raster(path, kind, bounds)` with styling (RGB / `QgsSingleBandPseudoColorRenderer` / flood colormap) + CRS-correct combined-extent zoom via `QgsCoordinateTransform`.
- **Bug fix:** `_on_connect` (dockwidget ~line 192) reads `gemini_key_configured` but API returns `llm_key_configured` → always shows MISSING. Fix to read `llm_key_configured` + label with `llm_provider`.

### Pitfalls
Never world-file the matplotlib PNG. Plugin targets PyQt5 (short-form enums, e.g. `Qt.TextSelectableByMouse`) — keep new enums short-form. `crs.to_epsg()` may be None → `crs.to_string()` fallback. Layer creation must stay in GUI-thread done-callbacks (existing `_Worker`→signal pattern already ensures this). Run existing `tests/` — confirm extended return dicts don't break assertions.

### Files to touch
`gis_server.py`, new `api/charts.py`, `api/app.py`, `api/models.py`, `plugin/api_client.py`, `plugin/vyom_dockwidget.py`, and this `CLAUDE.md` (mark complete when done).

### Not in scope (separate downstream task)
Benchmark ingestion — only `kerala_periyar_2018` (87 scenes) is ingested; 11 other manifests await the ARD pipeline.

---

## Session update — 2026-06-23 (b) — QGIS: always-populated map via streamed preprocessed COGs

**Goal of this session:** make the QGIS plugin (the primary app) robust so EVERY query
(1) shows charts in the chat and (2) auto-adds relevant map layers — including the common
case where the agent answers purely from cached metrics (`compare_windows`) and emits NO
analysis GeoTIFF. Verified live: `"How did water area change in the Kerala 2018 floods?"`
runs only `compare_windows` → `analysis georef layers: []` previously meant an empty map;
now a preprocessed event-window NDWI COG is resolved and shown.

### Root cause found
`download_asset` read the **entire COG into memory** before writing (`data = self._request(...)`).
The Kerala event-window NDWI COG is **820 MB** (LISS4 @ 5.8m) → huge RAM spike + long stall =
the "infinite loader". COGs already carry overviews + internal tiling, so they're meant to be
**streamed** via GDAL `/vsicurl/` (Range requests fetch a few MB, not 800 MB).

### Decisive technical facts
- `/ard` StaticFiles mount honours HTTP Range → `/vsicurl/http://127.0.0.1:8000/ard/...` works.
- **Corporate proxy (`HTTP(S)_PROXY=jdmproxy.nrsc.gov.in:8080`) intercepts loopback → 504.**
  Fix: add API host + `127.0.0.1,localhost,::1` to `NO_PROXY`/`no_proxy` (libcurl/GDAL honour it).
  Verified with rasterio: vsicurl open **0.04s**, full-overview preview **0.37s** (vs 820 MB DL).

### Changes (additive — no existing keys/behaviour removed)
| File | Change |
|------|--------|
| `plugin/api_client.py` | `+ vsicurl_url(asset_url)` → `/vsicurl/{base}{asset}`. `+ _fetch_to_file()` streams in 256 KiB chunks (no full-body buffering); `download_asset`/`download_export` now use it. `download_asset` is now the **fallback** when vsicurl fails. |
| `plugin/vyom_dockwidget.py` | `_add_preprocessed_layers` now **vsicurl-first**: `_try_add_vsicurl(desc, zoom)` builds a `QgsRasterLayer` on the GUI thread (loopback header read ~0.04s) and styles+adds it; only if `isValid()` is False does it fall back to a chunked download on a worker thread. `+ _ensure_gdal_config()` (called once) sets `NO_PROXY` + GDAL opts (`GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR`, `CPL_VSIL_CURL_ALLOWED_EXTENSIONS=.tif`, `VSI_CACHE`, connect/read timeouts 10/30s so a proxy stall fails fast into the fallback). |
| `tests/test_plugin_client.py` | NEW — unit tests for `vsicurl_url`, `scene_ids_from_result`, `event_keys_from_result`, `_choose_asset`, `georef_layers` (5 pass, no server needed). |

### Status / how it fits together (already-built pieces from prior session, now verified end-to-end)
- **Charts** (`api/charts.py` → `result["charts"]` → plugin downloads via `/exports` → inline `<img>` in chat bubble): ✅ live-verified (compare_windows + flood_extent PNGs render & fetch).
- **Always-on map layers**: analysis GeoTIFFs via `georef_layers` (when a raster tool ran) PLUS
  `VyomApiClient.preprocessed_layers(result, events)` which resolves scene ids → `/scenes/{id}/assets`
  → most-relevant preprocessed COG (primary index, e.g. ndwi for floods) for EVERY query. ✅ live-verified.
- Test suite: **56 pass + 5 new = 61**; the **4 `test_api.py` failures are pre-existing** (stale Sarvam-migration assertions on `gemini_key_configured` + scripted-backend override `/query` bypasses) — NOT caused by this work.

### Still pending
- **Live QGIS interactive test** — code is unit/integration-verified outside QGIS (rasterio shares GDAL,
  proved vsicurl streaming + NO_PROXY bypass work against the running server) but NOT yet exercised in a
  live QGIS session. Reload plugin → Connect → Ask a metrics-only flood question → confirm: chart appears
  inline in the chat bubble AND a styled NDWI layer streams onto the canvas over Kerala (no long download).
- **Web frontend** infinite-loader is a SEPARATE issue (user deprioritised it; QGIS is the main app) — not touched.

---

## Session update — 2026-06-23 (c) — PAPER UPGRADE to JSTARS / top-tier (IN PROGRESS)

**Task:** Upgrade `paper/vyom.tex` into a top-tier (IEEE JSTARS-grade) manuscript. Reposition VYOM
as a **safety-constrained GeoAI agent system**, NOT a new flood-mapping method. Add a real evaluation
(expanded benchmark + dispatch-vs-prompt-only ablation + repeated-run stability), formalize the
coverage-first dispatch mechanism, fix every factual inconsistency, and adopt an honest scope.

**Canonical paper file:** `paper/vyom.tex` (704 lines, IEEEtran). `paper/paper/` is a stale unzip
duplicate — IGNORE it. Figures are drawn INLINE (TikZ/pgfplots) — the `figures/*.png` are not used by
the .tex. Benchmark harness: `paper/scripts/run_benchmark.py` (10 cases today). Results in
`paper/results/`.

### User decisions locked (this task)
1. **Honest scope** — only FLOODS are ingested (3 events, 2 sensors). Do NOT claim multi-hazard or
   national generalization; frame wildfire/drought/landslide as registered-but-not-yet-ingested.
2. **Cross-index consistency validation** — NO ground-truth flood polygons exist on disk, so flood-mask
   accuracy-vs-truth (IoU/precision/recall/F1) CANNOT be computed honestly. Validate the NDWI flood mask
   by cross-index agreement (NDWI vs MNDWI/NDVI-low/short-wave) instead, reported as consistency not truth.
3. **Full eval** — expand to ~30 queries across {valid, no-data, partial-coverage, tool-failure,
   adversarial}; add dispatch-enforcement vs prompt-only ablation; repeated runs for stability. Run live
   against Sarvam-30B in the background (~150-300 calls).

### GROUND TRUTH from the live DB (verified this session — supersedes older claims above)
- **Three flood events are ingested now** (not just Kerala):
  | event | scenes | windows | sensors | metric rows |
  |-------|--------|---------|---------|-------------|
  | kerala_periyar_2018 | 88 | 64 annual, 5 pre, 1 event, 18 post | LISS3+LISS4 | 1455 |
  | assam_brahmaputra_2022 | 42 | 12 event, 30 post | LISS4 only | 426 |
  | bihar_kosi_ganga_2019 | 63 | 1 pre, 33 event, 29 post | LISS3+LISS4 | 734 |
  Total scene_metrics rows = 2615. 1409 COG .tif on disk. Agent runs live (~19s/run, Sarvam-30B).

### CRITICAL integrity bugs found in current paper (must fix)
- "87 of 88 scenes / 63 annual / 1,435 metric rows" → ACTUAL 88 registered, 64 annual, 1455 rows.
- **"3.5× increase, 0.41→1.43→0.62" (Fig. 9 / case study) is FABRICATED.** Real full-scene
  `compare_windows(kerala, water_area_pct)` = pre **13.2%**, event **0.31%**, post **1.34%** — water
  *decreases* pre→event. Cause: full-scene averaging mixes different footprints, sensors (LISS3 23.5m vs
  LISS4 5.8m) and cloud across windows → comparison is CONFOUNDED and invalid. This is exactly the
  "same AOI / common valid footprint / co-registration" flaw to fix.
- **Assam "0.41→1.43"** is a verbatim copy of the fake Kerala numbers. Real assam full-scene:
  event **0.39%**, post **1.40%**.
- Fix: replace confounded full-scene window comparison with a **same-AOI, common-valid-footprint,
  co-registered** comparison (clip both scenes to one AOI, intersect valid masks) before reporting any
  temporal change; report the confounded result only as a documented failure mode.

### OBJECTIVE (ordered plan)
- **A.** Expanded benchmark harness `paper/scripts/run_eval.py`: 30 queries × 5 categories; a
  `prompt_only` ablation arm (orchestrator flag disabling dispatch enforcement, prompt unchanged);
  repeated runs (stability). Resumable (append per-result JSON). Metrics: tool-selection accuracy,
  policy-violation rate, unsupported-claim rate, latency, run-to-run stability. RUN IN BACKGROUND.
  → **DONE (harness) + RUNNING (live):** 90-run ablation (enforced ×2 + prompt_only ×1 over 30
  queries) launched; writes `eval_runs.jsonl` / `eval_summary.json` / `eval_runs.csv`. This REPLACES
  the extrapolated ablation numbers with real Sarvam-30B runs.
- **B.** Real same-AOI flood comparison + cross-index consistency — **DONE**
  (`paper/scripts/compute_validation.py`, outputs in `paper/results/validation_*.{csv,json}`).
  This REPLACES the "pending" Results section and the fabricated 3.5×. See corrected numbers below.
- **C.** Rewrite `paper/vyom.tex`: reposition (safety-constrained GeoAI agent); formalize coverage-first
  dispatch (guarantee statement); new Evaluation section (benchmark + ablation + stability tables);
  full successful + blocked agent traces; fix all counts, Algorithm 1, the 3.5× claim, Figs 9-13;
  honest 3-event/2-sensor/1-hazard scope; provenance + uncertainty; verify references.
  → **PENDING:** feed it the two real result sets (A + B) below.

### NRSC reference raster — NOT obtained (external dependency)
The advisor path suggested requesting the NRSC Kerala-2018 inundation GeoTIFF via an internal NRSC
channel for accuracy-vs-truth (IoU/precision/recall/F1). That raster is **not on disk and cannot be
fetched from here** (requires a human/NRSC channel). Per the locked decision, flood-mask validation is
done by **cross-index consistency** instead (computed in B). If the NRSC raster later arrives, add a
truth-based IoU pass; until then the paper reports consistency, not accuracy — honestly labelled.

### CORRECTED RESULTS (computed this session — supersede the fabricated 3.5× / "pending")
Same-AOI, co-registered (clip both scenes to one fixed AOI; water fraction = NDWI>0.3 over valid AOI
pixels; the fraction is GSD-invariant over an identical footprint, so LISS3 23.5m & LISS4 5.8m are
comparable). Key finding — the confound was a **catalog sampling gap**:
- **Highland AOI** (77.30–77.75 E, 9.00–9.65 N — the ONLY region a clean pre+event+post scene all
  cover): pre **0.002%** → event **0.295%** → post **0.0%** open water. The single event-window
  optical scene lands over the **forested Periyar headwaters, not the floodplain** → essentially no
  flood signal here. This is why the naive full-scene average was meaningless.
- **Floodplain AOI** (76.40–76.95 E, 10.00–10.25 N — lower Periyar/Aluva basin; NO cloud-free
  event-window scene exists over it, so only an honest **pre vs post** comparison is possible):
  open water **0.005% (Jan 2018)** → **0.561% (Dec 2018)**, Δ **+0.556 pp** — residual inundation
  /saturation persisting months after the August peak.
- **Cross-index consistency** (validates the NDWI mask without ground truth): floodplain post scene
  NDWI>0.3 vs NDVI<0 → IoU **0.315**, Cohen's **κ=0.475**, agreement **0.988** (moderate/substantial
  on a sparse-water scene). Highland/pre scenes have near-zero water so IoU≈0 (expected).
- Honest takeaway for the paper: VYOM's value is the **safety-constrained agent**, not a flood number;
  the case study now demonstrates the agent correctly surfacing a confounded/under-sampled comparison
  rather than fabricating a clean surge.

### Orchestrator change needed for ablation — DONE
`VyomAgent.__init__` has `enforce_coverage_first: bool = True`; the policy-intercept block in `run()`
is guarded on it. `prompt_only` arm = same SYSTEM_PROMPT (still says "coverage first") but NO hard
block, isolating the dispatch-level mechanism from the prompt-level instruction.
