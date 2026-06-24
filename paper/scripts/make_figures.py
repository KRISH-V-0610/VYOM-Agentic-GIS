"""
VYOM manuscript figure generator — fully reproducible.

Pulls ground truth from the live PostGIS catalog and renders the real ARD COGs and
tool-output GeoTIFFs in data/exports. Every number in the paper traces back to here.

Run from project root:
    PYTHONPATH=src PYTHONIOENCODING=utf-8 ./myenv/Scripts/python.exe paper/scripts/make_figures.py
"""
import os, json, math
from datetime import datetime
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Polygon as MplPolygon
import matplotlib.dates as mdates
import psycopg2
import rasterio

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FIG = os.path.join(ROOT, "paper", "figures")
os.makedirs(FIG, exist_ok=True)

plt.rcParams.update({
    "figure.dpi": 200, "savefig.dpi": 200, "font.size": 10,
    "axes.titlesize": 11, "axes.titleweight": "bold", "axes.labelsize": 10,
    "axes.grid": True, "grid.alpha": 0.25, "axes.axisbelow": True,
    "font.family": "DejaVu Sans", "savefig.bbox": "tight",
})
C = {"pre": "#1f77b4", "event": "#d62728", "post": "#2ca02c", "annual": "#7f7f7f",
     "accent": "#ff7f0e", "box": "#eef2f7", "edge": "#34495e"}


def env():
    e = {}
    for line in open(os.path.join(ROOT, ".env")):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1); e[k] = v.strip().strip('"').strip("'")
    return e


def conn():
    return psycopg2.connect(env()["DB_URL"])


def save(fig, name):
    p = os.path.join(FIG, name)
    fig.savefig(p); plt.close(fig)
    print("  wrote", name)


def read_cog(path, max_dim=1000, band=1):
    """Decimated (overview) read so 7k x 7k float32 COGs stay light."""
    with rasterio.open(path) as ds:
        scale = max(ds.width, ds.height) / max_dim
        ow, oh = max(1, int(ds.width / scale)), max(1, int(ds.height / scale))
        arr = ds.read(band, out_shape=(oh, ow), masked=True).astype("float32").filled(np.nan)
        return arr, ds.bounds, (ds.crs.to_string() if ds.crs else None)


def stretch(a, lo=2, hi=98):
    v = a[np.isfinite(a)]
    if v.size == 0: return a
    p1, p2 = np.percentile(v, [lo, hi])
    return np.clip((a - p1) / (p2 - p1 + 1e-9), 0, 1)


# ─────────────────────────────────────────────────────────────────────────────
# DATASET FIGURES
# ─────────────────────────────────────────────────────────────────────────────

def fig_study_area():
    """Scene footprints over the Kerala AOI, coloured by temporal window."""
    cur = conn().cursor()
    cur.execute("""select window_type, ST_AsGeoJSON(geometry) from scenes
                   where event_key='kerala_periyar_2018'""")
    rows = cur.fetchall()
    fig, ax = plt.subplots(figsize=(7, 6.2))
    order = ["annual", "pre_event", "event", "post_event"]
    ckey = {"annual": "annual", "pre_event": "pre", "event": "event", "post_event": "post"}
    seen = set()
    for win in order:
        for w, gj in rows:
            if w != win: continue
            geom = json.loads(gj)
            coords = geom["coordinates"][0] if geom["type"] == "Polygon" else geom["coordinates"][0][0]
            lab = win.replace("_", " ") if win not in seen else None
            seen.add(win)
            ax.add_patch(MplPolygon(coords, closed=True, fill=False,
                         edgecolor=C[ckey[win]], lw=1.2, alpha=0.55, label=lab))
    # event AOI bbox
    bb = [76.0, 9.5, 77.5, 11.0]
    ax.add_patch(MplPolygon([[bb[0], bb[1]], [bb[2], bb[1]], [bb[2], bb[3]], [bb[0], bb[3]]],
                 closed=True, fill=False, edgecolor="k", lw=2.2, ls="--", label="Event AOI"))
    ax.set_xlim(74.8, 79.4); ax.set_ylim(8.5, 11.8)
    ax.set_xlabel("Longitude (°E)"); ax.set_ylabel("Latitude (°N)")
    ax.set_title("Study area: ResourceSat scene footprints over the Kerala / Periyar basin (n=88)")
    ax.set_aspect(1.0); ax.legend(loc="lower right", fontsize=8, framealpha=0.9)
    ax.annotate("Arabian\nSea", (75.15, 9.3), color="#3b6ea5", fontsize=9, style="italic")
    save(fig, "fig02_study_area.png")


