"""
download_data.py — Bhoonidhi multi-hazard event downloader for the Agentic EO platform
======================================================================================

Downloads ISRO satellite scenes for a curated multi-hazard benchmark of Indian
events through the Bhoonidhi STAC API. The manifests it writes are the *contract*
the future ingestion script reads, so the ingestion side never parses folder names.

EVENT TYPES IN THE BENCHMARK
----------------------------
  flood      — NDWI / MNDWI         (water; SAR penetrates monsoon cloud)
  wildfire   — NBR / dNBR           (burn scars; needs SWIR -> LISS-3 B5, AWiFS)
  landslide  — dNDVI / bare-soil    (debris/GLOF scars; high-res LISS-4 + SAR)
  drought    — NDVI -> VCI / VHI    (slow, multi-year; AWiFS wide swath)
  cyclone    — dNDVI / MNDWI / BSI  (wind + surge damage; optical + SAR fusion)

DESIGN PRINCIPLES
-----------------
1. Registry-style event catalog. Events live in the EVENTS dict, tagged by tier
   AND event_type. Adding an event = add one entry. Nothing else changes.
2. Relative paths in the manifest. Paths are stored relative to the project root,
   so moving the project or switching machines does not break anything. Resolution
   is `PROJECT_ROOT / relative_filepath`.
3. raw/ is an archive. This script ONLY writes to data/raw/. The ARD pipeline
   (separate, later) reads raw/ and writes data/ard/. raw/ is never mutated.
4. Satellite-epoch guarding. Each sensor knows its satellite's launch date. A
   window that predates the satellite is skipped automatically — so EOS-04 SAR is
   never searched for a 2016 event, and ResourceSat-2A is never searched for a
   2013/2015 event. This is why the wildfire (2016) and drought (2015-16) events
   are configured on ResourceSat-2, not 2A.
5. BOA-first fallback. For optical ResourceSat sensors the script tries the
   surface-reflectance (_BOA) collection first; if the archive has it that is one
   fewer correction step for the ARD pipeline. If _BOA is empty for a window it
   falls back to the radiance (_L2) collection automatically.

USAGE
-----
  # Inspect the catalog without touching the network
  python download_data.py --list

  # Always preview first (verifies collection strings + shows real coverage)
  python download_data.py --tier 1 --search-only

  # Download by tier
  python download_data.py --tier 1
  python download_data.py --tier 1 2 3

  # Download by hazard type (across tiers)
  python download_data.py --type wildfire
  python download_data.py --type flood drought

  # Combine: tier-1 drought events only (intersection when both given)
  python download_data.py --tier 1 --type drought

  # Single event
  python download_data.py --event wildfire_uttarakhand_2016
  python download_data.py --event landslide_sikkim_glof_2023 --search-only

CREDENTIALS
-----------
Put your Bhoonidhi login in a .env file next to this script:
  BHOONIDHI_USER=your_user
  BHOONIDHI_PASS=your_pass
(The script creates a template on first run if .env is missing.)

NOTE ON COLLECTION STRINGS
--------------------------
Collection strings are taken verbatim from the live API spec at
https://bhoonidhi.nrsc.gov.in/bhoonidhi-api/ (Collections section). Still, run
--search-only on a known event before a full download: archive depth varies by
sensor and period, and a valid string may simply have no scenes for a given window.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Missing dependency 'requests'. Install with:  pip install requests")

try:
    from tqdm import tqdm
except ImportError:  # progress bar is optional
    def tqdm(iterable=None, total=None, **kwargs):
        return iterable if iterable is not None else range(total or 0)

import warnings
warnings.filterwarnings("ignore", message="Unverified HTTPS request")


# ============================================================================
# PATHS & API CONSTANTS
# ============================================================================
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_RAW     = PROJECT_ROOT / "data" / "raw"
MANIFEST_DIR = PROJECT_ROOT / "data" / "manifests"
ENV_FILE     = PROJECT_ROOT / ".env"

BHOONIDHI_BASE = "https://bhoonidhi-api.nrsc.gov.in"
AUTH_URL       = f"{BHOONIDHI_BASE}/auth/token"
SEARCH_URL     = f"{BHOONIDHI_BASE}/data/search"
DOWNLOAD_URL   = f"{BHOONIDHI_BASE}/download"

# NRSC's cert chain sometimes fails verification on certain networks; the prior
# project ran with verification off. Default off, flip on with --verify-ssl.
VERIFY_SSL_DEFAULT = False

CHUNK_DAYS      = 180   # split long date ranges so each search stays small
SEARCH_LIMIT    = 500   # max features per chunk
SEARCH_PAUSE_S  = 0.4   # polite delay between search calls
MAX_CONCURRENT  = 3     # parallel downloads
APPROX_GB_SCENE = 0.25  # rough size estimate for the summary only


# ============================================================================
# SATELLITE LAUNCH EPOCHS  (used to auto-skip impossible windows)
# ============================================================================
SATELLITE_EPOCHS = {
    "ResourceSat-2":  date(2011, 4, 20),
    "ResourceSat-2A": date(2016, 12, 7),
    "EOS-04":         date(2022, 2, 15),
    "Sentinel-1A":    date(2014, 4, 3),
}


# ============================================================================
# SENSOR BUILDERS  (keep the event catalog readable)
# ============================================================================
def rs(key: str, sat: str, kind: str, windows: list[str]) -> dict:
    """Build a ResourceSat optical sensor entry.

    kind: 'LISS3' | 'LISS4' | 'AWIFS'
    LISS3 and AWIFS have surface-reflectance (_BOA) variants; LISS4 does not.
    """
    if kind == "LISS4":
        l2  = f"{sat}_LISS4-MX70_L2"
        boa = None
    else:
        l2  = f"{sat}_{kind}_L2"
        boa = f"{sat}_{kind}_BOA"
    return {
        "key": key, "satellite": sat, "optical": True,
        "boa_collection": boa, "l2_collection": l2, "windows": windows,
    }


def sar(windows: list[str]) -> dict:
    """Build an EOS-04 SAR sensor entry (medium-resolution ScanSAR, sigma0)."""
    return {
        "key": "SAR", "satellite": "EOS-04", "optical": False,
        "boa_collection": None, "l2_collection": "EOS-04_SAR-MRS_L2B",
        "windows": windows,
    }


# ============================================================================
# EVENT CATALOG  (the registry)
# ============================================================================
# bbox order is [min_lon, min_lat, max_lon, max_lat].
# cloud_max is per-window; monsoon event windows are generous on purpose and lean
# on SAR where it exists. event_type drives the --type filter. primary_index is
# documentation for the ARD pipeline (not used to fetch).
EVENTS: dict[str, dict] = {

    # ======================= FLOOD (tier 1) =======================
    "kerala_periyar_2018": {
        "tier": 1, "event_type": "flood", "primary_index": "NDWI / MNDWI",
        "display_name": "Kerala floods 2018 — Periyar & Ernakulam basin",
        "bbox": [76.0, 9.5, 77.5, 11.0],
        "event_date": "2018-08-15",
        "windows": {
            "pre_event":  ("2017-06-01", "2018-06-30"),
            "event":      ("2018-08-01", "2018-09-15"),
            "post_event": ("2018-09-16", "2018-12-31"),
            "annual":     ("2015-01-01", "2018-07-31"),
        },
        "cloud_max": {"pre_event": 30, "event": 70, "post_event": 40, "annual": 25},
        "sensors": [
            rs("LISS3",     "ResourceSat-2A", "LISS3", ["pre_event", "event", "post_event"]),
            rs("LISS4",     "ResourceSat-2A", "LISS4", ["event", "post_event"]),
            rs("AWIFS",     "ResourceSat-2A", "AWIFS", ["event", "post_event"]),
            rs("LISS3_RS2", "ResourceSat-2",  "LISS3", ["annual"]),
        ],
    },

    "assam_brahmaputra_2022": {
        "tier": 1, "event_type": "flood", "primary_index": "NDWI / MNDWI + SAR",
        "display_name": "Assam floods 2022 — Brahmaputra & Barak basins",
        "bbox": [89.5, 24.0, 93.5, 26.5],
        "event_date": "2022-06-20",
        "windows": {
            "pre_event":  ("2021-11-01", "2022-04-30"),
            "event":      ("2022-05-15", "2022-07-31"),
            "post_event": ("2022-08-01", "2022-11-30"),
            "annual":     ("2019-01-01", "2022-04-30"),
        },
        "cloud_max": {"pre_event": 30, "event": 80, "post_event": 50, "annual": 25},
        "sensors": [
            rs("LISS3", "ResourceSat-2A", "LISS3", ["pre_event", "event", "post_event"]),
            rs("LISS4", "ResourceSat-2A", "LISS4", ["event", "post_event"]),
            rs("AWIFS", "ResourceSat-2A", "AWIFS", ["event"]),
            sar(["event", "post_event"]),   # EOS-04 active from Feb 2022 — cloud-penetrating
        ],
    },

    "bihar_kosi_ganga_2019": {
        "tier": 1, "event_type": "flood", "primary_index": "NDWI / MNDWI",
        "display_name": "Bihar floods 2019 — Ganga, Kosi, Gandak plains",
        "bbox": [84.5, 25.0, 87.5, 27.0],
        "event_date": "2019-09-28",
        "windows": {
            "pre_event":  ("2018-11-01", "2019-05-31"),
            "event":      ("2019-09-01", "2019-10-31"),
            "post_event": ("2019-11-01", "2020-02-28"),
            "annual":     ("2015-01-01", "2019-08-31"),
        },
        "cloud_max": {"pre_event": 30, "event": 70, "post_event": 40, "annual": 25},
        "sensors": [
            rs("LISS3",     "ResourceSat-2A", "LISS3", ["pre_event", "event", "post_event"]),
            rs("LISS4",     "ResourceSat-2A", "LISS4", ["event", "post_event"]),
            rs("AWIFS",     "ResourceSat-2A", "AWIFS", ["event"]),
            rs("LISS3_RS2", "ResourceSat-2",  "LISS3", ["annual"]),
        ],
    },

    # ======================= WILDFIRE (tier 1) =======================
    "wildfire_uttarakhand_2016": {
        "tier": 1, "event_type": "wildfire", "primary_index": "NBR / dNBR (needs SWIR)",
        "display_name": "Uttarakhand forest fires 2016 — burn scar mapping",
        "bbox": [78.5, 29.3, 80.2, 30.5],
        "event_date": "2016-04-29",
        "windows": {
            "pre_event":  ("2015-11-01", "2016-04-20"),   # pre-fire dry-season baseline
            "event":      ("2016-04-24", "2016-05-31"),   # active fire + immediate post
            "post_event": ("2016-06-01", "2016-07-15"),   # consolidated scar, pre monsoon green-up
            "annual":     ("2014-01-01", "2016-04-20"),
        },
        "cloud_max": {"pre_event": 25, "event": 35, "post_event": 40, "annual": 25},
        "sensors": [
            # 2016: RS-2A not launched yet (Dec 2016). LISS-3 carries SWIR (B5) for NBR.
            rs("LISS3", "ResourceSat-2", "LISS3", ["pre_event", "event", "post_event", "annual"]),
            rs("AWIFS", "ResourceSat-2", "AWIFS", ["pre_event", "event", "post_event"]),
            # LISS-4 has no SWIR -> cannot compute NBR -> omitted on purpose.
        ],
    },

    # ======================= LANDSLIDE / GLOF (tier 1) =======================
    "landslide_sikkim_glof_2023": {
        "tier": 1, "event_type": "landslide", "primary_index": "dNDVI / bare-soil + SAR change",
        "display_name": "Sikkim South Lhonak GLOF 2023 — debris/landslide scars",
        "bbox": [88.0, 27.3, 88.7, 28.1],   # lake + Teesta valley to Chungthang
        "event_date": "2023-10-04",
        "windows": {
            "pre_event":  ("2023-01-01", "2023-09-30"),
            "event":      ("2023-10-03", "2023-10-31"),
            "post_event": ("2023-11-01", "2024-03-31"),
            "annual":     ("2019-01-01", "2023-09-30"),
        },
        "cloud_max": {"pre_event": 50, "event": 70, "post_event": 45, "annual": 40},
        "sensors": [
            rs("LISS3", "ResourceSat-2A", "LISS3", ["pre_event", "event", "post_event"]),
            rs("LISS4", "ResourceSat-2A", "LISS4", ["event", "post_event"]),  # 5.8m narrow-valley scars
            sar(["pre_event", "event", "post_event"]),  # EOS-04 validated on this exact event
        ],
    },

    # ======================= DROUGHT (tier 1) =======================
    "drought_marathwada_2016": {
        "tier": 1, "event_type": "drought", "primary_index": "NDVI -> VCI / VHI (multi-year)",
        "display_name": "Marathwada agricultural drought 2015-16 — NDVI/VCI",
        "bbox": [75.0, 17.5, 78.5, 20.2],   # Aurangabad, Beed, Latur, Osmanabad
        "event_date": "2015-10-15",
        "windows": {
            "pre_event":  ("2013-06-01", "2013-11-30"),   # normal-monsoon baseline kharif
            "event":      ("2015-08-01", "2015-12-31"),   # drought peak, post-monsoon stress
            "post_event": ("2016-08-01", "2016-12-31"),   # 2016 recovery monsoon
            "annual":     ("2011-01-01", "2016-07-31"),   # multi-year NDVI range for VCI/VHI
        },
        "cloud_max": {"pre_event": 35, "event": 30, "post_event": 35, "annual": 30},
        "sensors": [
            # Semi-arid, 2015-16 -> ResourceSat-2 (RS-2A not yet launched). AWiFS = state scale.
            rs("AWIFS", "ResourceSat-2", "AWIFS", ["pre_event", "event", "post_event", "annual"]),
            rs("LISS3", "ResourceSat-2", "LISS3", ["pre_event", "event", "post_event"]),
        ],
    },

    # ======================= FLOOD (tier 2) =======================
    "godavari_ap_2022": {
        "tier": 2, "event_type": "flood", "primary_index": "NDWI / MNDWI + SAR",
        "display_name": "Godavari–Sabari AP floods 2022",
        "bbox": [80.5, 16.0, 82.5, 18.0],
        "event_date": "2022-07-15",
        "windows": {
            "pre_event":  ("2021-11-01", "2022-05-31"),
            "event":      ("2022-07-01", "2022-08-31"),
            "post_event": ("2022-09-01", "2022-12-31"),
        },
        "cloud_max": {"pre_event": 30, "event": 80, "post_event": 50},
        "sensors": [
            rs("LISS3", "ResourceSat-2A", "LISS3", ["pre_event", "event", "post_event"]),
            rs("LISS4", "ResourceSat-2A", "LISS4", ["event"]),
            rs("AWIFS", "ResourceSat-2A", "AWIFS", ["event"]),
            sar(["event", "post_event"]),   # NRSC's EOS-04 flood validation site
        ],
    },

    "uttarakhand_kedarnath_2013": {
        "tier": 2, "event_type": "flood", "primary_index": "NDWI / MNDWI",
        "display_name": "Uttarakhand cloudbursts 2013 — Kedarnath/Chorabari",
        "bbox": [78.8, 30.5, 79.5, 31.0],
        "event_date": "2013-06-16",
        "windows": {
            "pre_event":  ("2012-09-01", "2013-06-10"),
            "event":      ("2013-06-11", "2013-07-31"),
            "post_event": ("2013-08-01", "2013-12-31"),
            "annual":     ("2011-05-01", "2013-06-10"),
        },
        "cloud_max": {"pre_event": 30, "event": 70, "post_event": 40, "annual": 25},
        "sensors": [
            # 2013: only ResourceSat-2 existed.
            rs("LISS3", "ResourceSat-2", "LISS3", ["pre_event", "event", "post_event", "annual"]),
            rs("LISS4", "ResourceSat-2", "LISS4", ["event", "post_event"]),
        ],
    },

    "assam_brahmaputra_2019": {
        "tier": 2, "event_type": "flood", "primary_index": "NDWI / MNDWI",
        "display_name": "Assam floods 2019 — Brahmaputra baseline year",
        "bbox": [89.5, 25.5, 92.5, 26.5],
        "event_date": "2019-07-15",
        "windows": {
            "pre_event":  ("2018-11-01", "2019-05-31"),
            "event":      ("2019-06-01", "2019-08-31"),
            "post_event": ("2019-09-01", "2019-12-31"),
        },
        "cloud_max": {"pre_event": 30, "event": 80, "post_event": 50},
        "sensors": [
            rs("LISS3", "ResourceSat-2A", "LISS3", ["pre_event", "event", "post_event"]),
            rs("AWIFS", "ResourceSat-2A", "AWIFS", ["event"]),
        ],
    },

    # ======================= CYCLONE (tier 2) =======================
    "cyclone_michaung_2023": {
        "tier": 2, "event_type": "cyclone", "primary_index": "dNDVI / MNDWI / BSI + SAR",
        "display_name": "Cyclone Michaung 2023 — AP coast wind+flood damage",
        "bbox": [79.5, 13.0, 81.2, 16.5],   # Chennai -> Bapatla landfall corridor
        "event_date": "2023-12-05",
        "windows": {
            "pre_event":  ("2023-09-01", "2023-11-30"),
            "event":      ("2023-12-03", "2023-12-20"),
            "post_event": ("2023-12-21", "2024-02-29"),
        },
        "cloud_max": {"pre_event": 30, "event": 80, "post_event": 45},
        "sensors": [
            rs("LISS3", "ResourceSat-2A", "LISS3", ["pre_event", "event", "post_event"]),
            rs("LISS4", "ResourceSat-2A", "LISS4", ["post_event"]),
            sar(["event", "post_event"]),   # EOS-04 (NRSC published Michaung products)
        ],
    },

    # ======================= FLOOD (tier 3) =======================
    "kerala_2019": {
        "tier": 3, "event_type": "flood", "primary_index": "NDWI / MNDWI",
        "display_name": "Kerala floods 2019 — same basin, second year",
        "bbox": [76.0, 10.0, 76.8, 11.5],
        "event_date": "2019-08-15",
        "windows": {
            "pre_event":  ("2019-01-01", "2019-07-31"),
            "event":      ("2019-08-08", "2019-09-15"),
            "post_event": ("2019-09-16", "2019-12-31"),
        },
        "cloud_max": {"pre_event": 30, "event": 70, "post_event": 40},
        "sensors": [
            rs("LISS3", "ResourceSat-2A", "LISS3", ["pre_event", "event", "post_event"]),
            rs("LISS4", "ResourceSat-2A", "LISS4", ["event"]),
        ],
    },

    "delhi_yamuna_2023": {
        "tier": 3, "event_type": "flood", "primary_index": "NDWI / MNDWI + SAR",
        "display_name": "Delhi NCR Yamuna floods 2023 — urban flood",
        "bbox": [76.8, 28.3, 77.5, 28.9],
        "event_date": "2023-07-13",
        "windows": {
            "pre_event":  ("2023-01-01", "2023-06-30"),
            "event":      ("2023-07-08", "2023-07-25"),
            "post_event": ("2023-07-26", "2023-10-31"),
        },
        "cloud_max": {"pre_event": 30, "event": 70, "post_event": 40},
        "sensors": [
            rs("LISS3", "ResourceSat-2A", "LISS3", ["pre_event", "event", "post_event"]),
            rs("LISS4", "ResourceSat-2A", "LISS4", ["event"]),
            sar(["event", "post_event"]),   # NRSC published an EOS-04 animation for this event
        ],
    },
}


# ============================================================================
# PRETTY OUTPUT
# ============================================================================
G, Y, R, C = "\033[92m", "\033[93m", "\033[91m", "\033[96m"
B, D, X = "\033[1m", "\033[2m", "\033[0m"

def banner(text):
    bar = "=" * 70
    print(f"\n{C}{bar}{X}\n{C}  {B}{text}{X}\n{C}{bar}{X}")

def step(t): print(f"  {D}>{X} {t}", end=" ", flush=True)
def ok(m="OK"): print(f"{G}{m}{X}")
def warn(m): print(f"{Y}! {m}{X}")
def fail(m): print(f"{R}x {m}{X}")
def info(m): print(f"  {D}.{X} {m}")
def fatal(m, hint=""):
    print(f"\n{R}{B}STOP:{X} {R}{m}{X}")
    if hint:
        print(f"  {Y}-> {hint}{X}")
    sys.exit(1)


# ============================================================================
# DATE HELPERS
# ============================================================================
def parse_d(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()

def chunk_range(start: date, end: date, max_days=CHUNK_DAYS):
    chunks, cur = [], start
    while cur <= end:
        c_end = min(cur + timedelta(days=max_days - 1), end)
        chunks.append((cur, c_end))
        cur = c_end + timedelta(days=1)
    return chunks

def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(s))


# ============================================================================
# CREDENTIALS & AUTH
# ============================================================================
def load_credentials():
    if not ENV_FILE.exists():
        ENV_FILE.write_text("BHOONIDHI_USER=your_user\nBHOONIDHI_PASS=your_pass\n")
        fatal(".env created next to the script.", "Fill in real credentials and re-run.")
    for raw in ENV_FILE.read_text().splitlines():
        if "=" in raw and not raw.strip().startswith("#"):
            k, _, v = raw.partition("=")
            os.environ.setdefault(k.strip(), v.strip())
    user = os.environ.get("BHOONIDHI_USER", "")
    pw   = os.environ.get("BHOONIDHI_PASS", "")
    if not user or "your_user" in user or not pw or "your_pass" in pw:
        fatal("BHOONIDHI_USER / BHOONIDHI_PASS not set in .env")
    return user, pw


def authenticate(session, user, pw):
    banner("Authenticating with Bhoonidhi")
    step("requesting access token")
    r = session.post(AUTH_URL, json={
        "userId": user, "password": pw, "grant_type": "password",
    }, timeout=30)
    if r.status_code == 200:
        d = r.json()
        ok(f"token received (valid {d['expires_in']}s)")
        return {
            "token":   d["access_token"],
            "refresh": d["refresh_token"],
            "expires": datetime.now() + timedelta(seconds=d["expires_in"]),
            "user": user, "pw": pw,
        }
    if r.status_code == 400:
        fatal("HTTP 400 — IP likely not whitelisted", "email bhoonidhi@nrsc.gov.in to whitelist your IP")
    if r.status_code == 401:
        fatal("HTTP 401 — wrong credentials in .env")
    if r.status_code == 403:
        fatal("HTTP 403 — too many active sessions", "log out other sessions or wait, then retry")
    fatal(f"auth failed with HTTP {r.status_code}")


def valid_token(session, st) -> str:
    """Return a live access token, refreshing if it's about to expire."""
    if (st["expires"] - datetime.now()).total_seconds() > 120:
        return st["token"]
    try:
        r = session.post(AUTH_URL, json={
            "userId": st["user"], "refresh_token": st["refresh"],
            "grant_type": "refresh_token",
        }, timeout=30)
        d = r.json()
    except Exception:
        r = session.post(AUTH_URL, json={
            "userId": st["user"], "password": st["pw"], "grant_type": "password",
        }, timeout=30)
        d = r.json()
    st["token"]   = d["access_token"]
    st["refresh"] = d["refresh_token"]
    st["expires"] = datetime.now() + timedelta(seconds=d["expires_in"])
    return st["token"]


