#!/usr/bin/env python3
"""Alternate conventional entry point: ``python main.py analyze``."""

from repurchase.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