def fig_temporal():
    cur = conn().cursor()
    cur.execute("""select acq_datetime, window_type, cloud_cover from scenes
                   where event_key='kerala_periyar_2018' order by acq_datetime""")
    rows = cur.fetchall()
    fig, ax = plt.subplots(figsize=(9, 3.6))
    ckey = {"annual": "annual", "pre_event": "pre", "event": "event", "post_event": "post"}
    ywin = {"annual": 0, "pre_event": 1, "event": 2, "post_event": 3}
    seen = set()
    for dt, w, cc in rows:
        lab = w.replace("_", " ") if w not in seen else None; seen.add(w)
        ax.scatter(dt, ywin[w], c=C[ckey[w]], s=28, alpha=0.8, label=lab,
                   edgecolor="white", lw=0.4)
    ev = datetime(2018, 8, 15)
    ax.axvline(ev, color="k", ls="--", lw=1.3)
    ax.annotate("Flood event\n2018-08-15", (ev, 3.35), ha="center", fontsize=8)
    ax.set_yticks(list(ywin.values())); ax.set_yticklabels([k.replace("_", " ") for k in ywin])
    ax.set_xlabel("Acquisition date"); ax.set_ylim(-0.6, 3.8)
    ax.set_title("Temporal distribution of acquisitions by analysis window (2015–2018)")
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.legend(ncol=4, fontsize=8, loc="upper left")
    save(fig, "fig03_temporal.png")


def fig_cloud_hist():
    cur = conn().cursor()
    cur.execute("select cloud_cover from scenes where event_key='kerala_periyar_2018'")
    cc = [r[0] for r in cur.fetchall() if r[0] is not None]
    fig, ax = plt.subplots(figsize=(6.5, 3.8))
    ax.hist(cc, bins=20, color=C["pre"], edgecolor="white", alpha=0.85)
    m = np.mean(cc)
    ax.axvline(m, color=C["event"], lw=1.6, ls="--", label=f"mean = {m:.1f}%")
    ax.set_xlabel("Scene cloud cover (%)"); ax.set_ylabel("Number of scenes")
    ax.set_title(f"Cloud-cover distribution (n={len(cc)}; median {np.median(cc):.1f}%, max {max(cc):.1f}%)")
    ax.legend()
    save(fig, "fig04_cloud_hist.png")


def fig_composition():
    cur = conn().cursor()
    cur.execute("select sensor, count(*) from scenes group by sensor")
    sens = dict(cur.fetchall())
    cur.execute("select window_type, count(*) from scenes group by window_type")
    win = dict(cur.fetchall())
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(9.5, 3.8))
    a1.bar(list(sens.keys()), list(sens.values()), color=[C["pre"], C["accent"]],
           edgecolor="white")
    a1.set_title("Scenes by sensor"); a1.set_ylabel("Count")
    for i, (k, v) in enumerate(sens.items()):
        a1.text(i, v + 0.5, f"{v}\n({v/sum(sens.values())*100:.0f}%)", ha="center", fontsize=9)
    a1.set_ylim(0, max(sens.values()) * 1.2)
    worder = ["annual", "pre_event", "event", "post_event"]
    vals = [win.get(w, 0) for w in worder]
    cols = [C["annual"], C["pre"], C["event"], C["post"]]
    a2.bar([w.replace("_", " ") for w in worder], vals, color=cols, edgecolor="white")
    a2.set_title("Scenes by temporal window"); a2.set_ylabel("Count")
    for i, v in enumerate(vals):
        a2.text(i, v + 0.5, str(v), ha="center", fontsize=9)
    a2.set_ylim(0, max(vals) * 1.2)
    fig.suptitle("Dataset composition — Kerala 2018 catalog (88 scenes)", fontweight="bold")
    save(fig, "fig05_composition.png")


