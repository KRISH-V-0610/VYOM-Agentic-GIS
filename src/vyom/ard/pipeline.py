"""
ARD preprocessing pipeline (plan Phase 2).

Manifest-driven (never walks folder names as source of truth). Per scene:

  unzip -> parse BAND_META -> resolve bands via band_registry -> calibrate each
  band (DN->TOA->DOS reflectance) -> cloud mask -> write band COGs -> compute
  indices (NDVI/NDWI always; NBR/MNDWI only if sensor has SWIR) -> write index
  COGs -> compute & cache metrics -> register scene row.

Idempotent: a scene already present at the current PIPELINE_VERSION is skipped.
Raw zips are never modified or deleted — ARD outputs are derived and rebuildable.

Usage:
  python -m vyom.ard.pipeline --event kerala_periyar_2018 --scene RA318NOV2017...
  python -m vyom.ard.pipeline --event kerala_periyar_2018 --window pre_event --limit 1
  python -m vyom.ard.pipeline --event kerala_periyar_2018 --all
"""

import argparse
import csv
import sys
import tempfile
import zipfile
from pathlib import Path

import numpy as np
import rasterio

from ..config import (
    DATA_ARD, DATA_MANIFESTS, PROJECT_ROOT, PIPELINE_VERSION,
    load_band_registry,
)
from ..ingest.band_meta import parse_band_meta
from . import calibrate, indices
from .cog import write_cog
from ..db import writer

# collection middle-token -> ESUN sensor family
_FAMILY = {"LISS3": "LISS3", "LISS4-MX70": "LISS4", "LISS4": "LISS4", "AWIFS": "AWIFS"}

FULL_SCENE_AOI_HASH = "full_scene"  # sentinel: metric over whole scene (no AOI clip)


def sensor_family(collection: str) -> str:
    """'ResourceSat-2A_LISS3_L2' -> 'LISS3'."""
    token = collection.split("_")[1]
    return _FAMILY.get(token, token)


def short_sensor(collection: str) -> str:
    """Human sensor label for the scenes.sensor column."""
    return collection.split("_")[1].split("-")[0]


def read_manifest(event_key: str) -> list[dict]:
    path = DATA_MANIFESTS / f"{event_key}.csv"
    if not path.exists():
        sys.exit(f"ERROR: manifest not found: {path}")
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _find_scene_dir(extract_root: Path) -> Path:
    """Zip extracts to a single scene subdir containing BAND*.tif + BAND_META.txt."""
    if (extract_root / "BAND_META.txt").exists():
        return extract_root
    for child in extract_root.iterdir():
        if child.is_dir() and (child / "BAND_META.txt").exists():
            return child
    raise FileNotFoundError(f"No BAND_META.txt found under {extract_root}")


