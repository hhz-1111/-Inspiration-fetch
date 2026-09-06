#!/usr/bin/env python3
"""Stop frontend and backend processes started by start.py."""

from start import stop_services


if __name__ == "__main__":
    stop_services()
