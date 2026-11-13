from __future__ import annotations

import argparse
import sys
from pathlib import Path

from solution.db import Database
from solution.ingest import ingest_drop
from solution.report import ReportEngine
from solution.schema_loader import SchemaRegistry


def get_default_schemas_dir() -> Path:
    # Try finding schemas in relative paths
    candidates = [
        Path("assignment-a-data/schemas"),
        Path("../assignment-a-data/schemas"),
        Path(__file__).resolve().parent.parent / "assignment-a-data" / "schemas",
    ]
    for c in candidates:
        if c.exists():
            return c.resolve()
    return candidates[0]


def main():
    parser = argparse.ArgumentParser(description="Interaction Ledger & Reporting Pipeline (Assignment A)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Ingest subcommand
    ingest_parser = subparsers.add_parser("ingest", help="Ingest a data drop folder (e.g. data/drop_1)")
    ingest_parser.add_argument("drop_dir", type=str, help="Path to drop folder (containing leads/ and events/)")
    ingest_parser.add_argument("--db", type=str, default="ledger.db", help="Path to SQLite database file")
    ingest_parser.add_argument("--schemas", type=str, default=None, help="Path to schemas directory")

    # Report subcommand
    report_parser = subparsers.add_parser("report", help="Generate the five report files")
    report_parser.add_argument("--out", type=str, required=True, help="Output directory for reports")
    report_parser.add_argument("--db", type=str, default="ledger.db", help="Path to SQLite database file")
    report_parser.add_argument("--schemas", type=str, default=None, help="Path to schemas directory")

    args = parser.parse_args()

    schemas_dir = Path(args.schemas) if args.schemas else get_default_schemas_dir()
    db = Database(args.db)

    try:
        if args.command == "ingest":
            drop_path = Path(args.drop_dir)
            if not drop_path.exists():
                print(f"Error: Drop directory '{drop_path}' does not exist.", file=sys.stderr)
                sys.exit(1)
            print(f"Ingesting drop: {drop_path} into {args.db} ...")
            ingest_drop(drop_path, schemas_dir, db)
            print("Ingest complete.")

        elif args.command == "report":
            out_dir = Path(args.out)
            print(f"Generating reports into: {out_dir} ...")
            registry = SchemaRegistry(schemas_dir)
            engine = ReportEngine(db, registry)
            engine.generate_all_reports(out_dir)
            print("Reports generated successfully:")
            print(f" - {out_dir / 'batch_summary.csv'}")
            print(f" - {out_dir / 'daily_overview.csv'}")
            print(f" - {out_dir / 'lead_report.csv'}")
            print(f" - {out_dir / 'rejections.csv'}")
            print(f" - {out_dir / 'data_quality.json'}")
    finally:
        db.close()


if __name__ == "__main__":
    main()

