"""Launch the main app with isolated, repeatable first-start behavior."""

import os
import runpy
import sys
from pathlib import Path


APP_DIR = Path(
    getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
APP_FILE = APP_DIR / "cudy_auto_refill.py"

if not APP_FILE.is_file():
    raise SystemExit(f"Arbeitsdatei nicht gefunden: {APP_FILE}")

os.environ["CUDY_AUTO_REFILL_FIRST_START_TEST"] = "1"
sys.path.insert(0, str(APP_DIR))
runpy.run_path(str(APP_FILE), run_name="__main__")