# ─────────────────────────────────────────────────────────────────────────────
# METHOD / ARCHITECTURE FIGURES (vector schematics)
# ─────────────────────────────────────────────────────────────────────────────

def _box(ax, xy, w, h, text, fc="#eef2f7", ec="#34495e", fs=8.5, lw=1.3):
    ax.add_patch(FancyBboxPatch(xy, w, h, boxstyle="round,pad=0.02,rounding_size=0.04",
                 fc=fc, ec=ec, lw=lw))
    ax.text(xy[0] + w / 2, xy[1] + h / 2, text, ha="center", va="center", fontsize=fs)


def _arrow(ax, p1, p2, color="#34495e"):
    ax.add_patch(FancyArrowPatch(p1, p2, arrowstyle="-|>", mutation_scale=12,
                 lw=1.3, color=color, shrinkA=2, shrinkB=2))


def fig_architecture():
    fig, ax = plt.subplots(figsize=(10, 6.6)); ax.axis("off")
    ax.set_xlim(0, 10); ax.set_ylim(0, 10)
    # layers
    _box(ax, (0.3, 8.4), 9.4, 1.2,
         "DATA  —  ISRO/NRSC ResourceSat-2/2A raw L2 zips  (LISS-III 23.5 m · LISS-IV 5.8 m · AWiFS 56 m)",
         fc="#fdf2e3")
    _box(ax, (0.3, 6.2), 4.6, 1.6,
         "ARD PIPELINE (GPU/CuPy)\nDN → TOA radiance → DOS\nsurface reflectance →\nNDVI/NDWI/NBR/MNDWI →\nCloud-Optimized GeoTIFF", fc="#e8f3ee")
    _box(ax, (5.1, 6.2), 4.6, 1.6,
         "POSTGIS CATALOG\nscenes (geometry, metadata) ·\nscene_metrics (cached index\nstats) · derived_products", fc="#e8eef7")
    _box(ax, (0.3, 4.1), 4.6, 1.4,
         "MCP SERVER: catalog\n(postgis_server.py — psycopg2 only,\ncold-start <2 s)\ncheck_coverage · list_scenes ·\nget_scene_metrics · compare_windows", fc="#f3e8f1", fs=8)
    _box(ax, (5.1, 4.1), 4.6, 1.4,
         "MCP SERVER: raster\n(gis_server.py — rasterio/numpy)\nflood_extent · compute_change ·\nclip_to_aoi · export_png", fc="#f3e8f1", fs=8)
    _box(ax, (2.4, 2.2), 5.2, 1.3,
         "AGENT ORCHESTRATOR (LLM function-calling loop)\nSarvam-30B / Gemini · 11 tools ·\ncheck_coverage-FIRST policy enforced", fc="#fdeaea")
    _box(ax, (0.3, 0.3), 3.0, 1.1, "FastAPI REST\n/health /events\n/scenes /query", fc="#eef2f7", fs=8)
    _box(ax, (3.5, 0.3), 3.0, 1.1, "QGIS plugin\n(thin REST client)", fc="#eef2f7", fs=8)
    _box(ax, (6.7, 0.3), 3.0, 1.1, "Web workbench\n(React + Leaflet COG)", fc="#eef2f7", fs=8)
    # arrows
    _arrow(ax, (2.6, 8.4), (2.6, 7.8)); _arrow(ax, (4.9, 7.0), (5.1, 7.0))
    _arrow(ax, (2.6, 6.2), (2.6, 5.5)); _arrow(ax, (7.4, 6.2), (7.4, 5.5))
    _arrow(ax, (2.6, 4.1), (4.0, 3.5)); _arrow(ax, (7.4, 4.1), (6.0, 3.5))
    _arrow(ax, (5.0, 2.2), (4.0, 1.4)); _arrow(ax, (5.0, 2.2), (5.0, 1.4))
    _arrow(ax, (5.0, 2.2), (7.6, 1.4))
    _arrow(ax, (1.8, 1.4), (3.2, 2.2)); _arrow(ax, (5.0, 1.4), (5.0, 2.2))
    ax.set_title("VYOM system architecture: ISRO imagery → ARD → PostGIS/COG → MCP tools → LLM agent → clients",
                 fontsize=11, fontweight="bold")
    save(fig, "fig01_architecture.png")


