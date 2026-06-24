"""
Walk data/raw/, count scenes per event/sensor/window, and print an inventory table.

Folder layout expected:
  data/raw/<event_key>/<sensor_folder>/<window_type>/<date>_<scene_id>.zip

Sensor folder → band_registry sensor key mapping:
  LISS3       + prefix RA3* → ResourceSat-2A_LISS3_L2
  LISS3_RS2   + prefix R23* → ResourceSat-2_LISS3_L2
  LISS4       + prefix RAF* → ResourceSat-2A_LISS4-MX70_L2
  AWIFS       + prefix RAW* → ResourceSat-2A_AWIFS_L2  (default; RS-2 AWIFS prefix: R2W*)
"""

import sys
from collections import defaultdict
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RAW_DIR = PROJECT_ROOT / "data" / "raw"
BAND_REGISTRY_PATH = PROJECT_ROOT / "config" / "band_registry.yaml"
EVENTS_REGISTRY_PATH = PROJECT_ROOT / "config" / "events_registry.yaml"

# Map (folder_name, filename_prefix) → sensor key in band_registry
_PREFIX_MAP = {
    ("LISS3",    "RA3"): "ResourceSat-2A_LISS3_L2",
    ("LISS3_RS2","R23"): "ResourceSat-2_LISS3_L2",
    ("LISS4",    "RAF"): "ResourceSat-2A_LISS4-MX70_L2",
    ("AWIFS",    "RAW"): "ResourceSat-2A_AWIFS_L2",
    ("AWIFS",    "R2W"): "ResourceSat-2_AWIFS_L2",
}

# Fallback: folder name alone → sensor key (when prefix doesn't match known patterns)
_FOLDER_FALLBACK = {
    "LISS3":    "ResourceSat-2A_LISS3_L2",
    "LISS3_RS2":"ResourceSat-2_LISS3_L2",
    "LISS4":    "ResourceSat-2A_LISS4-MX70_L2",
    "AWIFS":    "ResourceSat-2A_AWIFS_L2",
}


def resolve_sensor_key(folder_name: str, zip_filename: str) -> str:
    """Return the band_registry sensor key for a given folder + filename."""
    # Strip optional date prefix (e.g. '2018-08-22_RAF...' → 'RAF...')
    basename = zip_filename.split("_", 1)[-1] if "_" in zip_filename else zip_filename
    prefix = basename[:3].upper()
    key = _PREFIX_MAP.get((folder_name, prefix))
    if key:
        return key
    return _FOLDER_FALLBACK.get(folder_name, folder_name)


def load_registries():
    with open(BAND_REGISTRY_PATH) as f:
        band_registry = yaml.safe_load(f)
    with open(EVENTS_REGISTRY_PATH) as f:
        events_registry = yaml.safe_load(f)
    known_events = {e["key"] for e in events_registry["events"]}
    return band_registry, known_events


def audit() -> list[dict]:
    band_registry, known_events = load_registries()
    known_sensors = set(band_registry["sensors"].keys())

    # counts[(event, sensor_key, window)] = int
    counts: dict[tuple, int] = defaultdict(int)
    unknown_events: set[str] = set()
    unknown_sensors: set[str] = set()

    if not RAW_DIR.exists():
        print(f"ERROR: {RAW_DIR} does not exist.", file=sys.stderr)
        sys.exit(1)

    for event_dir in sorted(RAW_DIR.iterdir()):
        if not event_dir.is_dir():
            continue
        event_key = event_dir.name
        if event_key not in known_events:
            unknown_events.add(event_key)

        for sensor_dir in sorted(event_dir.iterdir()):
            if not sensor_dir.is_dir():
                continue
            folder_name = sensor_dir.name

            for window_dir in sorted(sensor_dir.iterdir()):
                if not window_dir.is_dir():
                    continue
                window_type = window_dir.name

                zips = list(window_dir.glob("*.zip"))
                if not zips:
                    continue

                # Resolve sensor key from first zip filename
                sensor_key = resolve_sensor_key(folder_name, zips[0].name)
                if sensor_key not in known_sensors:
                    unknown_sensors.add(sensor_key)

                counts[(event_key, sensor_key, window_type)] += len(zips)

    return counts, unknown_events, unknown_sensors


def print_table(counts: dict, unknown_events: set, unknown_sensors: set):
    col_w = [32, 30, 12, 8]
    header = ("Event", "Sensor", "Window", "Scenes")
    sep = "-+-".join("-" * w for w in col_w)

    def row(*cols):
        return " | ".join(str(c).ljust(col_w[i]) for i, c in enumerate(cols))

    print(row(*header))
    print(sep)

    total = 0
    for (event, sensor, window), count in sorted(counts.items()):
        print(row(event, sensor, window, count))
        total += count

    print(sep)
    print(row("TOTAL", "", "", total))

    if unknown_events:
        print(f"\nWARN: Events in data/ not in events_registry.yaml: {sorted(unknown_events)}")
    if unknown_sensors:
        print(f"\nWARN: Sensor keys not in band_registry.yaml: {sorted(unknown_sensors)}")


def main():
    counts, unknown_events, unknown_sensors = audit()
    print_table(counts, unknown_events, unknown_sensors)


if __name__ == "__main__":
    main()
