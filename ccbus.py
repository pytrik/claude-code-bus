#!/usr/bin/env python3
"""Zero-install entry point.

Sessions invoke the bus by absolute path with no pip install:

    python "<plugin-root>/ccbus.py" --me alice recv

This shim only puts src/ on sys.path and hands off to the package. All real
code lives under src/ccbus/.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from ccbus.cli import main

if __name__ == "__main__":
    sys.exit(main())
