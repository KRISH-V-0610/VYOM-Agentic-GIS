"""
Cloud-Optimized GeoTIFF writer (plan Phase 2, step 7).

rio-cogeo with 256x256 internal tiles and overviews 2/4/8/16. Float bands store
reflectance/index values with NaN nodata; the cloud mask is written as uint8.
"""

from pathlib import Path

import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rio_cogeo.cogeo import cog_translate
from rio_cogeo.profiles import cog_profiles

OVERVIEW_LEVELS = [2, 4, 8, 16]


def write_cog(
    array: np.ndarray,
    out_path: str | Path,
    *,
    crs,
    transform,
    dtype: str,
    nodata,
) -> Path:
    """Write a single-band array to a COG at out_path."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    arr = array.astype(dtype)
    src_profile = {
        "driver": "GTiff",
        "height": arr.shape[0],
        "width": arr.shape[1],
        "count": 1,
        "dtype": dtype,
        "crs": crs,
        "transform": transform,
        "nodata": nodata,
    }

    cfg = cog_profiles.get("deflate")
    cfg.update(blockxsize=256, blockysize=256)

    with MemoryFile() as mem:
        with mem.open(**src_profile) as src:
            src.write(arr, 1)
        cog_translate(
            mem,
            out_path,
            cfg,
            overview_level=len(OVERVIEW_LEVELS),
            overview_resampling="average",
            in_memory=True,
            quiet=True,
        )
    return out_path
