"""
GIS MCP server — the heavy pixel-work half of VYOM's tool layer.

Heavy imports (rasterio, numpy, shapely, matplotlib) are explicitly allowed here.
This server handles on-the-fly raster analysis that the catalog-only
postgis_server.py deliberately avoids.

Tools are defined as module-level functions then registered with mcp.tool()() —
identical pattern to postgis_server.py — so they remain unit-testable without a
running server:

    from vyom.db.gis_server import flood_extent
    result = flood_extent("RA318NOV2017...")

Cold-start time is NOT a constraint here (heavy libs accepted).
Separate FastMCP instance named "vyom-gis".
"""

import json
import os
import sys
from pathlib import Path


def _fix_proj_data_path() -> None:
    """Force PROJ to use the venv's own proj.db, not PostgreSQL's old one.

    The PostgreSQL/PostGIS installer sets a system-wide ``PROJ_LIB`` pointing at its
    bundled PROJ (``...postgis-3.6\\proj``), whose ``proj.db`` uses an old database
    layout (VERSION.MINOR=2). rasterio/pyproj need layout >= 6, so any CRS lookup
    (e.g. in export_png / flood_extent) raises ``CRSError``. We override the env var
    to point at the correct bundled database BEFORE rasterio imports GDAL/PROJ.
    """
    candidates = [
        Path(sys.prefix) / "Lib" / "site-packages" / "rasterio" / "proj_data",
        Path(sys.prefix) / "Lib" / "site-packages" / "pyproj" / "proj_dir" / "share" / "proj",
    ]
    for path in candidates:
        if (path / "proj.db").exists():
            os.environ["PROJ_LIB"] = str(path)
            os.environ["PROJ_DATA"] = str(path)
            return


_fix_proj_data_path()

import numpy as np
import psycopg2
import rasterio
from rasterio.enums import ColorInterp
from rasterio.mask import mask as rio_mask
from rasterio.transform import array_bounds
from rasterio.warp import transform_bounds
from rasterio.warp import transform_geom as _warp_geom
from shapely.geometry import shape
from fastmcp import FastMCP

from ..config import get_db_url, PROJECT_ROOT

PIPELINE_VERSION = "v1.0-dos"
NDWI_FLOOD_THRESHOLD = 0.3  # McFeeters, water-positive

mcp = FastMCP("vyom-gis")


# ── DB helpers ────────────────────────────────────────────────────────────────


def _connect():
    return psycopg2.connect(get_db_url())


def _get_scene_row(scene_id: str) -> tuple[dict, float]:
    """Return (assets dict, gsd_m) for a scene. Raises ValueError if not found."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT assets, gsd_m FROM scenes WHERE id = %s", (scene_id,))
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"Scene not found in catalog: {scene_id!r}")
    assets, gsd_m = row
    return (assets or {}), gsd_m


def _resolve_asset(assets: dict, key: str, scene_id: str) -> Path:
    """Resolve a relative asset path to an absolute Path, asserting it exists."""
    rel = assets.get(key)
    if not rel:
        raise ValueError(
            f"Asset {key!r} not found for scene {scene_id!r}. "
            f"Available: {sorted(assets.keys())}"
        )
    path = PROJECT_ROOT / rel
    if not path.exists():
        raise FileNotFoundError(f"Asset file missing on disk: {path}")
    return path


def _aoi_hash(geom_geojson: dict) -> str:
    """MD5(PostGIS WKT of AOI) — matches the hash definition in postgis_server + DB schema."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT md5(ST_AsText(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)))",
            (json.dumps(geom_geojson),),
        )
        return cur.fetchone()[0]


def _cache_metrics(scene_id: str, aoi_hash: str, metrics: dict) -> None:
    """Write computed metrics to scene_metrics (mirrors db/writer.cache_metrics)."""
    if not metrics:
        return
    with _connect() as conn, conn.cursor() as cur:
        for metric, value in metrics.items():
            cur.execute(
                """
                INSERT INTO scene_metrics
                    (scene_id, aoi_hash, metric, value, pipeline_version)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (scene_id, aoi_hash, metric, pipeline_version)
                DO UPDATE SET value = EXCLUDED.value, computed_at = now()
                """,
                (scene_id, aoi_hash, metric, float(value), PIPELINE_VERSION),
            )
        conn.commit()


