#!/usr/bin/env python3
"""Local dashboard for working through the jobs that job_finder.py collects.

Run:  python3 dashboard.py   (opens http://127.0.0.1:5050 in your browser)
"""

from jobscout.web import main

if __name__ == "__main__":
    main()
