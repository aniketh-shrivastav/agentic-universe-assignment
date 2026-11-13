import json
import pytest
from pathlib import Path
from solution.db import Database, hash_phone, encrypt_pii, decrypt_pii, parse_to_ist
from solution.schema_loader import SchemaRegistry
from solution.ingest import process_lead_file, process_event_file, ingest_drop

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "assignment-a-data" / "schemas"


class TestDatabaseAndPII:
    def test_pii_hashing_and_encryption(self):
        phone = "+919876543210"
        h = hash_phone(phone)
        assert len(h) == 64
        # Same phone -> same hash
        assert hash_phone(phone) == h

        name = "Aarav Sharma"
        enc = encrypt_pii(name)
        assert enc.startswith("ENC:")
        assert enc != name
        assert decrypt_pii(enc) == name

    def test_parse_to_ist(self):
        # UTC
        iso_str, date_str = parse_to_ist("2026-10-05T13:20:21Z")
        assert iso_str == "2026-10-05T18:50:21+05:30"
        assert date_str == "2026-10-05"

        # With offset
        iso_str2, date_str2 = parse_to_ist("2026-10-05T18:50:21+05:30")
        assert iso_str2 == "2026-10-05T18:50:21+05:30"
        assert date_str2 == "2026-10-05"

        # Naive (IST)
        iso_str3, date_str3 = parse_to_ist("2026-10-03 10:01:00")
        assert iso_str3 == "2026-10-03T10:01:00+05:30"
        assert date_str3 == "2026-10-03"


class TestIngestionWorkflow:
    @pytest.fixture
    def db(self, tmp_path):
        db_file = tmp_path / "test_ledger.db"
        return Database(db_file)

    @pytest.fixture
    def registry(self):
        return SchemaRegistry(SCHEMA_DIR)

    def test_lead_rejection_and_duplicate_merging(self, tmp_path, db, registry):
        schema = registry.get_schema("motor_renewal")
        
        # Batch 1 with 1 valid lead, 1 invalid phone lead
        csv_batch_1 = tmp_path / "batch_001.csv"
        csv_batch_1.write_text(
            "name,mobile,policy_number,premium\n"
            "Aarav Sharma,9876543210,POL-001,1500\n"
            "Bad Lead,1234,POL-002,1000\n",
            encoding="utf-8"
        )

        process_lead_file(csv_batch_1, "motor_renewal", schema, db)

        cur = db.conn.cursor()
        cur.execute("SELECT * FROM leads")
        leads = cur.fetchall()
        assert len(leads) == 1
        assert leads[0]["batch_id"] == "batch_001"
        assert leads[0]["phone_masked"] == "+91******3210"

        cur.execute("SELECT * FROM rejections")
        rejections = cur.fetchall()
        assert len(rejections) == 1
        assert rejections[0]["reason"] == "invalid_phone"
        assert rejections[0]["row_number"] == 2

        # Batch 2: Same lead with updated premium and expiry
        csv_batch_2 = tmp_path / "batch_002.csv"
        csv_batch_2.write_text(
            "name,mobile,policy_number,expiry,premium\n"
            "Aarav Sharma,9876543210,POL-001,01-Dec-2026,2000\n",
            encoding="utf-8"
        )
        process_lead_file(csv_batch_2, "motor_renewal", schema, db)

        # Still only 1 lead, batch_id remains batch_001 (permanent!)
        cur.execute("SELECT * FROM leads")
        leads_after = cur.fetchall()
        assert len(leads_after) == 1
        assert leads_after[0]["batch_id"] == "batch_001"

        fields = json.loads(leads_after[0]["fields_json"])
        assert fields["premium_amount"] == 2000.0
        assert fields["policy_expiry"] == "2026-12-01"

        metrics = db.get_metrics()
        assert metrics["duplicate_lead_rows_merged"] == 1
        assert metrics["lead_rows_rejected"] == 1

    def test_orphan_event_reconciliation(self, tmp_path, db, registry):
        # 1. Event arrives BEFORE its lead
        event_file = tmp_path / "events_001.jsonl"
        event_file.write_text(
            json.dumps({
                "event_id": "evt_100",
                "event_type": "call.initiated",
                "workflow_id": "motor_renewal",
                "call_id": "call_500",
                "occurred_at": "2026-10-05T10:00:00Z",
                "phone": "+91 98765 43210",
                "payload": {}
            }) + "\n" +
            json.dumps({
                "event_id": "evt_101",
                "event_type": "disposition.set",
                "workflow_id": "motor_renewal",
                "call_id": "call_500",
                "occurred_at": "2026-10-05T10:05:00Z",
                "phone": "+91 98765 43210",
                "payload": {"disposition_code": "renewed"}
            }) + "\n",
            encoding="utf-8"
        )

        process_event_file(event_file, registry, db)

        # Initially, events are in orphan_events staging table
        cur = db.conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM orphan_events")
        assert cur.fetchone()["c"] == 2
        cur.execute("SELECT COUNT(*) AS c FROM events")
        assert cur.fetchone()["c"] == 0

        # 2. Lead arrives in subsequent batch!
        schema = registry.get_schema("motor_renewal")
        csv_file = tmp_path / "batch_later.csv"
        csv_file.write_text(
            "name,mobile,policy_number\n"
            "Aarav Sharma,9876543210,POL-999\n",
            encoding="utf-8"
        )
        process_lead_file(csv_file, "motor_renewal", schema, db)

        # Orphan events must have been automatically reconciled into events table!
        cur.execute("SELECT COUNT(*) AS c FROM orphan_events")
        assert cur.fetchone()["c"] == 0
        cur.execute("SELECT COUNT(*) AS c FROM events WHERE lead_id IS NOT NULL")
        assert cur.fetchone()["c"] == 2

