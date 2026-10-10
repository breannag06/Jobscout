#!/usr/bin/env python3
"""Entry point. Keeps the original `python3 job_finder.py` command working."""

import sys

from jobscout.cli import main

if __name__ == "__main__":
    sys.exit(main())
