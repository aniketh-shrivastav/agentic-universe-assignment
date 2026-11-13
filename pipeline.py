#!/usr/bin/env python3
"""
Top-level entry point for Interaction Ledger & Reporting Pipeline.
Usage:
    python pipeline.py ingest data/drop_1
    python pipeline.py ingest data/drop_2
    python pipeline.py report --out reports/
"""
from solution.cli import main

if __name__ == "__main__":
    main()
