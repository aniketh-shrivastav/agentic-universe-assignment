import json
import pytest
from pathlib import Path
from solution.db import Database, hash_phone
from solution.schema_loader import SchemaRegistry
from solution.report import ReportEngine

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "assignment-a-data" / "schemas"


@pytest.fixture
def test_setup(tmp_path):
    db_path = tmp_path / "test_report.db"
    db = Database(db_path)
    registry = SchemaRegistry(SCHEMA_DIR)
    cur = db.conn.cursor()

    # Create 3 leads:
    # Lead 1: Will have a completed call (renewed)
    # Lead 2: Will have telephony failed call only
    # Lead 3: Will have 0 calls (yet_to_reach_out)
    p1 = "+919876543211"
    p2 = "+919876543212"
    p3 = "+919876543213"

    cur.execute(
        """
        INSERT INTO leads (workflow_id, phone_hash, batch_id, phone_masked, external_ref, fields_json, created_at, updated_at)
        VALUES 
        ('motor_renewal', ?, 'batch_001', '+91******3211', 'REF-001', '{}', '2026-10-01T00:00:00+05:30', '2026-10-01T00:00:00+05:30'),
        ('motor_renewal', ?, 'batch_001', '+91******3212', 'REF-002', '{}', '2026-10-01T00:00:00+05:30', '2026-10-01T00:00:00+05:30'),
        ('motor_renewal', ?, 'batch_002', '+91******3213', 'REF-003', '{}', '2026-10-01T00:00:00+05:30', '2026-10-01T00:00:00+05:30')
        """,
        (hash_phone(p1), hash_phone(p2), hash_phone(p3)),
    )
    db.conn.commit()

    # Events for Lead 1 (lead_id = 1):
    # Call 1: initiated, connected, disposition.set=callback_requested, disposition.override=renewed
    cur.execute(
        """
        INSERT INTO events (event_id, event_type, workflow_id, call_id, phone_hash, occurred_at_ist, ist_date, payload_json, lead_id, ingested_at)
        VALUES
        ('ev_1', 'call.initiated', 'motor_renewal', 'call_1', ?, '2026-10-05T10:00:00+05:30', '2026-10-05', '{}', 1, 'now'),
        ('ev_2', 'call.connected', 'motor_renewal', 'call_1', ?, '2026-10-05T10:00:10+05:30', '2026-10-05', '{}', 1, 'now'),
        ('ev_3', 'disposition.set', 'motor_renewal', 'call_1', ?, '2026-10-05T10:02:00+05:30', '2026-10-05', '{"disposition_code": "callback_requested"}', 1, 'now'),
        ('ev_4', 'disposition.override', 'motor_renewal', 'call_1', ?, '2026-10-05T10:05:00+05:30', '2026-10-05', '{"disposition_code": "renewed"}', 1, 'now')
        """,
        (hash_phone(p1), hash_phone(p1), hash_phone(p1), hash_phone(p1)),
    )

    # Events for Lead 2 (lead_id = 2):
    # Call 2: initiated, telephony_failed
    cur.execute(
        """
        INSERT INTO events (event_id, event_type, workflow_id, call_id, phone_hash, occurred_at_ist, ist_date, payload_json, lead_id, ingested_at)
        VALUES
        ('ev_5', 'call.initiated', 'motor_renewal', 'call_2', ?, '2026-10-05T11:00:00+05:30', '2026-10-05', '{}', 2, 'now'),
        ('ev_6', 'call.telephony_failed', 'motor_renewal', 'call_2', ?, '2026-10-05T11:00:05+05:30', '2026-10-05', '{"reason": "carrier_rejected"}', 2, 'now')
        """,
        (hash_phone(p2), hash_phone(p2)),
    )

    # Call 3: Has disposition but NO call.initiated -> must NOT be counted as a call!
    cur.execute(
        """
        INSERT INTO events (event_id, event_type, workflow_id, call_id, phone_hash, occurred_at_ist, ist_date, payload_json, lead_id, ingested_at)
        VALUES
        ('ev_7', 'disposition.set', 'motor_renewal', 'call_ghost', ?, '2026-10-05T12:00:00+05:30', '2026-10-05', '{"disposition_code": "renewed"}', 2, 'now')
        """,
        (hash_phone(p2),),
    )
    db.conn.commit()

    return db, registry, tmp_path


