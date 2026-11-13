from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from solution.db import Database, decrypt_pii, encrypt_pii, hash_phone, parse_to_ist
from solution.normalizers import mask_phone, normalize_phone, validate_lead_row
from solution.schema_loader import SchemaDef, SchemaRegistry


def process_lead_file(
    file_path: Path,
    workflow_id: str,
    schema: SchemaDef,
    db: Database,
):
    batch_id = file_path.stem  # e.g. "batch_001"
    cur = db.conn.cursor()

    with open(file_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        try:
            raw_headers = next(reader)
        except StopIteration:
            return  # Empty file

        # Build column index -> FieldDef mapping
        col_mapping: Dict[int, Any] = {}
        for idx, h in enumerate(raw_headers):
            matched = schema.match_header(h)
            if matched:
                col_mapping[idx] = matched

        ref_field = schema.get_reference_field()
        ref_field_name = ref_field.name if ref_field else "id"

        non_blank_row_num = 0

        for row in reader:
            # Check for blank row (every cell empty or whitespace)
            if not row or all(cell.strip() == "" for cell in row):
                continue  # Skipped and not counted

            non_blank_row_num += 1
            db.inc_metric("lead_rows_total", 1)

            # Map raw row to matched field names
            row_dict: Dict[str, Any] = {}
            for col_idx, field_def in col_mapping.items():
                if col_idx < len(row):
                    row_dict[field_def.name] = row[col_idx]

            clean_fields, rejection_reason, warnings = validate_lead_row(row_dict, schema)

            if rejection_reason:
                db.inc_metric("lead_rows_rejected", 1)
                db.inc_rejection(rejection_reason, 1)
                cur.execute(
                    """
                    INSERT INTO rejections (workflow_id, batch_id, row_number, reason)
                    VALUES (?, ?, ?, ?)
                    """,
                    (workflow_id, batch_id, non_blank_row_num, rejection_reason),
                )
            else:
                db.inc_metric("field_warnings", warnings)
                canonical_phone = clean_fields["phone"]
                p_hash = hash_phone(canonical_phone)
                masked_phone = mask_phone(canonical_phone)
                external_ref = str(clean_fields.get(ref_field_name, ""))

                # Check if lead already exists
                cur.execute(
                    "SELECT id, batch_id, fields_json FROM leads WHERE workflow_id = ? AND phone_hash = ?",
                    (workflow_id, p_hash),
                )
                existing = cur.fetchone()

                if existing is None:
                    # Apply schema defaults for missing fields
                    for f in schema.fields:
                        if clean_fields.get(f.name) is None and f.default is not None:
                            clean_fields[f.name] = f.default

                    # Encrypt PII fields in fields_json
                    stored_fields: Dict[str, Any] = {}
                    for k, v in clean_fields.items():
                        f_def = next((fld for fldld in [schema.fields] for fld in fldld if fld.name == k), None)
                        if f_def and f_def.pii and v is not None:
                            stored_fields[k] = encrypt_pii(str(v))
                        else:
                            stored_fields[k] = v

                    now_iso = parse_to_ist("2026-10-01 00:00:00")[0]
                    cur.execute(
                        """
                        INSERT INTO leads (workflow_id, phone_hash, batch_id, phone_masked, external_ref, fields_json, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            workflow_id,
                            p_hash,
                            batch_id,
                            masked_phone,
                            external_ref,
                            json.dumps(stored_fields),
                            now_iso,
                            now_iso,
                        ),
                    )
                    lead_id = cur.lastrowid

                    # Reconcile previously orphaned events for this (workflow_id, phone_hash)
                    cur.execute(
                        """
                        SELECT id, event_id, event_type, workflow_id, call_id, phone_hash, occurred_at_ist, ist_date, payload_json
                        FROM orphan_events
                        WHERE workflow_id = ? AND phone_hash = ?
                        """,
                        (workflow_id, p_hash),
                    )
                    orphans = cur.fetchall()
                    reconciled_call_ids = set()

                    for orph in orphans:
                        cur.execute(
                            """
                            INSERT OR IGNORE INTO events (event_id, event_type, workflow_id, call_id, phone_hash, occurred_at_ist, ist_date, payload_json, lead_id, ingested_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                orph["event_id"],
                                orph["event_type"],
                                orph["workflow_id"],
                                orph["call_id"],
                                orph["phone_hash"],
                                orph["occurred_at_ist"],
                                orph["ist_date"],
                                orph["payload_json"],
                                lead_id,
                                orph["occurred_at_ist"],
                            ),
                        )
                        reconciled_call_ids.add(orph["call_id"])
                        cur.execute("DELETE FROM orphan_events WHERE id = ?", (orph["id"],))

                    # Also reconcile any call_id orphans (like disposition.override)
                    for c_id in reconciled_call_ids:
                        cur.execute(
                            """
                            SELECT id, event_id, event_type, workflow_id, call_id, phone_hash, occurred_at_ist, ist_date, payload_json
                            FROM orphan_events
                            WHERE workflow_id = ? AND call_id = ?
                            """,
                            (workflow_id, c_id),
                        )
                        call_orphans = cur.fetchall()
                        for c_orph in call_orphans:
                            cur.execute(
                                """
                                INSERT OR IGNORE INTO events (event_id, event_type, workflow_id, call_id, phone_hash, occurred_at_ist, ist_date, payload_json, lead_id, ingested_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    c_orph["event_id"],
                                    c_orph["event_type"],
                                    c_orph["workflow_id"],
                                    c_orph["call_id"],
                                    c_orph["phone_hash"],
                                    c_orph["occurred_at_ist"],
                                    c_orph["ist_date"],
                                    c_orph["payload_json"],
                                    lead_id,
                                    c_orph["occurred_at_ist"],
                                ),
                            )
                            cur.execute("DELETE FROM orphan_events WHERE id = ?", (c_orph["id"],))

                else:
                    # Merged duplicate lead row
                    db.inc_metric("duplicate_lead_rows_merged", 1)
                    existing_id = existing["id"]
                    existing_fields = json.loads(existing["fields_json"])

                    # Overwrite only non-null fields
                    for k, v in clean_fields.items():
                        if v is not None:
                            f_def = next((fld for fldld in [schema.fields] for fld in fldld if fld.name == k), None)
                            if f_def and f_def.pii:
                                existing_fields[k] = encrypt_pii(str(v))
                            else:
                                existing_fields[k] = v

                    # Update external_ref if provided
                    if clean_fields.get(ref_field_name) is not None:
                        new_ref = str(clean_fields[ref_field_name])
                    else:
                        new_ref = external_ref

                    now_iso = parse_to_ist("2026-10-01 00:00:00")[0]
                    cur.execute(
                        """
                        UPDATE leads
                        SET fields_json = ?, external_ref = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (json.dumps(existing_fields), new_ref, now_iso, existing_id),
                    )

    db.conn.commit()


def process_event_file(
    file_path: Path,
    registry: SchemaRegistry,
    db: Database,
):
    cur = db.conn.cursor()

    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line_str = line.strip()
            if not line_str:
                continue

            db.inc_metric("events_total", 1)

            # Step 1: Malformed check
            try:
                event_data = json.loads(line_str)
                if not isinstance(event_data, dict):
                    db.inc_metric("malformed_events", 1)
                    continue
                event_id = event_data.get("event_id")
                if not event_id or not isinstance(event_id, str):
                    db.inc_metric("malformed_events", 1)
                    continue
            except Exception:
                db.inc_metric("malformed_events", 1)
                continue

            # Step 2: Duplicate check
            cur.execute(
                "SELECT 1 FROM events WHERE event_id = ? UNION SELECT 1 FROM orphan_events WHERE event_id = ?",
                (event_id, event_id),
            )
            if cur.fetchone() is not None:
                db.inc_metric("duplicate_events_dropped", 1)
                continue

            event_type = event_data.get("event_type", "")
            workflow_id = event_data.get("workflow_id", "")
            call_id = event_data.get("call_id", "")
            occurred_at_raw = event_data.get("occurred_at", "")
            payload = event_data.get("payload", {})
            phone_raw = event_data.get("phone")

            # Step 3: Check unknown disposition
            if event_type.startswith("disposition."):
                disp_code = payload.get("disposition_code") if isinstance(payload, dict) else None
                if not disp_code or not registry.get_disposition(workflow_id, disp_code):
                    db.inc_metric("unknown_disposition_events", 1)
                    continue

            # Parse timestamp to IST
            try:
                occurred_at_ist, ist_date = parse_to_ist(occurred_at_raw)
            except Exception:
                db.inc_metric("malformed_events", 1)
                continue

            # Step 4: Link to Lead
            phone_hash = None
            lead_id = None

            if phone_raw is not None:
                canonical_p = normalize_phone(phone_raw)
                if canonical_p:
                    phone_hash = hash_phone(canonical_p)
                    cur.execute(
                        "SELECT id FROM leads WHERE workflow_id = ? AND phone_hash = ?",
                        (workflow_id, phone_hash),
                    )
                    lead_row = cur.fetchone()
                    if lead_row:
                        lead_id = lead_row["id"]

            if lead_id is None and call_id:
                # Link via call_id to the lead of that call's other events
                cur.execute(
                    "SELECT lead_id, phone_hash FROM events WHERE call_id = ? AND lead_id IS NOT NULL LIMIT 1",
                    (call_id,),
                )
                prev_event = cur.fetchone()
                if prev_event:
                    lead_id = prev_event["lead_id"]
                    if not phone_hash:
                        phone_hash = prev_event["phone_hash"]

            payload_str = json.dumps(payload)

            if lead_id is not None:
                # Linked event -> insert into append-only interaction ledger
                cur.execute(
                    """
                    INSERT INTO events (event_id, event_type, workflow_id, call_id, phone_hash, occurred_at_ist, ist_date, payload_json, lead_id, ingested_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        event_type,
                        workflow_id,
                        call_id,
                        phone_hash,
                        occurred_at_ist,
                        ist_date,
                        payload_str,
                        lead_id,
                        occurred_at_ist,
                    ),
                )
            else:
                # Orphan event -> store in orphan_events staging table
                cur.execute(
                    """
                    INSERT INTO orphan_events (event_id, event_type, workflow_id, call_id, phone_hash, occurred_at_ist, ist_date, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        event_type,
                        workflow_id,
                        call_id,
                        phone_hash,
                        occurred_at_ist,
                        ist_date,
                        payload_str,
                        occurred_at_ist,
                    ),
                )

    db.conn.commit()


def ingest_drop(
    drop_dir: Path | str,
    schemas_dir: Path | str,
    db: Database,
):
    """
    Ingests one drop folder (e.g. data/drop_1).
    Processes lead files first (in filename order), then event files (in filename order).
    Idempotent: skips files already recorded as ingested.
    """
    drop_path = Path(drop_dir)
    registry = SchemaRegistry(schemas_dir)

    # 1. Ingest Leads
    leads_root = drop_path / "leads"
    if leads_root.exists():
        # Find all workflow subdirectories
        for wf_dir in sorted(leads_root.iterdir()):
            if not wf_dir.is_dir():
                continue
            workflow_id = wf_dir.name
            schema = registry.get_schema(workflow_id)
            if not schema:
                continue

            # Process CSV files in filename order
            csv_files = sorted(wf_dir.glob("*.csv"))
            for csv_file in csv_files:
                file_key = str(csv_file.resolve())
                if db.is_file_ingested(file_key):
                    continue
                process_lead_file(csv_file, workflow_id, schema, db)
                db.record_ingested_file(file_key, "leads", workflow_id, csv_file.stem)

    # 2. Ingest Events
    events_root = drop_path / "events"
    if events_root.exists():
        jsonl_files = sorted(events_root.glob("*.jsonl"))
        for jsonl_file in jsonl_files:
            file_key = str(jsonl_file.resolve())
            if db.is_file_ingested(file_key):
                continue
            process_event_file(jsonl_file, registry, db)
            db.record_ingested_file(file_key, "events", None, jsonl_file.stem)