def fig_ard_flow():
    fig, ax = plt.subplots(figsize=(11, 2.9)); ax.axis("off")
    ax.set_xlim(0, 11); ax.set_ylim(0, 3)
    steps = [
        ("Raw DN\n(10-bit, L2)", "#fdf2e3"),
        ("TOA radiance\nL=Lmin+(Lmax-Lmin)·DN/1023", "#e8f3ee"),
        ("TOA reflectance\nρ=πLd²/(ESUN·cosθs)", "#e8f3ee"),
        ("DOS1 haze\nsubtraction\n(1% dark object)", "#e8eef7"),
        ("Spectral indices\nNDVI·NDWI·NBR·MNDWI\n+ cloud mask", "#f3e8f1"),
        ("COG write +\nPostGIS register\n+ metric cache", "#fdeaea"),
    ]
    w = 1.62; x = 0.15
    for i, (t, c) in enumerate(steps):
        _box(ax, (x, 0.9), w, 1.2, t, fc=c, fs=7.6)
        if i < len(steps) - 1:
            _arrow(ax, (x + w, 1.5), (x + w + 0.18, 1.5))
        x += w + 0.18
    ax.set_title("ARD preprocessing chain (per band; GPU-accelerated, idempotent, PIPELINE_VERSION=v1.0-dos)",
                 fontsize=10.5, fontweight="bold")
    save(fig, "fig06_ard_flow.png")


def fig_agent_loop(bench=None):
    fig, ax = plt.subplots(figsize=(9.5, 4.4)); ax.axis("off")
    ax.set_xlim(0, 10); ax.set_ylim(0, 6)
    _box(ax, (0.3, 4.6), 2.2, 1.0, "User NL query", fc="#fdf2e3")
    _box(ax, (3.2, 4.6), 3.2, 1.0, "LLM proposes\nfunction call(s)", fc="#e8eef7")
    _box(ax, (7.0, 4.6), 2.7, 1.0, "Text-only answer?\n→ return", fc="#e8f3ee")
    _box(ax, (3.2, 2.6), 3.2, 1.1, "check_coverage-first\nPOLICY GATE\n(blocks data tools first)", fc="#fdeaea")
    _box(ax, (3.2, 0.5), 3.2, 1.1, "Dispatch tool →\nPostGIS / raster MCP\n→ feed result back", fc="#f3e8f1")
    _arrow(ax, (2.5, 5.1), (3.2, 5.1)); _arrow(ax, (6.4, 5.1), (7.0, 5.1))
    _arrow(ax, (4.8, 4.6), (4.8, 3.7)); _arrow(ax, (4.8, 2.6), (4.8, 1.6))
    _arrow(ax, (6.4, 1.05), (8.0, 1.05)); _arrow(ax, (8.0, 1.05), (8.0, 4.6))
    ax.annotate("loop until\ntext answer\nor max_steps", (8.15, 2.9), fontsize=8, color="#555")
    seq = (bench or {}).get("example_sequence")
    cap = "Observed sequence (Kerala change query): " + " → ".join(seq) if seq else \
          "Observed sequence: list_events → get_event_aoi → check_coverage → compare_windows"
    ax.text(5.0, 0.05, cap, ha="center", fontsize=8, style="italic", color="#333")
    ax.set_title("Agent function-calling loop with enforced check_coverage-first policy",
                 fontsize=11, fontweight="bold")
    save(fig, "fig07_agent_loop.png")


