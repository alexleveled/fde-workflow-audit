#!/usr/bin/env python
"""Convenience entrypoint: `python run_capture.py [args...]` == `digest [args...]`.

With no args, starts the capture loop for the first registered person.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from fde_audit.cli import main  # noqa: E402

if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv:
        argv = ["capture", "start"]
    main(argv)
