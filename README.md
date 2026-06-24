# VYOM — Agentic GIS for Indian Disaster Analysis

**VYOM** (meaning "sky" in Sanskrit) is a research prototype that lets a scientist ask
natural-language questions about satellite imagery of Indian disasters and receive
spatial answers grounded in real data — without writing a single line of GIS code.

> *"How did water area change in the Kerala 2018 floods?"*
> → the system queries the database, computes indices on the correct scenes, renders a
> flood-extent map, and returns a structured answer with confidence information, all
> automatically.

---

## Table of Contents

1. [Motivation](#1-motivation)
2. [Satellite Data Used](#2-satellite-data-used)
3. [System Architecture](#3-system-architecture)
4. [Pre-processing Pipeline (ARD)](#4-pre-processing-pipeline-ard)
5. [Database Schema](#5-database-schema)
6. [LLM Agent and Its Tools](#6-llm-agent-and-its-tools)
7. [API Server](#7-api-server)
8. [QGIS Plugin](#8-qgis-plugin)
9. [How to Run](#9-how-to-run)
10. [What You Can Ask Right Now](#10-what-you-can-ask-right-now)
11. [Limitations and Known Issues](#11-limitations-and-known-issues)
12. [Roadmap — Making It Robust](#12-roadmap--making-it-robust)

---

## 1. Motivation

Traditional GIS disaster analysis requires a scientist to:
- Manually download scenes from BHUVAN/NRSC
- Open QGIS or write Python scripts
- Compute spectral indices (NDWI, NDVI, NBR…)
- Compare pre- and post-event imagery by hand

This works but creates a bottleneck: the domain expert spends hours on GIS operations
instead of scientific interpretation. VYOM inverts this. It exposes a chat interface where
a scientist describes what they want to know, and an LLM agent automatically selects the
right data, runs the right analysis, and returns the result — including a georeferenced
raster layer placed directly on the map.

The key design principle is **safety-first dispatching**: the agent is architecturally
prevented from hallucinating spatial statistics. Before any analysis, it must confirm
that actual data exists for the queried region (the `check_coverage` mandate). If no data
exists, it says so — it does not fabricate numbers.

---

## 2. Satellite Data Used

### Source and Download Method

All imagery was sourced from **NRSC/ISRO** at the Earth Observation Data Centre (EODC),
Hyderabad, using a custom bulk-download script (`scripts/download_data.py`) that queries
the **BHUVAN portal** (bhuvan.nrsc.gov.in) for each registered event's bounding box and
date range, then downloads zipped L2 scenes in batch. This script also auto-generates
the scene manifest CSV and registers the event entry in `config/events_registry.yaml` —
no manual cataloguing is required for a new event.

Data product level: **L2** (radiometrically and geometrically corrected by NRSC before
delivery). Each zip contains per-band GeoTIFFs (raw DN values) and a `BAND_META.txt`
with acquisition geometry, solar angles, and per-band calibration coefficients.

Total raw data downloaded: **~150 GB** (stored at NRSC; ARD products transferred to
this machine).

---

### Sensors (5 configurations registered in `config/band_registry.yaml`)

| Sensor key | Satellite | GSD | Bands available | SWIR | Computable indices |
|------------|-----------|-----|-----------------|------|--------------------|
| ResourceSat-2_LISS3_L2 | RS-2 | 23.5 m | B2(Green), B3(Red), B4(NIR), B5(SWIR1) | Yes | NDWI, NDVI, NBR, MNDWI |
| ResourceSat-2A_LISS3_L2 | RS-2A | 23.5 m | B2(Green), B3(Red), B4(NIR), B5(SWIR1) | Yes | NDWI, NDVI, NBR, MNDWI |
| ResourceSat-2A_LISS4-MX70_L2 | RS-2A | **5.8 m** | B2(Green), B3(Red), B4(NIR) | **No** | NDWI, NDVI only |
| ResourceSat-2_AWIFS_L2 | RS-2 | 56 m | B2(Green), B3(Red), B4(NIR), B5(SWIR1) | Yes | NDWI, NDVI, NBR |
| ResourceSat-2A_AWIFS_L2 | RS-2A | 56 m | B2(Green), B3(Red), B4(NIR), B5(SWIR1) | Yes | NDWI, NDVI, NBR |

Band wavelengths: Green 520–590 nm · Red 620–680 nm · NIR 770–860 nm · SWIR1 1550–1700 nm

---

### Events Registered in the System

6 events are registered in `config/events_registry.yaml`. 3 flood events have their
metadata and preprocessed COGs fully available; the remaining 3 have manifests created
but ARD files are still at NRSC.

#### Flood events — fully ingested (metadata + ARD COGs in DB)

**1. Kerala Periyar Floods — August 2018** (`kerala_periyar_2018`)

Worst flooding in Kerala in nearly a century. SW monsoon 2018 caused Periyar and
tributaries to overflow, inundating Aluva, Ernakulam, Pathanamthitta, and Alappuzha.
~483 deaths, 1.4 million displaced, INR 31,000 crore damage.

- **Bounding box:** 76.0°E – 77.5°E, 9.5°N – 11.0°N
- **Sensors used:** ResourceSat-2_LISS3_L2 (23.5 m) + ResourceSat-2A_LISS4-MX70_L2 (5.8 m)
- **UTM zones:** EPSG:32643 (43N) and EPSG:32644 (44N)

| Window | Count | Date range | Notes |
|--------|-------|-----------|-------|
| Annual baseline | 64 | 2015–2017 | Background vegetation/water reference |
| Pre-event | 5 | Jan – Jul 2018 | Dry-season baseline |
| Event | 1 | Sep 2018 | Only cloud-free scene; over headwaters, not floodplain |
| Post-event | 18 | Sep – Dec 2018 | Residual inundation and recovery |
| **Total** | **88** | 2015 – Dec 2018 | |

Cached metric rows: **1,455** · COG files on disk: **~1,409**

> **Caveat:** The single event-window scene covers the forested Periyar headwaters
> (Idukki), not the Aluva-Ernakulam floodplain where peak inundation occurred. Monsoon
> cloud cover blocked all optical acquisitions over the floodplain during August 2018.
> Temporal flood comparison therefore uses **pre vs. post** for floodplain AOIs.

---

**2. Assam Brahmaputra Floods — 2022** (`assam_brahmaputra_2022`)

Annual monsoon flooding of the Brahmaputra valley, 2022 edition — one of the most
severe in recent years, affecting 32 of 35 Assam districts.

- **Sensors used:** ResourceSat-2A_LISS4-MX70_L2 (5.8 m) only
- **Indices available:** NDWI, NDVI (no SWIR → no NBR/MNDWI)

| Window | Count |
|--------|-------|
| Event | 12 |
| Post-event | 30 |
| **Total** | **42** |

Cached metric rows: **426**

---

**3. Bihar Kosi-Ganga Floods — 2019** (`bihar_kosi_ganga_2019`)

Flooding of the Kosi and Ganga river systems in Bihar, 2019. Affected Supaul,
Darbhanga, Muzaffarpur and Saharsa districts.

- **Sensors used:** ResourceSat-2_LISS3_L2 (23.5 m) + ResourceSat-2A_LISS4-MX70_L2 (5.8 m)

| Window | Count |
|--------|-------|
| Pre-event | 1 |
| Event | 33 |
| Post-event | 29 |
| **Total** | **63** |

Cached metric rows: **734**

---

#### Summary across all ingested events

| Event | Scenes | Metric rows | Sensors |
|-------|--------|-------------|---------|
| kerala_periyar_2018 | 88 | 1,455 | LISS3 + LISS4 |
| assam_brahmaputra_2022 | 42 | 426 | LISS4 only |
| bihar_kosi_ganga_2019 | 63 | 734 | LISS3 + LISS4 |
| **Total** | **193** | **2,615** | |

Total COG `.tif` files on disk: **~1,409**

---

#### Events registered but ARD pending (manifests exist, COGs at NRSC)

| Event key | Hazard | Region | Primary index |
|-----------|--------|--------|---------------|
| wildfire_uttarakhand_2016 | Wildfire | Uttarakhand | NBR |
| landslide_sikkim_glof_2023 | Landslide/GLOF | Sikkim | dNDVI |
| drought_marathwada_2016 | Drought | Marathwada, Maharashtra | VCI |

Additionally, 6 more events have manifests created but are not yet in the registry:
Assam 2019, Cyclone Michaung 2023, Delhi Yamuna 2023, Godavari AP 2022, Kerala 2019,
Uttarakhand Kedarnath 2013.

---

## 3. System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Scientist / User                        │
└──────────────┬──────────────────────────────┬──────────────┘
               │ natural language              │ QGIS plugin
               ▼                              ▼
┌──────────────────────────┐    ┌─────────────────────────────┐
│    FastAPI REST Server   │◄───│   QGIS Plugin (PyQt5/6)     │
│    (python -m vyom.api)  │    │   vyom_dockwidget.py        │
│                          │    │   • Chat tab (query + answer)│
│  /health  /events        │    │   • Map tab (opacity, AOI,  │
│  /scenes  /query         │    │     threshold slider)       │
│  /exports /ard           │    │   • Scenes tab (browser)    │
└──────────┬───────────────┘    │   • Status tab (trace)      │
           │                    └─────────────────────────────┘
           ▼
┌──────────────────────────────────────────────────────────────┐
│               VYOM LLM Agent (orchestrator.py)               │
│                                                              │
│  LLM Backend priority:  Sarvam-30B → Groq → Gemini          │
│  Max steps: configurable (default 12)                        │
│  Safety rule: check_coverage MUST be called first            │
└──────┬───────────────────────┬───────────────────────────────┘
       │                       │
       ▼                       ▼
┌──────────────┐    ┌──────────────────────────────────────────┐
│  Catalog     │    │  Raster Analysis Tools                    │
│  Tools       │    │  (rasterio, numpy, shapely, matplotlib)  │
│  (psycopg2   │    │                                          │
│   only)      │    │  compute_change  clip_to_aoi             │
│              │    │  flood_extent    export_png              │
│  check_cov.  │    └──────────────────────────┬───────────────┘
│  list_scenes │                               │
│  get_metrics │                               │
│  by_date_rng │    ┌──────────────────────────▼───────────────┐
│  compare_win │    │  COG Files on disk  (data/ard/)           │
│  list_events │    │  Cloud-Optimized GeoTIFFs                 │
│  get_aoi     │    │  • false_color (NIR/R/G)                  │
└──────┬───────┘    │  • ndwi, ndvi, nbr, mndwi                │
       │            │  • cloud_mask                             │
       ▼            └──────────────────────────────────────────┘
┌──────────────────────────┐
│  PostgreSQL + PostGIS    │
│  agentic_gis_db          │
│                          │
│  scenes         (88 rows)│
│  scene_metrics (1455 rows│
│  derived_products        │
└──────────────────────────┘
```

---

## 4. Pre-processing Pipeline (ARD)

**ARD = Analysis-Ready Data.** Raw satellite data from ISRO arrives as digital numbers
(DNs) stored in GeoTIFFs. These raw values are not physically meaningful — they depend on
the sensor's gain settings, sun angle, atmospheric conditions, and the time of
acquisition. The ARD pipeline converts them into **surface reflectance** — the fraction
of sunlight reflected by the ground — which is physically comparable across dates and
sensors.

### Step 1 — Metadata parsing (`band_meta.py`)

Reads `BAND_META.txt` from each ISRO zip to extract:
- Acquisition date and time
- Scene ID and orbit parameters
- Per-band solar irradiance (ESUN) and gain/bias coefficients
- Corner coordinates → computes WGS84 footprint polygon

### Step 2 — Radiometric calibration (`calibrate.py`)

**DN → TOA Radiance:**

```
L = (DN × gain) + bias        [W m⁻² sr⁻¹ μm⁻¹]
```

**TOA Radiance → TOA Reflectance:**

```
ρ_TOA = (π × L × d²) / (ESUN × cos(θ_sun))
```
where `d` = Earth–Sun distance (AU), `θ_sun` = solar zenith angle.

**TOA → Surface Reflectance (DOS — Dark Object Subtraction):**

DOS is an atmospheric correction method that assumes the darkest pixel in the scene
(e.g. a deep shadow or clear water body) should theoretically have near-zero
reflectance. Any offset above zero is attributed to atmospheric path radiance (haze,
scattering).

```
ρ_surface = ρ_TOA − ρ_dark_object
```

Accuracy: ±5–10% surface reflectance. More sophisticated methods (MODTRAN, Sen2Cor)
would require atmospheric sounding profiles. DOS is the standard first-order correction
for archive satellite data where no concurrent atmospheric measurements exist.

GPU acceleration is used automatically via CuPy if a CUDA-capable GPU is present,
falling back to NumPy otherwise.

### Step 3 — Index computation (`indices.py`)

From the four calibrated surface-reflectance bands (green, red, NIR, SWIR):

| Index | Formula | Physical meaning | Used for |
|-------|---------|-----------------|---------|
| **NDWI** | (Green − NIR) / (Green + NIR) | Water-positive: open water > 0.3, dry land < 0 | Flood mapping |
| **MNDWI** | (Green − SWIR) / (Green + SWIR) | Better than NDWI in urban areas | Urban flood |
| **NDVI** | (NIR − Red) / (NIR + Red) | Vegetation density: dense veg > 0.5 | Crop stress, drought |
| **NBR** | (NIR − SWIR) / (NIR + SWIR) | Burn sensitivity: unburned > 0, severely burned < −0.5 | Wildfire |

A **cloud mask** is also computed using brightness thresholds (green > 0.35 AND NIR > 0.30).
All indices are float32 in the range [−1, +1] with NaN over clouds/nodata.

Index summary statistics (mean, std, percentiles, valid pixel fraction) are computed per
scene and cached in the `scene_metrics` table.

### Step 4 — COG generation (`cog.py`)

Each output is written as a **Cloud-Optimized GeoTIFF (COG)**:
- Internal tiling: 256 × 256 pixels
- Compression: DEFLATE (lossless)
- Internal overviews at 2×, 4×, 8×, 16× for fast zoom levels
- GDAL /vsicurl/ compatible: the QGIS plugin can stream tiles on demand without
  downloading the full 800 MB file

### Step 5 — Database registration (`writer.py`)

The pipeline registers the scene in PostgreSQL with:
- PostGIS geometry (WGS84 footprint polygon)
- Full metadata (sensor, satellite, cloud cover, GSD, window type)
- Asset paths (relative paths to each COG)
- Pipeline version tag (bumping this invalidates cached metrics → pipeline re-runs
  only on affected scenes)

---

## 5. Database Schema

```sql
scenes (
    id              TEXT PRIMARY KEY,    -- ISRO scene ID
    collection      TEXT,                -- event_key
    satellite       TEXT,                -- ResourceSat-2 / ResourceSat-2A
    sensor          TEXT,                -- ResourceSat-2A_LISS4-MX70_L2
    geometry        GEOMETRY(Polygon, 4326),   -- WGS84 footprint
    acq_datetime    TIMESTAMPTZ,
    cloud_cover     FLOAT,               -- percent
    gsd_m           FLOAT,               -- ground sample distance in metres
    window_type     TEXT,                -- pre_event / event / post_event / annual
    event_key       TEXT,                -- e.g. kerala_periyar_2018
    assets          JSONB,               -- {"ndwi": "data/ard/.../ndwi.tif", ...}
    properties      JSONB,               -- raw metadata
    pipeline_version TEXT                -- e.g. "v1.0-dos"
)

scene_metrics (
    scene_id        TEXT REFERENCES scenes(id),
    aoi_hash        TEXT,                -- MD5 of AOI GeoJSON or "full_scene"
    metric          TEXT,                -- e.g. "water_area_pct", "mean_ndwi"
    value           FLOAT,
    pipeline_version TEXT
)
```

---

## 6. LLM Agent and Its Tools

The agent uses a **function-calling loop**: at each step, the LLM either calls a tool
or returns a final answer. The loop enforces:

1. `check_coverage` MUST be the first data-accessing tool — prevents hallucinated stats
2. Heavy raster tools (compute_change, flood_extent, etc.) are only allowed after
   coverage is confirmed
3. Maximum steps: configurable (default 12)

### Catalog tools (fast, psycopg2 only)

| Tool | What it does |
|------|-------------|
| `list_events` | Returns all 6 registered disaster events with bbox and hazard type |
| `get_event_aoi` | Returns the WGS84 bounding box GeoJSON for a specific event |
| `check_coverage` | Counts how many scenes exist for an AOI + event; MUST be first |
| `list_scenes` | Lists scenes filtered by event, window type, sensor, cloud cover |
| `get_scene_metrics` | Returns cached index statistics for one scene |
| `scenes_by_date_range` | Finds scenes within a date window, with optional AOI filter |
| `compare_windows` | Aggregates a metric across pre/event/post windows + computes deltas + interpretation |

### Raster tools (heavy — rasterio, numpy, shapely)

| Tool | What it does | Output |
|------|-------------|--------|
| `flood_extent` | Applies NDWI > threshold to produce a binary flood mask; returns water pixels, percent, km² | GeoTIFF + cached metric |
| `compute_change` | Pixel-wise index delta (B − A) between two scenes; handles CRS reprojection | Float32 GeoTIFF |
| `clip_to_aoi` | Clips any COG to a user-supplied WGS84 polygon; returns stats | Cached metric |
| `export_png` | Renders false-colour (NIR/R/G) or index (diverging colormap) as a PNG | PNG in data/exports/ |

### Server-side charts (deterministic, not LLM-generated)

After the agent returns, the API server automatically renders matplotlib charts from the
tool call results — bar charts for `compare_windows`, pie charts for `flood_extent`,
change breakdown for `compute_change`. These charts appear inline in the QGIS chat
panel and are not dependent on the LLM (no hallucination risk).

---

## 7. API Server

A **FastAPI** REST server wraps the agent and catalog tools. All clients (QGIS plugin,
React web app, Swagger UI) talk to this server.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | DB status, LLM provider, key configured |
| `/events` | GET | List all registered events |
| `/scenes` | GET | List scenes with filters (event, window, cloud, sensor) |
| `/scenes/{id}/assets` | GET | COG asset URLs + WGS84 footprint for a scene |
| `/query` | POST | Run the agent on a natural-language question |
| `/exports/{file}` | GET | Download a PNG or GeoTIFF produced by analysis |
| `/ard/...` | GET (static) | Stream COG files via HTTP Range (for QGIS vsicurl) |

Swagger interactive docs at `http://127.0.0.1:8000/docs`

---

## 8. QGIS Plugin

A thin Qt-based client installed as a dock panel in QGIS. It has four tabs:

**Chat tab** — The primary interface:
- Event picker (populated automatically on Connect)
- Query templates per hazard type (flood/wildfire/drought/landslide)
- Answer rendered as markdown with tables, inline charts, and a confidence badge
  (green/orange/red based on coverage check + cloud cover)
- Layer explanation appears in chat when a raster is added to the map
- "Copy last answer" button

**Map tab** — Controls on the active layers:
- Opacity slider for the last VYOM raster layer
- NDWI threshold slider (0.10–0.60) + "Re-run flood extent" button
- Draw custom AOI rectangle on the map (overrides event bbox for the next question)
- Clear session (removes all VYOM layers + clears chat)

**Scenes tab** — Browse all 88 scenes in a table (date, window, cloud%, sensor).
Double-click any row to zoom to that scene's footprint on the map.

**Status tab** — Connection health detail and full tool-call trace for the last query.

The Connect button shows live states:
- **● Connected** (green) — DB and LLM key both ready
- **⚠ Degraded** (orange) — connected but LLM key missing or DB down
- **✗ Failed — retry** (red) — server unreachable

---

## 9. How to Run

### Prerequisites

- Python 3.11 (virtual environment at `myenv/`)
- PostgreSQL 17 + PostGIS (DB: `agentic_gis_db`)
- QGIS 3.16+ or QGIS 4.0+ (plugin supports both)
- `.env` file with `SARVAM_API_KEY`, `DB_URL`, `GEMINI_API_KEY`

### 1. Start the API server (keep this terminal open)

```powershell
cd C:\Users\krish\Desktop\AGIS-3
$env:PYTHONPATH = "C:\Users\krish\Desktop\AGIS-3\src"
myenv\Scripts\python -m vyom.api
# Swagger UI: http://127.0.0.1:8000/docs
```

### 2. Open QGIS

1. Open QGIS 4
2. Go to **View → Panels → Layers** to show the layer panel
3. Go to **Plugins → Manage and Install Plugins → Installed**
4. Enable **VYOM — Agentic GIS**
5. The VYOM dock appears on the right

### 3. Connect and query

1. In the VYOM panel, click **Connect** — button turns green when ready
2. Select an event from the dropdown (e.g. *Kerala floods 2018*)
3. Pick a query template or type your own question
4. Click **Ask** — the agent runs (typically 15–30 seconds)
5. Answer appears in chat; flood/change rasters are auto-added to the map

### 4. CLI (quick test without QGIS)

```powershell
$env:PYTHONPATH = "C:\Users\krish\Desktop\AGIS-3\src"
myenv\Scripts\python -m vyom.agent "How did water area change in the Kerala 2018 floods?"
```

---

## 10. What You Can Ask Right Now

These questions work with the current Kerala 2018 dataset:

```
How did water area change in the Kerala 2018 floods?
Which Kerala 2018 scene has the lowest cloud cover?
Calculate the flood extent for the Kerala 2018 event scene in km².
Show the NDWI change map between the pre-event and post-event scenes.
Export a false-colour image of the Kerala 2018 post-event scene.
List all post-event scenes from December 2018.
What was the mean NDWI in the Kerala 2018 event-window scene?
Compare vegetation health before and after the Kerala 2018 floods.
```

---

## 11. Limitations and Known Issues

### Scientific limitations

**Cross-sensor comparison is confounded.** Kerala 2018 pre-event scenes are mostly
LISS-III (23.5 m) while event/post-event scenes are LISS-IV (5.8 m). Because pixel
area differs 16×, fractional water coverage (water pixels / total pixels) is NOT
directly comparable across windows. `compare_windows` returns a sensor_warning when this
occurs. The correct approach — resampling both to a common resolution before computing
fractions — is planned.

**The single event-window optical scene is over the headwaters, not the floodplain.**
The lower Periyar/Aluva floodplain (Aluva, Ernakulam) had no cloud-free event-window
LISS data. The agent correctly surfaces this as a coverage gap rather than fabricating a
flood signal.

**DOS atmospheric correction has ±5–10% uncertainty.** Reflectance values should be
interpreted as relative (change detection) rather than absolute. Cross-date absolute
comparisons have this inherent uncertainty.

### Technical limitations

- Only ISRO ResourceSat LISS/AWIFS data is supported. Sentinel-2, Landsat, and other
  sensors would require a new metadata parser.
- Only the Kerala 2018 event is ingested on this machine. Other events in the registry
  (Assam, Bihar, wildfire, etc.) need their ARD data transferred before they can be
  queried.
- NDWI flood threshold of 0.30 (McFeeters 1996) is a scene-wide default. Mixed pixels
  at the urban–water boundary need per-scene calibration for production use.

---

## 12. Roadmap — Making It Robust

### Phase A — Multi-sensor support (no code changes for new ISRO sensors)

**Problem:** ESUN calibration values are currently hardcoded in `calibrate.py`. Adding
ResourceSat-3 or a new LISS variant requires editing source code.

**Solution:** Move all sensor constants (ESUN values, band-to-canonical mappings,
available indices) into `config/band_registry.yaml`. A new sensor becomes a YAML edit:

```yaml
ResourceSat-3_LISS3_L2:
  satellite: ResourceSat-3
  gsd_m: 23.5
  bands:
    B2: {canonical: green, wavelength_nm: [520, 590], esun: 1855}
    B3: {canonical: red,   wavelength_nm: [620, 680], esun: 1572}
    B4: {canonical: nir,   wavelength_nm: [770, 860], esun: 1052}
    B5: {canonical: swir1, wavelength_nm: [1550,1700], esun: 232}
  available_indices: [ndwi, ndvi, nbr, mndwi]
```

Zero code changes for the calibration or index pipeline.

### Phase B — Automatic window classification (no manual CSV column)

**Problem:** A human must label each scene as `pre_event / event / post_event / annual`
in the manifest CSV. This is error-prone and bottlenecks ingestion.

**Solution:** Add `event_window` (start date, end date) to `events_registry.yaml`.
The pipeline classifies automatically using acquisition date:

```
date < event_start − 30 days       →  pre_event
event_start − 30 ≤ date ≤ event_end + 14  →  event
event_end + 14 < date < event_end + 180   →  post_event
otherwise                           →  annual
```

This is exactly how Google Earth Engine users filter temporal windows with `filterDate()`.

### Phase C — Cross-sensor radiometric consistency

**Problem:** Comparing LISS-III (23.5 m) and LISS-IV (5.8 m) fractional metrics
without resampling gives physically meaningless deltas (the metric changes because
pixel size changed, not because water changed).

**Solution (two parts):**

1. **Flag it now (30 lines):** Add `sensor_warning` to `compare_windows` output when
   sensors differ across compared windows. The agent includes the caveat in its answer.

2. **Fix it properly (future):** Before computing a fractional metric in `clip_to_aoi`
   or `flood_extent`, resample all input scenes to the coarsest GSD in the comparison
   set using `rasterio.warp.reproject`. This makes fractions GSD-invariant.

### Phase D — Multi-source ingestion adapter

**Problem:** `band_meta.py` parses only ISRO's `BAND_META.txt` format. Adding
Sentinel-2 requires a complete parallel parser.

**Solution:** Define a common `SceneMetadata` output contract and implement one adapter
per source format:

```python
class IngestAdapter:
    @staticmethod
    def can_handle(folder: Path) -> bool: ...
    @staticmethod
    def parse(folder: Path) -> SceneMetadata: ...

class ISROAdapter(IngestAdapter):     # current behaviour, unchanged
    # looks for BAND_META.txt

class Sentinel2Adapter(IngestAdapter):
    # looks for MTD_MSIL2A.xml

class LandsatAdapter(IngestAdapter):
    # looks for *_MTL.txt
```

Pipeline auto-detects: tries each adapter in order until one succeeds. Zero downstream
changes — `calibrate.py`, `indices.py`, `cog.py`, and `writer.py` all take
`SceneMetadata`, not anything format-specific. (Google Earth Engine uses the same
pattern with its collection drivers.)

### Phase E — Drop-zone automation

**Problem:** Adding a new event still requires a human to run the pipeline manually.

**Solution:** A file-system watcher on `data/inbox/`. When a new zip is dropped:
1. Auto-detects sensor (Phase D)
2. Auto-classifies window (Phase B)
3. Checks `events_registry.yaml` — auto-registers the event if the scene's footprint
   overlaps a known disaster region (via PostGIS `ST_Intersects`)
4. Runs the ARD pipeline
5. Notifies the scientist: "New scene ingested — 2 new scenes available for Kerala 2018"

### Phase F — Ground-truth validation

**Problem:** We validate the NDWI flood mask by cross-index consistency (NDWI vs
NDVI-low vs MNDWI) because no reference flood polygons are available.

**Solution:** Request the NRSC/SDMMC Kerala 2018 inundation GeoTIFF (available through
NRSC's internal data portal). Once obtained:
- Compute IoU, precision, recall, F1 between NDWI mask and reference
- Report accuracy alongside the consistency metrics
- Use as ground truth for threshold calibration (find per-scene optimal threshold
  rather than the global McFeeters 0.30)

### Summary timeline

| Phase | Effort | Outcome |
|-------|--------|---------|
| A — YAML-only sensor config | 1 day | Any new ISRO sensor in minutes |
| B — Auto window classification | 1 day | Remove manual CSV labelling |
| C — Sensor consistency warning | 2 hours | Scientific integrity flag in answers |
| D — Multi-source adapter | 3 days | Sentinel-2 / Landsat ingestion |
| E — Drop-zone automation | 2 days | Zero-touch data ingestion |
| F — Ground-truth validation | depends on NRSC data | Quantitative accuracy metrics |

---

## References

- McFeeters, S.K. (1996). *The use of the Normalized Difference Water Index (NDWI) in the delineation of open water features.* Int. J. Remote Sensing, 17(7), 1425–1432.
- Xu, H. (2006). *Modification of normalised difference water index (NDWI) to enhance open water features in remotely sensed imagery.* Int. J. Remote Sensing, 27(14), 3025–3033.
- Chavez, P.S. (1988). *An improved dark-object subtraction technique for atmospheric scattering correction of multispectral data.* Remote Sensing of Environment, 24(3), 459–479.
- ISRO/NRSC ResourceSat-2/2A Data Users Handbook, 2014.
- Gorelick, N. et al. (2017). *Google Earth Engine: Planetary-scale geospatial analysis for everyone.* Remote Sensing of Environment, 202, 18–27.

---

*Built with ResourceSat-2/2A imagery from ISRO/NRSC · Sarvam-30B LLM · PostGIS · FastAPI · QGIS · Python 3.11*