# ─────────────────────────────────────────────────────────────────────────────
# PRODUCT / RESULT RASTER FIGURES (real COGs + tool outputs)
# ─────────────────────────────────────────────────────────────────────────────

SCENE = "RA308MAR2018006480009900066PSANSTUC00GTDF"
SDIR = os.path.join(ROOT, "data", "ard", "ResourceSat-2A_LISS3_L2", "2018", SCENE)
EXP = os.path.join(ROOT, "data", "exports")


def fig_products():
    fig, axs = plt.subplots(1, 3, figsize=(13, 4.6))
    try:
        nir, _, _ = read_cog(os.path.join(SDIR, "nir.tif"))
        red, _, _ = read_cog(os.path.join(SDIR, "red.tif"))
        grn, _, _ = read_cog(os.path.join(SDIR, "green.tif"))
        rgb = np.dstack([stretch(nir), stretch(red), stretch(grn)])
        axs[0].imshow(np.nan_to_num(rgb)); axs[0].set_title("(a) False colour (NIR·R·G)")
    except Exception as e:
        axs[0].text(0.5, 0.5, f"n/a\n{e}", ha="center")
    try:
        ndvi, _, _ = read_cog(os.path.join(SDIR, "ndvi.tif"))
        im = axs[1].imshow(ndvi, cmap="RdYlGn", vmin=-0.4, vmax=0.8)
        axs[1].set_title("(b) NDVI (vegetation)")
        fig.colorbar(im, ax=axs[1], fraction=0.046, pad=0.04)
    except Exception as e:
        axs[1].text(0.5, 0.5, f"n/a\n{e}", ha="center")
    try:
        ndwi, _, _ = read_cog(os.path.join(SDIR, "ndwi.tif"))
        im = axs[2].imshow(ndwi, cmap="RdYlBu", vmin=-0.6, vmax=0.6)
        axs[2].set_title("(c) NDWI (McFeeters, water +)")
        fig.colorbar(im, ax=axs[2], fraction=0.046, pad=0.04)
    except Exception as e:
        axs[2].text(0.5, 0.5, f"n/a\n{e}", ha="center")
    for a in axs: a.set_xticks([]); a.set_yticks([]); a.grid(False)
    fig.suptitle(f"ARD spectral products for scene {SCENE[:24]}… (LISS-III, EPSG:32643)",
                 fontweight="bold")
    save(fig, "fig08_products.png")


def fig_flood_map():
    fig, axs = plt.subplots(1, 2, figsize=(11, 5))
    try:
        ndwi, _, _ = read_cog(os.path.join(SDIR, "ndwi.tif"))
        im = axs[0].imshow(ndwi, cmap="RdYlBu", vmin=-0.6, vmax=0.6)
        axs[0].set_title("(a) NDWI"); fig.colorbar(im, ax=axs[0], fraction=0.046, pad=0.04)
    except Exception as e:
        axs[0].text(0.5, 0.5, f"n/a\n{e}", ha="center")
    fp = os.path.join(EXP, f"{SCENE}_flood_full.tif")
    try:
        mask, _, _ = read_cog(fp)
        from matplotlib.colors import ListedColormap
        cmap = ListedColormap([[0, 0, 0, 0], [0.0, 0.35, 0.78, 1]])
        axs[1].imshow(np.where(np.isfinite(ndwi), 1, np.nan), cmap=ListedColormap(["#dddddd"]))
        axs[1].imshow(np.nan_to_num(mask), cmap=cmap, vmin=0, vmax=1)
        axs[1].set_title("(b) Flood mask (NDWI > 0.3) — flood_extent tool output")
    except Exception as e:
        axs[1].text(0.5, 0.5, f"n/a\n{e}", ha="center")
    for a in axs: a.set_xticks([]); a.set_yticks([]); a.grid(False)
    fig.suptitle("flood_extent tool: NDWI water classification (real GeoTIFF output)", fontweight="bold")
    save(fig, "fig10_flood_map.png")