def test_reporting_facts_and_buckets(test_setup):
    db, registry, tmp_path = test_setup
    engine = ReportEngine(db, registry)

    call_facts = engine.compute_call_facts()
    # Exactly 2 valid calls (call_1 and call_2); call_ghost is omitted
    assert len(call_facts) == 2
    assert "call_1" in call_facts
    assert "call_2" in call_facts
    assert "call_ghost" not in call_facts

    # Check Call 1: connected, override to renewed (terminal=True, success=True)
    c1 = call_facts["call_1"]
    assert c1.is_connected is True
    assert c1.effective_disposition == "renewed"
    assert c1.is_terminal is True
    assert c1.is_success is True

    # Check Call 2: not connected, telephony_failed=True, effective_disposition=None
    c2 = call_facts["call_2"]
    assert c2.is_connected is False
    assert c2.has_telephony_failed is True
    assert c2.effective_disposition is None

    lead_facts = engine.compute_lead_facts(call_facts)
    assert len(lead_facts) == 3

    lf_map = {lf.external_ref: lf for lf in lead_facts}

    # Lead 1: completed bucket, ever_success=True
    l1 = lf_map["REF-001"]
    assert l1.bucket == "completed"
    assert l1.total_calls == 1
    assert l1.connected_calls == 1
    assert l1.ever_success is True
    assert l1.current_disposition == "renewed"
    assert l1.last_call_at == "2026-10-05T10:00:00+05:30"

    # Lead 2: in_progress_telephony_failed bucket
    l2 = lf_map["REF-002"]
    assert l2.bucket == "in_progress_telephony_failed"
    assert l2.total_calls == 1
    assert l2.connected_calls == 0
    assert l2.ever_success is False
    assert l2.current_disposition == ""

    # Lead 3: yet_to_reach_out bucket
    l3 = lf_map["REF-003"]
    assert l3.bucket == "yet_to_reach_out"
    assert l3.total_calls == 0
    assert l3.connected_calls == 0
    assert l3.last_call_at == ""


def test_generate_all_reports(test_setup):
    db, registry, tmp_path = test_setup
    engine = ReportEngine(db, registry)
    out_dir = tmp_path / "reports"

    engine.generate_all_reports(out_dir)

    # 1. batch_summary.csv
    batch_summary_file = out_dir / "batch_summary.csv"
    assert batch_summary_file.exists()
    lines = batch_summary_file.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0] == "workflow_id,batch_id,total_leads,yet_to_reach_out,in_progress_telephony_failed,in_progress_dialed,completed"
    # 2 batches: batch_001 (2 leads: 1 completed, 1 telephony failed) and batch_002 (1 lead: yet_to_reach_out)
    assert len(lines) == 3
    assert lines[1] == "motor_renewal,batch_001,2,0,1,0,1"
    assert lines[2] == "motor_renewal,batch_002,1,1,0,0,0"

    # 2. daily_overview.csv
    daily_file = out_dir / "daily_overview.csv"
    assert daily_file.exists()
    d_lines = daily_file.read_text(encoding="utf-8").strip().splitlines()
    assert d_lines[0] == "workflow_id,date,total_calls,connected_calls,connectivity_rate,success_leads"
    # 2 calls on 2026-10-05, 1 connected -> rate 0.5000, 1 success lead
    assert d_lines[1] == "motor_renewal,2026-10-05,2,1,0.5000,1"

    # 3. lead_report.csv
    lead_file = out_dir / "lead_report.csv"
    assert lead_file.exists()
    l_lines = lead_file.read_text(encoding="utf-8").strip().splitlines()
    assert l_lines[0] == "workflow_id,external_ref,batch_id,phone_masked,bucket,total_calls,connected_calls,last_call_at,current_disposition,ever_success"
    assert len(l_lines) == 4  # header + 3 leads

    # 4. rejections.csv
    rejections_file = out_dir / "rejections.csv"
    assert rejections_file.exists()
    r_lines = rejections_file.read_text(encoding="utf-8").strip().splitlines()
    assert r_lines[0] == "workflow_id,batch_id,row_number,reason"

    # 5. data_quality.json
    dq_file = out_dir / "data_quality.json"
    assert dq_file.exists()
    dq = json.loads(dq_file.read_text(encoding="utf-8"))
    assert "lead_rows_total" in dq
    assert "rejections_by_reason" in dq
    assert "orphan_events" in dq
