# VYOM Web Frontend — Vite + React + Tailwind + Leaflet GIS Workbench

## Context

VYOM is an agentic GIS platform (FastAPI backend at `:8000`, PostGIS catalog, Sarvam/Gemini
LLM agent) that analyzes Indian disasters over ISRO ResourceSat COG imagery. Today the only
graphical client is the **QGIS plugin** (`src/vyom/plugin/`), which is heavy to install and
demo. We want a **browser-based GIS workbench** that anyone can open locally — keeping the 
QGIS plugin fully intact and untouched.

The web app reuses the existing FastAPI server as-is for its core (the QGIS plugin and the web
app both become thin REST clients). The frontend mirrors what the plugin does — natural-language
query → markdown answer + tables + inline charts + auto-overlaid georeferenced result rasters —
and adds a **full GIS workbench**: an event/scene browser to toggle preprocessed band & index
COGs, a layer panel with opacity + colormap controls, a swipe/compare slider, and a draw-AOI
tool that feeds the agent.

**Decisions locked with the user:** Leaflet + `georaster-layer-for-leaflet` (client-side COG
rendering, no tile server); full GIS workbench scope; local-dev only (Vite dev server + proxy
to `:8000`).

**Decisive technical facts (verified in code):**
- `/query` returns `tool_calls[].result` carrying `geotiff_path`, `crs`, `bounds_wgs84`, plus a
  top-level `charts: [filename]`. All files are fetched via `GET /exports/{filename}`
  ([app.py:156-166](src/vyom/api/app.py#L156-L166)). The plugin's `georef_layers(result)`
  ([plugin/api_client.py](src/vyom/plugin/api_client.py)) already encodes the
  result-dict → layer-descriptor logic we replicate in TS.
- Preprocessed scene COGs in `data/ard/<sensor>/<year>/<scene_id>/*.tif` are **true COGs**
  (256px tiles, overviews `[2,4,8,16]`, EPSG:32643, deflate) → readable by `geotiff.js` over
  HTTP range requests. They are NOT currently served by any endpoint (only `data/exports` is).
- `scenes.assets` (jsonb) maps asset key → **relative path**; `list_scenes` exposes only asset
  *keys* (`available_assets`), so we add one endpoint to expose paths + footprint bbox.
- A COG self-describes CRS+bounds in its header, so `georaster-layer-for-leaflet` auto-positions
  and reprojects to the Leaflet map — no manual bounds arithmetic client-side.
- Per-`kind` colormaps to replicate (from `plugin/vyom_dockwidget.py._style_raster`): `flood`
  (0→transparent, 1→blue `rgb(0,90,200)`), `index` (diverging red→yellow→blue, −1..+1),
  `change` (diverging maroon→white→navy, −1..+1), `rgb` (3-band false-color, default render).

---

## Part 1 — Minimal backend additions (within `src/vyom/api/`)

Three small, additive changes. No existing route, schema, or tool signature changes.

### 1A. CORS + static COG mounts — `src/vyom/api/app.py`
- Add `CORSMiddleware` (allow `http://localhost:5173` + `http://127.0.0.1:5173`, all methods/headers).
  Belt-and-suspenders; the Vite proxy already avoids CORS in dev.
- Mount preprocessed data for range-request reads (Starlette `StaticFiles` honors HTTP `Range`,
  which `geotiff.js` needs for COG overview reads):
  ```python
  from fastapi.staticfiles import StaticFiles
  app.mount("/ard", StaticFiles(directory=PROJECT_ROOT / "data" / "ard"), name="ard")
  ```
  (`/exports/{filename}` stays as the existing route for result rasters + charts.)

### 1B. Scene-assets endpoint — `src/vyom/api/app.py` + `models.py`
New `GET /scenes/{scene_id}/assets` returning, per scene, the COG URLs + footprint bbox so the
browser can place band/index layers and set a default view:
```json
{
  "scene_id": "RA308MAR2018...",
  "sensor": "LISS3", "satellite": "RS-2A", "gsd_m": 23.5,
  "acq_datetime": "2018-03-08T...", "window_type": "pre_event",
  "bbox_wgs84": [minlon, minlat, maxlon, maxlat],
  "assets": { "ndwi": "/ard/ResourceSat-2A_LISS3_L2/2018/<id>/ndwi.tif", "red": "...", ... }
}
```
- Query the `scenes` table directly (mirror `postgis_server._get_scene_row` pattern) for
  `assets` (jsonb) + `gsd_m` + metadata, and `ST_AsGeoJSON(ST_Envelope(geometry))` (or
  `Box2D`) for `bbox_wgs84`.
- Convert each relative asset path (`data/ard/...`) → URL under the `/ard` mount by stripping
  the `data/` prefix. Add `SceneAssetsResponse` to `models.py`.

### 1C. (Optional, nice-to-have) AOI passthrough — skip for v1
v1 frontend appends a drawn AOI to the query text as GeoJSON (the agent already parses AOI from
the prompt), so **no backend change is required for draw-AOI**. A cleaner optional follow-up:
add `aoi_geojson: dict | None` to `QueryRequest` and have the orchestrator seed it — deferred.

---

## Part 2 — Frontend app (new `web/` folder under `D:\AGENTIC-GIS\web\`)

Greenfield Vite + React + TypeScript project. QGIS plugin untouched.

### 2A. Stack & dependencies
- **Build:** Vite + React 18 + TypeScript.
- **Styling:** Tailwind CSS (+ `@tailwindcss/forms`).
- **Map:** `leaflet`, `react-leaflet`, `georaster`, `georaster-layer-for-leaflet`, `geotiff`
  (COG reader), `leaflet-side-by-side` (swipe/compare), `leaflet-draw` + `@types/leaflet-draw`
  (or `leaflet-geoman-free`) for AOI drawing.
- **Data/state:** `@tanstack/react-query` (API fetching/caching), `zustand` (layer + UI state).
- **Answer rendering:** `react-markdown` + `remark-gfm` (tables) for the agent answer.
- **Colors:** `chroma-js` for diverging colormap ramps (mirror plugin styling).
- **Icons:** `lucide-react`.

### 2B. Vite config — dev proxy (`web/vite.config.ts`)
Proxy backend paths to `http://127.0.0.1:8000` so the browser stays same-origin (no CORS, range
headers forwarded): proxy `/health`, `/events`, `/scenes`, `/query`, `/exports`, `/ard`.

### 2C. API client (`web/src/api/client.ts`) — TS port of `plugin/api_client.py`
- Typed wrappers: `getHealth()`, `getEvents()`, `getScenes(filters)`, `getSceneAssets(id)`,
  `postQuery({query, max_steps, model})`, `exportUrl(filename)` → `/exports/{filename}`.
- `georefLayers(result)` — **direct port** of the plugin's `georef_layers` + `_kind_for`:
  walk `tool_calls`, pick results with `geotiff_path`, classify `kind` (`flood_extent`→flood,
  `compute_change`→change, `export_png` false_color→rgb else index), return
  `{filename, kind, bounds_wgs84, crs}`.
- `chartFilenames(result)` → `result.charts`.
- TS types mirroring `models.py` (`QueryResponse`, `ToolCall`, `EventInfo`, `HealthResponse`,
  `SceneAssets`).

### 2D. Map rendering (`web/src/map/`)
- `MapView.tsx` — `react-leaflet` `MapContainer` with an OSM (or Esri imagery) basemap; default
  view from selected event `bbox` (`/events`) — `[[minlat,minlon],[maxlat,maxlon]]`.
- `cogLayer.ts` — helper that loads any COG URL via `parseGeorasterFromUrl`/`GeoTIFF.fromUrl`
  and builds a `GeoRasterLayer` with a `pixelValuesToColorFn` chosen by `kind`. **Use COG
  overviews** (read a reduced-resolution level) so the 143MB ARD COGs render fast.
- `colormaps.ts` — replicate plugin ramps exactly:
  - `flood`: `v===1 → rgba(0,90,200,255)`, else transparent.
  - `index`: chroma diverging `[165,0,38]→[255,255,191]→[49,54,149]` over `[-1,0,1]`.
  - `change`: chroma diverging `[178,24,43]→[247,247,247]→[33,102,172]` over `[-1,0,1]`.
  - `rgb`: multiband passthrough (3-band false-color renders natively).
- `aoiDraw.ts` — `leaflet-draw`/geoman control emitting a GeoJSON polygon to the store.
- `compareSwipe.ts` — `leaflet-side-by-side` wiring two chosen layers to a drag slider.

### 2E. UI components (`web/src/components/`)
- `AppShell.tsx` — 3-pane layout (left sidebar = browser/layers, center = map, right = chat),
  Tailwind grid; collapsible panels.
- `HealthBadge.tsx` — `/health`: DB ok dot + `llm_provider`/`llm_model` chip (reads
  `llm_key_configured` — the field the plugin bug-fix used).
- `EventSelector.tsx` — dropdown from `/events`; selecting an event fits the map to its bbox and
  scopes the scene browser.
- `SceneBrowser.tsx` — `/scenes?event_key=…` table (date, sensor, window, cloud%); expand a
  scene → `GET /scenes/{id}/assets` → checkboxes per band/index COG → toggle as map layers via
  `cogLayer.ts`.
- `LayerPanel.tsx` — active layers (zustand): visibility toggle, opacity slider, colormap/kind
  selector, reorder, remove; "Compare (swipe)" picker for two layers.
- `ChatPanel.tsx` — query textarea + max-steps; on submit `POST /query`; render:
  - answer via `react-markdown`+`remark-gfm`;
  - a collapsible **tool-call trace** (step/name/heavy/args) like the plugin's HTML tables;
  - inline **charts** as `<img src={exportUrl(name)} />`;
  - auto-add **result rasters** from `georefLayers(result)` to the map + zoom to their union.
  - If an AOI is drawn, append its GeoJSON to the outgoing query text.
- `Legend.tsx` — colormap legend per active raster kind.

### 2F. State (`web/src/store.ts`, zustand)
`{ selectedEvent, scenes, layers: [{id,url,kind,opacity,visible,leafletLayer}], aoiGeoJSON,
chatHistory, comparePair }` with actions add/remove/reorder/setOpacity/setVisible.

### 2G. Docs — `web/README.md`
How to run: start backend (`$env:PYTHONPATH="D:\AGENTIC-GIS\src"; python -m vyom.api`), then
`cd web; npm install; npm run dev` → open `http://localhost:5173`.

---

## Files to create / modify

**Backend (modify, additive only):**
- `src/vyom/api/app.py` — CORS middleware, `/ard` static mount, `GET /scenes/{id}/assets`.
- `src/vyom/api/models.py` — `SceneAssetsResponse` model.

**Frontend (new, under `web/`):**
- `web/package.json`, `vite.config.ts`, `tsconfig.json`, `tailwind.config.js`, `postcss.config.js`,
  `index.html`, `src/main.tsx`, `src/index.css`.
- `src/api/client.ts`, `src/api/types.ts`.
- `src/map/MapView.tsx`, `src/map/cogLayer.ts`, `src/map/colormaps.ts`, `src/map/aoiDraw.ts`,
  `src/map/compareSwipe.ts`.
- `src/components/AppShell.tsx`, `HealthBadge.tsx`, `EventSelector.tsx`, `SceneBrowser.tsx`,
  `LayerPanel.tsx`, `ChatPanel.tsx`, `Legend.tsx`.
- `src/store.ts`, `web/README.md`.

**Untouched:** entire `src/vyom/plugin/` (QGIS plugin), all ARD/agent/DB code.

---

## Build order (incremental, each step verifiable)

1. **Backend:** add CORS + `/ard` mount + `/scenes/{id}/assets`; verify in Swagger `/docs`.
2. **Scaffold** `web/` (Vite React-TS + Tailwind), proxy config, `AppShell` + `HealthBadge` —
   confirm `/health` renders.
3. **Events + map:** `EventSelector` + `MapView` with basemap; selecting Kerala fits the bbox.
4. **Scene browser + COG layers:** `SceneBrowser` → `/scenes/{id}/assets` → toggle an NDWI/
   false-color COG onto the map (validates `georaster` + overviews + colormaps).
5. **Layer panel:** opacity/colormap/reorder/remove + swipe compare.
6. **Chat:** `POST /query` → markdown answer + trace + inline charts + auto result rasters + zoom.
7. **AOI draw** → append GeoJSON to query → re-run; confirm agent scopes to the polygon.
8. **Polish:** legends, loading/error states, README.

---

## Verification (end-to-end, Kerala 2018)

1. **Backend:** `python -m vyom.api`; in `/docs` hit `GET /scenes/kerala_periyar_2018` then
   `GET /scenes/{id}/assets` → confirm `assets` URLs under `/ard/...` resolve (open one `.tif`
   URL → 200 + range support) and `bbox_wgs84` lands over Kerala (~76–77.5°E, 9.5–11°N).
2. **Frontend:** `npm run dev`; HealthBadge shows DB ok + provider (sarvam). Select
   *kerala_periyar_2018* → map fits Kerala.
3. **Browser:** expand a scene → toggle NDWI COG → renders over Kerala with the diverging ramp;
   toggle false-color → true-color composite; adjust opacity; swipe-compare two scenes.
4. **Chat:** ask *"How did water area change in the Kerala 2018 floods?"* → markdown answer +
   tool-call trace + inline `compare_windows`/`flood_extent` charts + flood/change rasters
   auto-overlaid at correct coordinates with auto-zoom.
5. **AOI:** draw a polygon over the Periyar basin → re-ask → confirm the agent's coverage/metrics
   scope to it.
6. **Regression:** QGIS plugin still connects and works unchanged (backend changes are additive).

## Out of scope
- Production build / single-server static hosting (chose local-dev only) — trivial follow-up.
- TiTiler / server-side tiling (chose client-side georaster).
- Ingesting the 11 other event manifests (only `kerala_periyar_2018` is loaded).
- `QueryRequest.aoi_geojson` field (v1 appends AOI to query text instead).