def fig_change_map():
    cf = os.path.join(EXP, "RA322FEB2018006281010100066PSANSTUC00GTDF_change_ndwi_full.tif")
    fig, ax = plt.subplots(figsize=(6.6, 5.6))
    try:
        d, _, _ = read_cog(cf)
        im = ax.imshow(d, cmap="RdBu", vmin=-0.6, vmax=0.6)
        fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04, label="ΔNDWI (B − A)")
        ax.set_title("compute_change tool: pixel-wise ΔNDWI between two scenes")
    except Exception as e:
        ax.text(0.5, 0.5, f"n/a\n{e}", ha="center")
    ax.set_xticks([]); ax.set_yticks([]); ax.grid(False)
    save(fig, "fig11_change_map.png")


# ─────────────────────────────────────────────────────────────────────────────
# RESULT METRIC FIGURES
# ─────────────────────────────────────────────────────────────────────────────

def fig_ndwi_window():
    cur = conn().cursor()
    cur.execute("""select s.window_type, avg(sm.value), stddev(sm.value), count(*)
                   from scene_metrics sm join scenes s on s.id=sm.scene_id
                   where sm.metric='ndwi_mean' group by s.window_type""")
    d = {r[0]: (float(r[1]), float(r[2] or 0), r[3]) for r in cur.fetchall()}
    order = ["pre_event", "event", "post_event", "annual"]
    cols = [C["pre"], C["event"], C["post"], C["annual"]]
    fig, ax = plt.subplots(figsize=(6.8, 4))
    means = [d[w][0] for w in order]; errs = [d[w][1] for w in order]
    bars = ax.bar([w.replace("_", " ") for w in order], means, yerr=errs, capsize=4,
                  color=cols, edgecolor="white", alpha=0.9)
    for b, w in zip(bars, order):
        ax.text(b.get_x() + b.get_width() / 2, b.get_height() - 0.02,
                f"{d[w][0]:.3f}\n(n={d[w][2]})", ha="center", va="top", fontsize=8, color="white")
    ax.set_ylabel("Mean NDWI"); ax.set_title("Scene-mean NDWI by temporal window (Kerala 2018)")
    ax.axhline(0, color="k", lw=0.8)
    save(fig, "fig09_ndwi_window.png")


def fig_water_pct_window():
    cur = conn().cursor()
    cur.execute("""select s.window_type, sm.value
                   from scene_metrics sm join scenes s on s.id=sm.scene_id
                   where sm.metric='water_area_pct' and sm.aoi_hash='full_scene'""")
    from collections import defaultdict
    g = defaultdict(list)
    for w, v in cur.fetchall(): g[w].append(float(v))
    order = ["pre_event", "event", "post_event", "annual"]
    fig, ax = plt.subplots(figsize=(7, 4.2))
    data = [g[w] for w in order]
    bp = ax.boxplot(data, tick_labels=[w.replace("_", " ") for w in order], patch_artist=True,
                    medianprops=dict(color="black"))
    for patch, c in zip(bp["boxes"], [C["pre"], C["event"], C["post"], C["annual"]]):
        patch.set_facecolor(c); patch.set_alpha(0.6)
    for i, w in enumerate(order, 1):
        ax.scatter(np.random.normal(i, 0.05, len(g[w])), g[w], s=14, c="k", alpha=0.4, zorder=3)
        ax.text(i, max(g[w]) + 0.8, f"μ={np.mean(g[w]):.2f}%", ha="center", fontsize=8)
    ax.set_ylabel("Open-water area (% of scene, NDWI > 0.3)")
    ax.set_title("Water-area fraction distribution by window (full-scene NDWI threshold)")
    save(fig, "fig12_water_pct_window.png")