# ── GeoJSON / raster helpers ──────────────────────────────────────────────────


def _to_geometry(aoi_geojson: dict) -> dict:
    """Normalise a GeoJSON input to a bare geometry dict."""
    if not isinstance(aoi_geojson, dict):
        raise ValueError("aoi_geojson must be a GeoJSON dict")
    gtype = aoi_geojson.get("type")
    if gtype == "FeatureCollection":
        features = aoi_geojson.get("features") or []
        if not features:
            raise ValueError("FeatureCollection has no features")
        return _to_geometry(features[0])
    if gtype == "Feature":
        geom = aoi_geojson.get("geometry")
        if not geom:
            raise ValueError("Feature has no geometry")
        return geom
    if gtype in ("Polygon", "MultiPolygon", "Point", "LineString",
                 "MultiPoint", "MultiLineString", "GeometryCollection"):
        return aoi_geojson
    raise ValueError(f"Unsupported GeoJSON type: {gtype!r}")


def _read_clipped(tif_path: Path, geom_wgs84: dict):
    """Open a COG and clip to a WGS84 GeoJSON geometry (auto-reprojected to dataset CRS).

    Returns (array float32, out_transform, crs). Outside-AOI and nodata pixels are NaN.
    """
    with rasterio.open(tif_path) as ds:
        if ds.crs and ds.crs.to_epsg() != 4326:
            geom_proj = _warp_geom("EPSG:4326", ds.crs, geom_wgs84)
        else:
            geom_proj = geom_wgs84
        out_arr, out_transform = rio_mask(
            ds, [shape(geom_proj)], crop=True, nodata=np.nan, filled=True
        )
        return out_arr[0].astype("float32"), out_transform, ds.crs


def _read_full(tif_path: Path):
    """Open a COG and read the full band as float32. Returns (array, transform, crs)."""
    with rasterio.open(tif_path) as ds:
        arr = ds.read(1).astype("float32")
        # Float32 COGs from this pipeline store nodata as NaN already.
        return arr, ds.transform, ds.crs


def _array_stats(arr: np.ndarray, name: str) -> dict:
    """NaN-safe summary stats for one array. Keys: <name>_{mean,std,min,max}, valid_pixels."""
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return {
            f"{name}_mean": None, f"{name}_std": None,
            f"{name}_min": None, f"{name}_max": None,
            "valid_pixels": 0,
        }
    return {
        f"{name}_mean": round(float(np.mean(finite)), 6),
        f"{name}_std": round(float(np.std(finite)), 6),
        f"{name}_min": round(float(np.min(finite)), 6),
        f"{name}_max": round(float(np.max(finite)), 6),
        "valid_pixels": int(finite.size),
    }


# ── Georeferencing helpers ────────────────────────────────────────────────────


def _crs_str(crs) -> str | None:
    """Best-effort CRS string: 'EPSG:<code>' when available, else WKT/PROJ string."""
    if not crs:
        return None
    code = crs.to_epsg()
    return f"EPSG:{code}" if code else crs.to_string()


def _wgs84_bounds(transform, crs, h: int, w: int) -> list | None:
    """Return [minlon, minlat, maxlon, maxlat] in EPSG:4326 for a raster grid.

    Densifies the reprojected box (UTM rectangle ≠ lon/lat rectangle). Returns None
    if no CRS is known.
    """
    if transform is None:
        return None
    left, bottom, right, top = array_bounds(h, w, transform)
    if crs is None:
        return [left, bottom, right, top]
    if crs.to_epsg() == 4326:
        return [left, bottom, right, top]
    minlon, minlat, maxlon, maxlat = transform_bounds(
        crs, "EPSG:4326", left, bottom, right, top, densify_pts=21
    )
    return [minlon, minlat, maxlon, maxlat]