# ============================================================================
# CLIENT-SIDE FILTERING
# ============================================================================
def get_cloud(props) -> float | None:
    for k in ("CloudCoverPercentage", "eo:cloud_cover", "CloudCover",
              "cloud_cover", "cloudcover", "Cloud_Cover"):
        if k in props:
            try:
                return float(props[k])
            except (TypeError, ValueError):
                pass
    return None

def is_online(props) -> bool:
    for k in ("Online", "online", "OnlineStatus", "onlinestatus"):
        if k in props:
            return str(props[k]).upper() in ("Y", "YES", "TRUE", "1", "ONLINE")
    return True  # assume online if the field is absent


# ============================================================================
# SEARCH
# ============================================================================
def search_one(session, st, collection, bbox, start: date, end: date):
    """Search a single collection over one date span, chunked. Returns features."""
    feats, n_err, n_chunks = {}, 0, 0
    for c_start, c_end in chunk_range(start, end):
        n_chunks += 1
        payload = {
            "collections": [collection],
            "bbox": bbox,
            "datetime": f"{c_start.isoformat()}T00:00:00Z/{c_end.isoformat()}T23:59:59Z",
            "limit": SEARCH_LIMIT,
        }
        time.sleep(SEARCH_PAUSE_S)
        for attempt in range(3):
            try:
                r = session.post(
                    SEARCH_URL,
                    headers={"Authorization": f"Bearer {valid_token(session, st)}"},
                    json=payload, timeout=60,
                )
                if r.status_code == 429:          # rate limited — back off
                    time.sleep(10 * (attempt + 1))
                    continue
                if r.status_code != 200:
                    n_err += 1
                    break
                for f in r.json().get("features", []):
                    fid = f.get("id")
                    if fid and fid not in feats:
                        feats[fid] = f
                break
            except requests.RequestException:
                if attempt == 2:
                    n_err += 1
                else:
                    time.sleep(5)
    return list(feats.values()), n_chunks, n_err


