"""
Install the VYOM QGIS plugin into the active QGIS profile's plugins directory.

  python -m vyom.plugin.install              # copy into the default QGIS profile
  python -m vyom.plugin.install --symlink    # dev: symlink instead of copy
  python -m vyom.plugin.install --dest PATH  # explicit target plugins dir
  python -m vyom.plugin.install --profile X  # non-default QGIS profile name

QGIS loads a plugin from a folder named after its package, so this installs the
``src/vyom/plugin`` directory as ``<plugins_dir>/vyom``. Only the files the plugin needs
at runtime are copied (Python sources, metadata.txt, README) — no ``__pycache__``.
"""

import argparse
import os
import shutil
import sys
from pathlib import Path

PLUGIN_DIR_NAME = "vyom"
_SOURCE = Path(__file__).resolve().parent


def default_plugins_dir(profile: str = "default") -> Path:
    """Best-effort path to the QGIS3 plugins dir for the given profile, per-OS."""
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
        root = base / "QGIS" / "QGIS3"
    elif sys.platform == "darwin":
        root = Path.home() / "Library/Application Support/QGIS/QGIS3"
    else:
        root = Path.home() / ".local/share/QGIS/QGIS3"
    return root / "profiles" / profile / "python" / "plugins"


def _ignore(_dir, names):
    return [n for n in names if n in {"__pycache__"} or n.endswith(".pyc")]


def install(dest_plugins_dir: Path, symlink: bool = False) -> Path:
    """Place the plugin at ``<dest_plugins_dir>/vyom``; return that path."""
    dest_plugins_dir = Path(dest_plugins_dir)
    dest_plugins_dir.mkdir(parents=True, exist_ok=True)
    target = dest_plugins_dir / PLUGIN_DIR_NAME

    # Remove any prior install (or stale symlink) first.
    if target.is_symlink() or target.exists():
        if target.is_symlink():
            target.unlink()
        else:
            shutil.rmtree(target)

    if symlink:
        target.symlink_to(_SOURCE, target_is_directory=True)
    else:
        shutil.copytree(_SOURCE, target, ignore=_ignore)
    return target


def main(argv=None):
    ap = argparse.ArgumentParser(description="Install the VYOM QGIS plugin.")
    ap.add_argument("--dest", help="Target QGIS plugins directory (overrides --profile).")
    ap.add_argument("--profile", default="default", help="QGIS profile name.")
    ap.add_argument("--symlink", action="store_true",
                    help="Symlink the source folder instead of copying (dev mode).")
    args = ap.parse_args(argv)

    dest = Path(args.dest) if args.dest else default_plugins_dir(args.profile)
    target = install(dest, symlink=args.symlink)
    verb = "Symlinked" if args.symlink else "Copied"
    print(f"{verb} VYOM plugin -> {target}")
    print("Enable it in QGIS: Plugins -> Manage and Install Plugins -> Installed -> "
          "'VYOM -- Agentic GIS' (tick 'Show experimental plugins' if hidden).")


if __name__ == "__main__":
    main()