def _write_geotiff(out_path, array, transform, crs, *, dtype, nodata=None,
                   count=1, colormap=None, colorinterp=None) -> None:
    """Write a plain (non-COG) GeoTIFF from an in-memory array + affine transform.

    Small AOI-clipped products — no tiling/overviews needed. `array` is (H, W) for
    count=1 or (count, H, W) for multiband.
    """
    if count == 1:
        h, w = array.shape
        bands = array[np.newaxis, :, :]
    else:
        bands = array
        _, h, w = bands.shape

    profile = {
        "driver": "GTiff",
        "height": h,
        "width": w,
        "count": count,
        "dtype": dtype,
        "transform": transform,
        "crs": crs,
        "compress": "deflate",
    }
    if nodata is not None:
        profile["nodata"] = nodata

    with rasterio.open(out_path, "w", **profile) as ds:
        ds.write(bands.astype(dtype))
        if colormap is not None:
            ds.write_colormap(1, colormap)
        if colorinterp is not None:
            ds.colorinterp = colorinterp


# ── TOOLS ────────────────────────────────────────────────────────────────────────


def compute_change(
    scene_id_a: str,
    scene_id_b: str,
    index: str,
    aoi_geojson: dict = None,
) -> dict:
    """Pixel-wise index change between two scenes: delta = scene_B − scene_A.

    Scene A is the baseline (e.g. pre-event), scene B is the comparison (e.g. event).
    Both scenes must have the requested index as an ARD asset. For NDWI: positive delta
    means more water (flood signal). For NDVI: negative delta means vegetation loss.
    For NBR: negative delta indicates burn severity.

    Args:
        scene_id_a: baseline scene (scene A — typically pre_event).
        scene_id_b: comparison scene (scene B — typically event or post_event).
        index: index name — one of 'ndvi', 'ndwi', 'nbr', 'mndwi'.
        aoi_geojson: optional AOI geometry/Feature/FeatureCollection (WGS84).
            If omitted, uses full-scene arrays (scenes must overlap spatially).

    Returns:
        {
          "scene_a": str, "scene_b": str, "index": str, "aoi_hash": str,
          "mean_change": float,      # B.mean − A.mean (signed)
          "std_change": float,       # standard deviation of pixel deltas
          "pct_increased": float,    # % valid pixels where B > A by > 0.05
          "pct_decreased": float,    # % valid pixels where B < A by > 0.05
          "pct_unchanged": float,    # % valid pixels within ±0.05
          "valid_pixel_pairs": int,
          "stats_a": {<index>_mean, std, min, max, valid_pixels},
          "stats_b": {<index>_mean, std, min, max, valid_pixels}
        }
    """
    assets_a, _ = _get_scene_row(scene_id_a)
    assets_b, _ = _get_scene_row(scene_id_b)
    path_a = _resolve_asset(assets_a, index, scene_id_a)
    path_b = _resolve_asset(assets_b, index, scene_id_b)

    if aoi_geojson is not None:
        geom = _to_geometry(aoi_geojson)
        aoi_h = _aoi_hash(geom)
        arr_a, _, _ = _read_clipped(path_a, geom)
        arr_b, transform_b, crs_b = _read_clipped(path_b, geom)
    else:
        aoi_h = "full_scene"
        arr_a, _, _ = _read_full(path_a)
        arr_b, transform_b, crs_b = _read_full(path_b)

    # Trim to shared extent if same-event scenes have tiny shape differences
    min_rows = min(arr_a.shape[0], arr_b.shape[0])
    min_cols = min(arr_a.shape[1], arr_b.shape[1])
    arr_a = arr_a[:min_rows, :min_cols]
    arr_b = arr_b[:min_rows, :min_cols]

    valid = np.isfinite(arr_a) & np.isfinite(arr_b)
    n_valid = int(valid.sum())
    if n_valid == 0:
        return {
            "scene_a": scene_id_a, "scene_b": scene_id_b,
            "index": index, "aoi_hash": aoi_h,
            "error": "No overlapping valid pixels found between the two scenes.",
        }

    delta = (arr_b - arr_a)[valid]
    n = float(n_valid)
    pct_inc = round(float((delta > 0.05).sum()) / n * 100, 2)
    pct_dec = round(float((delta < -0.05).sum()) / n * 100, 2)

    # ── Georeferenced delta GeoTIFF (B's grid, NaN where either input invalid) ──
    geotiff_path = crs_out = bounds_wgs84 = None
    try:
        delta_grid = np.where(valid, arr_b - arr_a, np.nan).astype("float32")
        out_dir = PROJECT_ROOT / "data" / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        slug = aoi_h[:8] if aoi_h != "full_scene" else "full"
        tif_path = out_dir / f"{scene_id_b}_change_{index}_{slug}.tif"
        # array_bounds/transform must match the (possibly trimmed) delta grid.
        _write_geotiff(tif_path, delta_grid, transform_b, crs_b,
                       dtype="float32", nodata=float("nan"), count=1)
        geotiff_path = str(tif_path)
        crs_out = _crs_str(crs_b)
        bounds_wgs84 = _wgs84_bounds(transform_b, crs_b, min_rows, min_cols)
    except Exception:  # georeferencing is additive — never fail the analysis on it
        geotiff_path = crs_out = bounds_wgs84 = None

    return {
        "scene_a": scene_id_a,
        "scene_b": scene_id_b,
        "index": index,
        "aoi_hash": aoi_h,
        "mean_change": round(float(np.mean(delta)), 6),
        "std_change": round(float(np.std(delta)), 6),
        "pct_increased": pct_inc,
        "pct_decreased": pct_dec,
        "pct_unchanged": round(100.0 - pct_inc - pct_dec, 2),
        "valid_pixel_pairs": n_valid,
        "stats_a": _array_stats(arr_a, index),
        "stats_b": _array_stats(arr_b, index),
        "geotiff_path": geotiff_path,
        "crs": crs_out,
        "bounds_wgs84": bounds_wgs84,
    }


