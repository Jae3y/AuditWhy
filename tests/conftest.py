"""Test isolation: every pytest session gets its own logs dir and H-MEM store.

This runs before any `app` module is imported, so app.config.LOGS_DIR (and the H-MEM state file under it) point at a
throwaway directory instead of the repository's logs/, and results never depend on what a previous run learned.
"""

from __future__ import annotations

import os
import tempfile

os.environ["AUDITWHY_LOGS_DIR"] = tempfile.mkdtemp(prefix="auditwhy-test-logs-")
os.environ.setdefault("SCOUT_RATE_LIMIT_PER_MINUTE", "100000")
os.environ["SAMPLE_MODE"] = "true"