def process_scene(row: dict, *, force: bool = False, apply_dos: bool = True) -> str:
    """Process a single manifest row. Returns a status string."""
    scene_id = row["scene_id"]
    collection = row["collection"]
    zip_rel = row["relative_filepath"]
    zip_path = PROJECT_ROOT / zip_rel

    if not zip_path.exists():
        return f"SKIP (zip missing): {scene_id}"

    if not force and writer.scene_exists(scene_id, PIPELINE_VERSION):
        return f"SKIP (already ingested @ {PIPELINE_VERSION}): {scene_id}"

    registry = load_band_registry()
    if collection not in registry:
        return f"SKIP (unknown collection {collection}): {scene_id}"
    sensor_cfg = registry[collection]
    family = sensor_family(collection)
    band_map = sensor_cfg["bands"]          # {'green':'B2', ...}
    has_swir = sensor_cfg["has_swir"]
    gsd_m = sensor_cfg["gsd_m"]

    with tempfile.TemporaryDirectory(prefix="vyom_ard_") as tmp:
        tmp = Path(tmp)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp)
        scene_dir = _find_scene_dir(tmp)

        meta = parse_band_meta(scene_dir / "BAND_META.txt")
        doy = int(meta["acq_datetime"].strftime("%j"))
        sun_elev = meta["sun_elevation"]
        dn_max = meta["dn_max"]
        year = meta["acq_datetime"].strftime("%Y")

        out_dir = DATA_ARD / collection / year / scene_id
        out_dir.mkdir(parents=True, exist_ok=True)

        # ── Calibrate each canonical band -> reflectance COG ────────────────────
        refl: dict[str, np.ndarray] = {}
        crs = transform = None
        assets: dict[str, str] = {}

        for canonical, phys in band_map.items():
            band_tif = scene_dir / f"BAND{phys[1:]}.tif"  # 'B2' -> BAND2.tif
            if not band_tif.exists():
                print(f"  WARN: {canonical} ({phys}) tif missing — skipping band")
                continue
            with rasterio.open(band_tif) as ds:
                dn = ds.read(1)
                crs, transform = ds.crs, ds.transform

            arr = calibrate.calibrate_band(
                dn,
                canonical_band=canonical,
                sensor_family=family,
                lmin=meta["lmin"].get(phys, 0.0),
                lmax=meta["lmax"][phys],
                dn_max=dn_max,
                sun_elevation=sun_elev,
                doy=doy,
                apply_dos=apply_dos,
            )
            refl[canonical] = arr
            out = out_dir / f"{canonical}.tif"
            write_cog(arr, out, crs=crs, transform=transform, dtype="float32", nodata=np.nan)
            assets[canonical] = str(out.relative_to(PROJECT_ROOT)).replace("\\", "/")

        if "green" not in refl or "nir" not in refl:
            return f"FAIL (missing green/nir): {scene_id}"

        valid = np.isfinite(refl["nir"])

        # ── Cloud mask ──────────────────────────────────────────────────────────
        cmask = indices.cloud_mask(refl["green"], refl["nir"])
        cloud_pct = indices.cloud_fraction(cmask, valid)
        cmask_path = out_dir / "cloud_mask.tif"
        write_cog(cmask, cmask_path, crs=crs, transform=transform, dtype="uint8", nodata=0)
        assets["cloud_mask"] = str(cmask_path.relative_to(PROJECT_ROOT)).replace("\\", "/")

        # ── Indices ───────────────────────────────────────────────────────────
        index_arrays: dict[str, np.ndarray] = {
            "ndvi": indices.ndvi(refl["red"], refl["nir"]) if "red" in refl else None,
            "ndwi": indices.ndwi(refl["green"], refl["nir"]),
        }
        if has_swir and "swir1" in refl:
            index_arrays["nbr"] = indices.nbr(refl["nir"], refl["swir1"])
            index_arrays["mndwi"] = indices.mndwi(refl["green"], refl["swir1"])
        else:
            print(f"  NOTE: {scene_id} has no SWIR — skipping NBR/MNDWI (LISS4)")

        metrics: dict[str, float] = {}
        for name, arr in index_arrays.items():
            if arr is None:
                continue
            ipath = out_dir / f"{name}.tif"
            write_cog(arr, ipath, crs=crs, transform=transform, dtype="float32", nodata=np.nan)
            assets[name] = str(ipath.relative_to(PROJECT_ROOT)).replace("\\", "/")
            metrics.update(indices.index_metrics(name, arr))

        metrics["cloud_cover_pct"] = cloud_pct

        # ── Register scene + cache metrics ──────────────────────────────────────
        properties = {
            "dos_uncertainty": "+/-5-10% reflectance" if apply_dos else "TOA (no DOS)",
            "sun_elevation": sun_elev,
            "sun_azimuth": meta["sun_azimuth"],
            "utm_zone": meta["utm_zone"],
            "bits_per_pixel": meta["bits_per_pixel"],
            "has_swir": has_swir,
            "nbr_available": bool(has_swir and "swir1" in refl),
            "primary_index": row.get("primary_index"),
        }
        scene_row = {
            "id": scene_id,
            "collection": collection,
            "satellite": row.get("satellite") or meta["satellite"],
            "sensor": short_sensor(collection),
            "footprint_wkt": meta["footprint_wkt"],
            "acq_datetime": meta["acq_datetime"],
            "cloud_cover": cloud_pct,
            "gsd_m": gsd_m,
            "processing_level": "ARD",
            "event_key": row.get("event_key"),
            "window_type": row.get("window"),
            "properties": properties,
            "assets": assets,
            "pipeline_version": PIPELINE_VERSION,
        }
        writer.register_scene(scene_row)
        n = writer.cache_metrics(scene_id, FULL_SCENE_AOI_HASH, metrics, PIPELINE_VERSION)

    return f"OK: {scene_id}  ({len(assets)} assets, {n} metrics, cloud {cloud_pct}%)"


def main():
    ap = argparse.ArgumentParser(description="VYOM ARD preprocessing pipeline")
    ap.add_argument("--event", required=True, help="event_key (matches manifest filename)")
    ap.add_argument("--scene", help="process a single scene_id")
    ap.add_argument("--window", help="filter by window (pre_event/event/post_event/annual)")
    ap.add_argument("--limit", type=int, help="max scenes to process")
    ap.add_argument("--all", action="store_true", help="process all matching scenes")
    ap.add_argument("--force", action="store_true", help="reprocess even if already ingested")
    ap.add_argument("--no-dos", action="store_true", help="skip DOS (TOA reflectance only)")
    args = ap.parse_args()

    rows = read_manifest(args.event)
    if args.scene:
        rows = [r for r in rows if r["scene_id"] == args.scene]
    if args.window:
        rows = [r for r in rows if r["window"] == args.window]
    if not (args.all or args.scene):
        if args.limit is None:
            args.limit = 1  # safe default: one scene
    if args.limit:
        rows = rows[: args.limit]

    if not rows:
        sys.exit("No matching scenes in manifest.")

    print(f"Processing {len(rows)} scene(s) from {args.event} @ {PIPELINE_VERSION}\n",
          flush=True)
    for r in rows:
        print(process_scene(r, force=args.force, apply_dos=not args.no_dos), flush=True)


if __name__ == "__main__":
    main()
