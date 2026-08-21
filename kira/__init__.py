"""Kira package bootstrap: vendor path must exist before third-party imports."""
from pathlib import Path
import sys

VENDOR_DIR = Path(__file__).resolve().parent.parent / "vendor"
if VENDOR_DIR.exists():
    sys.path.insert(0, str(VENDOR_DIR))

APP_NAME = "Kira"
APP_VERSION = "0.9.7"