def clip_to_aoi(
    scene_id: str,
    asset: str,
    aoi_geojson: dict,
) -> dict:
    """Clip a scene's COG asset to an AOI and compute summary statistics.

    Results are cached in scene_metrics so postgis_server.get_scene_metrics()
    can serve them without pixel work on subsequent calls.

    Args:
        scene_id: scene to read.
        asset: asset name — one of 'ndwi', 'ndvi', 'nbr', 'mndwi',
               'green', 'red', 'nir', 'swir1', 'cloud_mask'.
        aoi_geojson: AOI geometry/Feature/FeatureCollection (WGS84).

    Returns:
        {
          "scene_id": str, "asset": str, "aoi_hash": str,
          "stats": {<asset>_mean, <asset>_std, <asset>_min, <asset>_max, valid_pixels},
          "cached": bool
        }
    """
    geom = _to_geometry(aoi_geojson)
    aoi_h = _aoi_hash(geom)

    assets, _ = _get_scene_row(scene_id)
    tif_path = _resolve_asset(assets, asset, scene_id)

    arr, _, _ = _read_clipped(tif_path, geom)
    stats = _array_stats(arr, asset)

    cacheable = {k: v for k, v in stats.items()
                 if v is not None and k != "valid_pixels"}
    _cache_metrics(scene_id, aoi_h, cacheable)

    return {
        "scene_id": scene_id,
        "asset": asset,
        "aoi_hash": aoi_h,
        "stats": stats,
        "cached": bool(cacheable),
    }