def resolve_window_dates(event, sensor, window):
    """Apply the satellite epoch. Returns (start, end) or None if impossible."""
    start = parse_d(event["windows"][window][0])
    end   = parse_d(event["windows"][window][1])
    epoch = SATELLITE_EPOCHS.get(sensor["satellite"])
    if epoch:
        if end < epoch:
            return None                       # whole window predates the satellite
        start = max(start, epoch)             # clamp the start to launch date
    return start, end


def search_event(session, st, event_key, max_cloud_override, prefer_boa):
    """Search every (sensor, window) for one event. Returns a list of scene records."""
    event = EVENTS[event_key]
    banner(f"Search: {event['display_name']}  [tier {event['tier']} · {event['event_type']}]")
    info(f"bbox {event['bbox']}   event date {event['event_date']}   index {event['primary_index']}")

    records = []
    for sensor in event["sensors"]:
        for window in sensor["windows"]:
            dates = resolve_window_dates(event, sensor, window)
            if dates is None:
                step(f"{sensor['key']:10s} {window:11s}")
                warn(f"skip — {sensor['satellite']} not launched yet for this window")
                continue
            start, end = dates

            # decide product levels to try (BOA first, then L2)
            attempts = []
            if prefer_boa and sensor["boa_collection"]:
                attempts.append(("BOA", sensor["boa_collection"]))
            attempts.append(("L2", sensor["l2_collection"]))

            chosen_feats, chosen_level, chosen_coll = [], None, None
            for level, coll in attempts:
                feats, n_chunks, n_err = search_one(session, st, coll, event["bbox"], start, end)
                if feats:
                    chosen_feats, chosen_level, chosen_coll = feats, level, coll
                    break
                # if BOA empty, loop continues to L2

            step(f"{sensor['key']:10s} {window:11s} {start.isoformat()[:7]}->{end.isoformat()[:7]}")

            if not chosen_feats:
                fail("0 scenes")
                continue

            # cloud + online filtering (optical only)
            cloud_cap = max_cloud_override
            if cloud_cap is None:
                cloud_cap = event["cloud_max"].get(window, 100)

            kept, dropped_cloud, dropped_offline = [], 0, 0
            for f in chosen_feats:
                props = f.get("properties", {})
                if not is_online(props):
                    dropped_offline += 1
                    continue
                if sensor["optical"]:
                    cc = get_cloud(props)
                    if cc is not None and cc > cloud_cap:
                        dropped_cloud += 1
                        continue
                kept.append(f)

            for f in kept:
                props = f.get("properties", {})
                sid   = f.get("id", "")
                acq   = (props.get("datetime") or "")[:10]
                cc    = get_cloud(props)
                fname = f"{safe_name(acq)}_{safe_name(sid)}.zip"
                rel   = (DATA_RAW / event_key / sensor["key"] / window / fname)
                rel_str = rel.relative_to(PROJECT_ROOT).as_posix()
                records.append({
                    "event_key": event_key,
                    "event_type": event["event_type"],
                    "primary_index": event["primary_index"],
                    "tier": event["tier"],
                    "sensor_key": sensor["key"],
                    "satellite": sensor["satellite"],
                    "collection": chosen_coll,
                    "product_level": chosen_level,
                    "window": window,
                    "scene_id": sid,
                    "acq_date": acq,
                    "cloud_cover": f"{cc:.1f}" if cc is not None else "",
                    "scene_bbox": ",".join(str(round(x, 4)) for x in f.get("bbox", [])) if f.get("bbox") else "",
                    "relative_filepath": rel_str,
                    "download_status": "planned",
                })

            extras = []
            if chosen_level == "BOA":
                extras.append("BOA")
            if dropped_cloud:
                extras.append(f"-{dropped_cloud} cloudy")
            if dropped_offline:
                extras.append(f"-{dropped_offline} offline")
            tag = f"  ({', '.join(extras)})" if extras else ""
            ok(f"{len(kept)} scenes{tag}")

    return records


