#!/usr/bin/env python3
"""Convenience entry point: ``python app.py --data samples analyze``."""

from repurchase.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
