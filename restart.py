#!/usr/bin/env python3
"""Restart the managed frontend and backend services."""

import sys
import time

from start import start_services, stop_services


if __name__ == "__main__":
    try:
        stop_services(quiet=True)
        time.sleep(0.5)
        start_services()
    except Exception as exc:
        print(f"Restart failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