# ============================================================================
# MANIFEST
# ============================================================================
MANIFEST_COLS = [
    "event_key", "event_type", "primary_index", "tier", "sensor_key", "satellite",
    "collection", "product_level", "window", "scene_id", "acq_date", "cloud_cover",
    "scene_bbox", "relative_filepath", "download_status",
]

def write_manifest(event_key, records):
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    path = MANIFEST_DIR / f"{event_key}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_COLS)
        w.writeheader()
        for rec in records:
            w.writerow(rec)
    return path


# ============================================================================
# DOWNLOAD
# ============================================================================
def resolve_download(feature_id, collection):
    """Bhoonidhi download endpoint. (STAC asset hrefs, if present, could be used
    instead — left as the documented endpoint since that is the proven path.)"""
    return DOWNLOAD_URL, {"id": feature_id, "collection": collection}


def download_records(session, st, records, workers):
    banner("Downloading scenes")
    pending = [r for r in records if r["download_status"] != "skip"]
    if not pending:
        warn("nothing to download")
        return

    already = 0
    for r in records:
        out = PROJECT_ROOT / r["relative_filepath"]
        if out.exists() and out.stat().st_size > 0:
            already += 1
    if already:
        info(f"{already}/{len(records)} already on disk — will skip those (resume)")
    print(f"  queued {B}{len(records)}{X} scenes, concurrency={workers}\n")

    def dl(rec):
        out = PROJECT_ROOT / rec["relative_filepath"]
        if out.exists() and out.stat().st_size > 0:
            return "skip"
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".part")
        url, params = resolve_download(rec["scene_id"], rec["collection"])
        for attempt in range(3):
            try:
                tok = valid_token(session, st)
                with session.get(url, headers={"Authorization": f"Bearer {tok}"},
                                 params=params, stream=True, timeout=600) as r:
                    if r.status_code in (412, 429):
                        time.sleep(15 * (attempt + 1))
                        continue
                    if r.status_code != 200:
                        return "fail"
                    with open(tmp, "wb") as fh:
                        for chunk in r.iter_content(1024 * 1024):
                            fh.write(chunk)
                tmp.rename(out)
                return "ok"
            except requests.RequestException:
                time.sleep(8)
            finally:
                if tmp.exists() and not out.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
        return "fail"

    counts = {"ok": 0, "fail": 0, "skip": 0}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(dl, r): r for r in records}
        bar = tqdm(total=len(futs), desc="Downloading", unit="scene")
        for fut in as_completed(futs):
            rec = futs[fut]
            try:
                res = fut.result()
            except Exception:
                res = "fail"
            counts[res] += 1
            rec["download_status"] = {"ok": "downloaded", "skip": "skipped", "fail": "failed"}[res]
            try:
                bar.set_postfix(**counts)
                bar.update(1)
            except Exception:
                pass
    print()
    ok(f"downloaded={counts['ok']}  skipped={counts['skip']}  failed={counts['fail']}")


