"""Vercel serverless entry point for the public AuditWhy demo.

Exposes the same FastAPI app as `python run.py`. On Vercel (VERCEL=1, or AUDITWHY_PUBLIC_DEMO=true) the app always
runs SAMPLE_MODE: Zetaris and Meterless Nightshift run on the builder's own machine, so a hosted instance has no
route to them, and credential editing is disabled. Traces and H-MEM state live in /tmp (per warm instance).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("AUDITWHY_PUBLIC_DEMO", "true")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from run import app  # noqa: E402,F401  (Vercel's Python runtime serves this ASGI app)