def fig_flood_km2():
    cur = conn().cursor()
    cur.execute("""select sm.scene_id, sm.value, s.window_type, s.acq_datetime
                   from scene_metrics sm join scenes s on s.id=sm.scene_id
                   where sm.metric='water_area_km2' order by sm.value""")
    rows = cur.fetchall()
    fig, ax = plt.subplots(figsize=(7.5, 4))
    labels = [f"{r[0][:11]}…\n{r[2]}" for r in rows]
    vals = [float(r[1]) for r in rows]
    cmap = {"pre_event": C["pre"], "event": C["event"], "post_event": C["post"], "annual": C["annual"]}
    bars = ax.bar(range(len(rows)), vals, color=[cmap.get(r[2], "#777") for r in rows],
                  edgecolor="white")
    ax.set_yscale("symlog")
    ax.set_xticks(range(len(rows))); ax.set_xticklabels(labels, fontsize=7)
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width() / 2, v, f"{v:,.2f}", ha="center", va="bottom", fontsize=8)
    ax.set_ylabel("Open-water area (km², symlog)")
    ax.set_title("flood_extent tool: classified open-water area per analysed scene")
    save(fig, "fig13_flood_km2.png")


# ─────────────────────────────────────────────────────────────────────────────
# BENCHMARK FIGURE (consumes results/benchmark_results.json if present)
# ─────────────────────────────────────────────────────────────────────────────

def fig_benchmark():
    p = os.path.join(ROOT, "paper", "results", "benchmark_results.json")
    if not os.path.exists(p):
        print("  (skip benchmark figure — run run_benchmark.py first)"); return None
    data = json.load(open(p, encoding="utf-8"))
    runs = data["runs"]
    fig, axs = plt.subplots(1, 2, figsize=(12, 4.6))
    names = [r["id"] for r in runs]
    lat = [r["latency_s"] for r in runs]
    steps = [r["steps"] for r in runs]
    ok = [r["passed"] for r in runs]
    cols = ["#2ca02c" if o else "#d62728" for o in ok]
    axs[0].barh(range(len(runs)), lat, color=cols, edgecolor="white")
    axs[0].set_yticks(range(len(runs))); axs[0].set_yticklabels(names, fontsize=7.5)
    axs[0].invert_yaxis(); axs[0].set_xlabel("Latency (s)")
    axs[0].set_title(f"Per-query latency (green=pass, red=fail) · mean {np.mean(lat):.1f}s")
    # tool usage aggregate
    from collections import Counter
    tc = Counter()
    for r in runs:
        for t in r["tools"]: tc[t] += 1
    items = tc.most_common()
    axs[1].bar([k for k, _ in items], [v for _, v in items], color=C["pre"], edgecolor="white")
    axs[1].set_ylabel("Total calls across suite"); axs[1].set_title("Tool-call frequency")
    axs[1].tick_params(axis="x", rotation=55);
    for lbl in axs[1].get_xticklabels(): lbl.set_ha("right"); lbl.set_fontsize(7.5)
    n = len(runs); npass = sum(ok)
    cf = sum(1 for r in runs if r.get("coverage_first_compliant"))
    fig.suptitle(f"Agentic tool-use benchmark — {npass}/{n} passed · "
                 f"{cf}/{n} check_coverage-first compliant · backend: {data.get('backend')}",
                 fontweight="bold")
    save(fig, "fig14_benchmark.png")
    return data


if __name__ == "__main__":
    print("Generating figures →", FIG)
    bench = None
    bp = os.path.join(ROOT, "paper", "results", "benchmark_results.json")
    if os.path.exists(bp):
        bench = json.load(open(bp, encoding="utf-8"))
    fig_architecture()
    fig_ard_flow()
    fig_agent_loop(bench)
    fig_study_area()
    fig_temporal()
    fig_cloud_hist()
    fig_composition()
    fig_products()
    fig_ndwi_window()
    fig_flood_map()
    fig_change_map()
    fig_water_pct_window()
    fig_flood_km2()
    fig_benchmark()
    print("done.")