# ============================================================================
# CATALOG LISTING
# ============================================================================
def list_catalog():
    banner("Event catalog")
    # count by type
    types = {}
    for ev in EVENTS.values():
        types[ev["event_type"]] = types.get(ev["event_type"], 0) + 1
    info("by type: " + ", ".join(f"{t}={n}" for t, n in sorted(types.items())))

    for tier in (1, 2, 3):
        print(f"\n  {B}TIER {tier}{X}")
        for key, ev in EVENTS.items():
            if ev["tier"] != tier:
                continue
            sensors = ", ".join(s["key"] for s in ev["sensors"])
            wins    = ", ".join(ev["windows"].keys())
            print(f"  {G}{key}{X}  {D}[{ev['event_type']}]{X}")
            print(f"      {ev['display_name']}")
            print(f"      bbox={ev['bbox']}  index={ev['primary_index']}")
            print(f"      sensors=[{sensors}]  windows=[{wins}]")
    print(f"\n  {D}by tier:   python download_data.py --tier 1{X}")
    print(f"  {D}by type:   python download_data.py --type wildfire{X}")
    print(f"  {D}one event: python download_data.py --event {next(iter(EVENTS))}{X}")
    print(f"  {D}preview:   add --search-only{X}\n")


# ============================================================================
# SELECTION
# ============================================================================
def select_events(args) -> list[str]:
    """Resolve which events to process.

    Base set = union of --all / --tier / --event.
    If only --type is given, base = everything, then filtered by type.
    If --type is combined with tier/event, it acts as an intersection filter.
    """
    base = []
    if args.all:
        base = list(EVENTS.keys())
    if args.tier:
        base += [k for k, ev in EVENTS.items() if ev["tier"] in args.tier]
    if args.event:
        for k in args.event:
            if k not in EVENTS:
                fatal(f"unknown event '{k}'", "run --list to see valid keys")
            base.append(k)

    if not base:
        if args.etype:
            base = list(EVENTS.keys())          # type-only selection
        else:
            base = [k for k, ev in EVENTS.items() if ev["tier"] == 1]
            warn("no --tier/--type/--event/--all given; defaulting to TIER 1")

    if args.etype:
        base = [k for k in base if EVENTS[k]["event_type"] in args.etype]

    seen, out = set(), []
    for k in base:
        if k not in seen:
            seen.add(k)
            out.append(k)
    if not out:
        fatal("selection is empty after filtering", "loosen --tier / --type / --event")
    return out