def flood_extent(
    scene_id: str,
    aoi_geojson: dict = None,
    threshold: float = NDWI_FLOOD_THRESHOLD,
) -> dict:
    """Binary flood map from NDWI threshold — the primary flood analysis tool.

    Reads the NDWI COG for a scene, applies the threshold (NDWI > threshold = water),
    and returns pixel counts + estimated surface area. Results are cached in
    scene_metrics so subsequent catalog-layer queries return them without pixel work.

    Args:
        scene_id: scene to analyse.
        aoi_geojson: optional AOI geometry/Feature/FeatureCollection (WGS84).
            If omitted, analyses the full scene extent.
        threshold: NDWI water threshold. Default 0.3 (McFeeters convention, water-positive).

    Returns:
        {
          "scene_id": str, "aoi_hash": str, "threshold": float,
          "water_pixels": int, "valid_pixels": int,
          "water_area_pct": float,
          "gsd_m": float,
          "estimated_water_area_km2": float or null
        }
    """
    assets, gsd_m = _get_scene_row(scene_id)
    tif_path = _resolve_asset(assets, "ndwi", scene_id)

    if aoi_geojson is not None:
        geom = _to_geometry(aoi_geojson)
        aoi_h = _aoi_hash(geom)
        arr, transform, crs = _read_clipped(tif_path, geom)
    else:
        aoi_h = "full_scene"
        arr, transform, crs = _read_full(tif_path)

    valid_pixels = int(np.isfinite(arr).sum())
    water_pixels = int((arr > threshold).sum())
    water_pct = round(water_pixels / valid_pixels * 100, 3) if valid_pixels > 0 else 0.0

    area_km2 = None
    if gsd_m and valid_pixels > 0:
        area_km2 = round(water_pixels * (gsd_m / 1000.0) ** 2, 4)

    metrics = {"water_area_pct": water_pct, "water_pixel_count": float(water_pixels)}
    if area_km2 is not None:
        metrics["water_area_km2"] = area_km2
    _cache_metrics(scene_id, aoi_h, metrics)

    # ── Georeferenced binary flood mask (1=water, 0=dry, 255=nodata) ──
    geotiff_path = crs_out = bounds_wgs84 = None
    try:
        mask = np.where(np.isfinite(arr), (arr > threshold).astype("uint8"), 255)
        out_dir = PROJECT_ROOT / "data" / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        slug = aoi_h[:8] if aoi_h != "full_scene" else "full"
        tif_path_out = out_dir / f"{scene_id}_flood_{slug}.tif"
        _write_geotiff(
            tif_path_out, mask, transform, crs,
            dtype="uint8", nodata=255, count=1,
            colormap={0: (0, 0, 0, 0), 1: (0, 90, 200, 255), 255: (0, 0, 0, 0)},
        )
        geotiff_path = str(tif_path_out)
        crs_out = _crs_str(crs)
        bounds_wgs84 = _wgs84_bounds(transform, crs, arr.shape[0], arr.shape[1])
    except Exception:
        geotiff_path = crs_out = bounds_wgs84 = None

    return {
        "scene_id": scene_id,
        "aoi_hash": aoi_h,
        "threshold": threshold,
        "water_pixels": water_pixels,
        "valid_pixels": valid_pixels,
        "water_area_pct": water_pct,
        "gsd_m": gsd_m,
        "estimated_water_area_km2": area_km2,
        "geotiff_path": geotiff_path,
        "crs": crs_out,
        "bounds_wgs84": bounds_wgs84,
    }