# ============================================================================
# MAIN
# ============================================================================
def main():
    ap = argparse.ArgumentParser(
        description="Download Bhoonidhi multi-hazard event scenes for the agentic GIS project.")
    ap.add_argument("--tier", nargs="+", type=int, choices=[1, 2, 3],
                    help="download whole tier(s), e.g. --tier 1 2")
    ap.add_argument("--type", nargs="+", dest="etype",
                    choices=["flood", "wildfire", "landslide", "drought", "cyclone"],
                    help="filter by hazard type, e.g. --type wildfire drought")
    ap.add_argument("--event", nargs="+",
                    help="download specific event key(s); see --list")
    ap.add_argument("--all", action="store_true", help="every event in the catalog")
    ap.add_argument("--search-only", action="store_true",
                    help="search + write manifests, but do not download")
    ap.add_argument("--list", action="store_true", help="print the catalog and exit")
    ap.add_argument("--workers", type=int, default=MAX_CONCURRENT,
                    help=f"parallel downloads (default {MAX_CONCURRENT})")
    ap.add_argument("--max-cloud", type=float, default=None,
                    help="override per-window cloud ceiling for every window")
    ap.add_argument("--no-prefer-boa", action="store_true",
                    help="do not try _BOA collections first; use _L2 directly")
    ap.add_argument("--verify-ssl", action="store_true",
                    help="enable TLS verification (default off for NRSC networks)")
    args = ap.parse_args()

    print(f"\n{B}Bhoonidhi multi-hazard event downloader{X}")
    print(f"{D}project root: {PROJECT_ROOT}{X}")

    if args.list:
        list_catalog()
        return

    selected = select_events(args)
    prefer_boa = not args.no_prefer_boa
    print(f"{D}selected events ({len(selected)}): {', '.join(selected)}{X}")
    print(f"{D}mode: {'SEARCH-ONLY' if args.search_only else 'SEARCH + DOWNLOAD'}"
          f"  |  prefer_boa={prefer_boa}{X}")

    user, pw = load_credentials()
    session = requests.Session()
    session.verify = True if args.verify_ssl else VERIFY_SSL_DEFAULT
    if not session.verify:
        warn("TLS verification OFF (NRSC default). Use --verify-ssl to enable.")

    st = authenticate(session, user, pw)

    grand_total = 0
    for event_key in selected:
        records = search_event(session, st, event_key, args.max_cloud, prefer_boa)
        path = write_manifest(event_key, records)
        info(f"manifest -> {path.relative_to(PROJECT_ROOT)}  ({len(records)} scenes)")
        grand_total += len(records)

        if not args.search_only and records:
            download_records(session, st, records, args.workers)
            write_manifest(event_key, records)   # rewrite with download_status updated

    # ----- summary -----
    banner("Summary")
    by_type = {}
    for event_key in selected:
        ev = EVENTS[event_key]
        path = MANIFEST_DIR / f"{event_key}.csv"
        n = 0
        if path.exists():
            with open(path, encoding="utf-8") as f:
                n = sum(1 for _ in f) - 1
        by_type[ev["event_type"]] = by_type.get(ev["event_type"], 0) + n
        marker = f"{G}+{X}" if n > 0 else f"{D}.{X}"
        print(f"  {marker} {event_key:32s} {D}[{ev['event_type']:9s}]{X} {n:4d} scenes")
    print(f"\n  {B}by type:{X} " + "  ".join(f"{t}={n}" for t, n in sorted(by_type.items())))
    print(f"  {B}TOTAL planned: {grand_total} scenes{X}"
          f"   {D}(~{grand_total * APPROX_GB_SCENE:.0f} GB if fully downloaded){X}")
    if args.search_only:
        print(f"\n  {B}Next:{X} review manifests in data/manifests/, then drop --search-only to download.")
    else:
        print(f"\n  {B}Next:{X} build events_registry.yaml pointing at these manifests, then the ARD ingest.")
    print(f"\n{G}{B}Done.{X}\n")


if __name__ == "__main__":
    main()