def export_png(
    scene_id: str,
    product: str = "false_color",
    aoi_geojson: dict = None,
    out_dir: str = None,
) -> dict:
    """Render a scene product as a PNG thumbnail.

    For false-colour composites, NIR/Red/Green are stretched to 2–98th percentile
    and composed as an RGB image. For index products, a diverging colormap is applied
    with fixed ±1.0 bounds.

    Args:
        scene_id: scene to render.
        product: 'false_color' (NIR-Red-Green composite), 'ndvi', 'ndwi', 'nbr',
                 or 'mndwi'.
        aoi_geojson: optional AOI to clip before rendering.
        out_dir: output directory (default: <project_root>/data/exports/).

    Returns:
        {
          "output_path": str,   # absolute path to the saved PNG
          "scene_id": str,
          "product": str,
          "aoi_hash": str,
          "width_px": int,
          "height_px": int
        }
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    assets, _ = _get_scene_row(scene_id)

    out_path = Path(out_dir) if out_dir else PROJECT_ROOT / "data" / "exports"
    out_path.mkdir(parents=True, exist_ok=True)

    aoi_h = "full_scene"
    geom = None
    if aoi_geojson is not None:
        geom = _to_geometry(aoi_geojson)
        aoi_h = _aoi_hash(geom)

    grid = {"transform": None, "crs": None}  # captured from the first asset read

    def _load(key):
        p = _resolve_asset(assets, key, scene_id)
        if geom:
            arr, transform, crs = _read_clipped(p, geom)
        else:
            arr, transform, crs = _read_full(p)
        grid["transform"], grid["crs"] = transform, crs
        return arr

    def _pct_stretch(arr):
        finite = arr[np.isfinite(arr)]
        if finite.size == 0:
            return np.zeros_like(arr)
        lo, hi = np.percentile(finite, 2), np.percentile(finite, 98)
        out = np.clip(arr, lo, hi)
        out[~np.isfinite(arr)] = lo
        denom = max(hi - lo, 1e-6)
        return (out - lo) / denom

    fig, ax = plt.subplots(figsize=(8, 8), dpi=150)
    ax.axis("off")

    # geotiff_data: (count, dtype, nodata, colorinterp, array) staged for _write_geotiff
    geotiff_array = None
    geotiff_kwargs = None

    if product == "false_color":
        nir = _load("nir")
        red = _load("red")
        green = _load("green")

        rgb = np.dstack([_pct_stretch(nir), _pct_stretch(red), _pct_stretch(green)])
        ax.imshow(rgb)
        ax.set_title(f"{scene_id} — False Colour (NIR/R/G)", fontsize=9)

        # 3-band uint8 GeoTIFF (bands = R:NIR, G:Red, B:Green to match the composite)
        bands = (np.stack([rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]]) * 255).astype("uint8")
        geotiff_array = bands
        geotiff_kwargs = dict(
            dtype="uint8", count=3,
            colorinterp=[ColorInterp.red, ColorInterp.green, ColorInterp.blue],
        )
    else:
        _cmaps = {
            "ndwi":  ("RdYlBu",  -1.0, 1.0),
            "ndvi":  ("RdYlGn",  -1.0, 1.0),
            "nbr":   ("RdYlGn",  -1.0, 1.0),
            "mndwi": ("RdYlBu",  -1.0, 1.0),
        }
        cmap_name, vmin, vmax = _cmaps.get(product, ("viridis", -1.0, 1.0))
        arr = _load(product)
        im = ax.imshow(arr, cmap=cmap_name, vmin=vmin, vmax=vmax)
        plt.colorbar(im, ax=ax, fraction=0.03, pad=0.02)
        ax.set_title(f"{scene_id} — {product.upper()}", fontsize=9)

        geotiff_array = arr.astype("float32")
        geotiff_kwargs = dict(dtype="float32", count=1, nodata=float("nan"))

    slug = aoi_h[:8] if aoi_h != "full_scene" else "full"
    png_path = out_path / f"{scene_id}_{product}_{slug}.png"
    plt.tight_layout()
    plt.savefig(png_path, bbox_inches="tight", dpi=150)
    plt.close(fig)

    # Read actual pixel dimensions from the saved file (rasterio supports PNG)
    with rasterio.open(png_path) as ds:
        width_px, height_px = ds.width, ds.height

    # ── Georeferenced GeoTIFF from the raw data array (NOT the matplotlib PNG) ──
    geotiff_path = crs_out = bounds_wgs84 = None
    try:
        transform, crs = grid["transform"], grid["crs"]
        if transform is not None and geotiff_array is not None:
            tif_path = out_path / f"{scene_id}_{product}_{slug}.tif"
            _write_geotiff(tif_path, geotiff_array, transform, crs, **geotiff_kwargs)
            geotiff_path = str(tif_path)
            crs_out = _crs_str(crs)
            h = geotiff_array.shape[-2]
            w = geotiff_array.shape[-1]
            bounds_wgs84 = _wgs84_bounds(transform, crs, h, w)
    except Exception:
        geotiff_path = crs_out = bounds_wgs84 = None

    return {
        "output_path": str(png_path),
        "scene_id": scene_id,
        "product": product,
        "aoi_hash": aoi_h,
        "width_px": width_px,
        "height_px": height_px,
        "geotiff_path": geotiff_path,
        "crs": crs_out,
        "bounds_wgs84": bounds_wgs84,
    }


# Register tools — same pattern as postgis_server.py
mcp.tool()(compute_change)
mcp.tool()(clip_to_aoi)
mcp.tool()(flood_extent)
mcp.tool()(export_png)


if __name__ == "__main__":
    mcp.run()
