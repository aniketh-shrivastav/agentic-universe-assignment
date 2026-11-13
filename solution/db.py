from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

IST = timezone(timedelta(hours=5, minutes=30))
PII_SECRET_KEY = b"au_secret_pii_key_2026_ledger_99"


def hash_phone(canonical_phone: str) -> str:
    """
    Computes deterministic SHA-256 hash of canonical phone for identity/matching.
    Ensures raw phone is not stored in plaintext at rest.
    """
    if not canonical_phone:
        return ""
    return hashlib.sha256(canonical_phone.encode("utf-8")).hexdigest()


def encrypt_pii(val: str) -> str:
    """
    Encrypts PII text at rest using XOR stream cipher + base64 encoding.
    """
    if val is None:
        return ""
    val_str = str(val)
    key = PII_SECRET_KEY
    raw_bytes = val_str.encode("utf-8")
    encrypted = bytes([b ^ key[i % len(key)] for i, b in enumerate(raw_bytes)])
    return "ENC:" + base64.b64encode(encrypted).decode("ascii")


def decrypt_pii(val: str) -> str:
    """
    Decrypts encrypted PII text.
    """
    if not val or not str(val).startswith("ENC:"):
        return val or ""
    key = PII_SECRET_KEY
    raw_bytes = base64.b64decode(str(val)[4:].encode("ascii"))
    decrypted = bytes([b ^ key[i % len(key)] for i, b in enumerate(raw_bytes)])
    return decrypted.decode("utf-8")


def parse_to_ist(ts_str: str) -> Tuple[str, str]:
    """
    Parses timestamps in UTC (...Z), offset (...[+-]HH:MM), or naive ('2026-10-03 10:01:00').
    Naive means IST. All dates in reports are IST dates.
    Returns:
        (iso_ist_string, date_ist_string)
        e.g. ("2026-10-05T18:50:21+05:30", "2026-10-05")
    """
    s = ts_str.strip()
    if s.endswith("Z"):
        dt = datetime.fromisoformat(s[:-1] + "+00:00")
        dt_ist = dt.astimezone(IST)
    elif "+" in s[10:] or "-" in s[10:]:
        # Has timezone offset
        dt = datetime.fromisoformat(s)
        dt_ist = dt.astimezone(IST)
    else:
        # Naive means IST
        if "T" in s:
            dt = datetime.fromisoformat(s)
        else:
            dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        dt_ist = dt.replace(tzinfo=IST)

    iso_str = dt_ist.strftime("%Y-%m-%dT%H:%M:%S+05:30")
    date_str = dt_ist.strftime("%Y-%m-%d")
    return iso_str, date_str


class Database:
    def __init__(self, db_path: Path | str = "ledger.db"):
        self.db_path = Path(db_path)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        cur = self.conn.cursor()
        cur.executescript(
            """
            PRAGMA foreign_keys = ON;

            -- Ingested files tracker for idempotency
            CREATE TABLE IF NOT EXISTS ingested_files (
                file_path TEXT PRIMARY KEY,
                file_type TEXT NOT NULL,
                workflow_id TEXT,
                batch_id TEXT,
                ingested_at TEXT NOT NULL
            );

            -- Quality metrics counters
            CREATE TABLE IF NOT EXISTS quality_metrics (
                metric_name TEXT PRIMARY KEY,
                metric_value INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS rejection_counts (
                reason TEXT PRIMARY KEY,
                count INTEGER NOT NULL DEFAULT 0
            );

            -- Valid Leads Table (Identity: workflow_id, phone_hash)
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_id TEXT NOT NULL,
                phone_hash TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                phone_masked TEXT NOT NULL,
                external_ref TEXT NOT NULL,
                fields_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(workflow_id, phone_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_leads_identity ON leads(workflow_id, phone_hash);
            CREATE INDEX IF NOT EXISTS idx_leads_batch ON leads(workflow_id, batch_id);
            CREATE INDEX IF NOT EXISTS idx_leads_ext_ref ON leads(workflow_id, external_ref);

            -- Rejected Rows Table (for rejections.csv - No PII)
            CREATE TABLE IF NOT EXISTS rejections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_id TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                row_number INTEGER NOT NULL,
                reason TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rejections_sort ON rejections(workflow_id, batch_id, row_number);

            -- Append-Only Interaction Ledger
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE NOT NULL,
                event_type TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                call_id TEXT NOT NULL,
                phone_hash TEXT,
                occurred_at_ist TEXT NOT NULL,
                ist_date TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                lead_id INTEGER,
                ingested_at TEXT NOT NULL,
                FOREIGN KEY (lead_id) REFERENCES leads(id)
            );
            CREATE INDEX IF NOT EXISTS idx_events_call_id ON events(call_id);
            CREATE INDEX IF NOT EXISTS idx_events_lead_id ON events(lead_id);
            CREATE INDEX IF NOT EXISTS idx_events_wf_date ON events(workflow_id, ist_date);

            -- Staging for orphan events (can't be linked to a lead yet)
            CREATE TABLE IF NOT EXISTS orphan_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE NOT NULL,
                event_type TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                call_id TEXT NOT NULL,
                phone_hash TEXT,
                occurred_at_ist TEXT NOT NULL,
                ist_date TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_orphans_identity ON orphan_events(workflow_id, phone_hash);
            CREATE INDEX IF NOT EXISTS idx_orphans_call_id ON orphan_events(call_id);
            """
        )
        self.conn.commit()

    def is_file_ingested(self, file_path: str) -> bool:
        cur = self.conn.cursor()
        cur.execute("SELECT 1 FROM ingested_files WHERE file_path = ?", (file_path,))
        return cur.fetchone() is not None

    def record_ingested_file(
        self, file_path: str, file_type: str, workflow_id: Optional[str], batch_id: Optional[str]
    ):
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO ingested_files (file_path, file_type, workflow_id, batch_id, ingested_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (file_path, file_type, workflow_id, batch_id, now),
        )
        self.conn.commit()

    def inc_metric(self, name: str, amount: int = 1):
        if amount == 0:
            return
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO quality_metrics (metric_name, metric_value)
            VALUES (?, ?)
            ON CONFLICT(metric_name) DO UPDATE SET metric_value = metric_value + ?
            """,
            (name, amount, amount),
        )
        self.conn.commit()

    def inc_rejection(self, reason: str, amount: int = 1):
        if amount == 0:
            return
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO rejection_counts (reason, count)
            VALUES (?, ?)
            ON CONFLICT(reason) DO UPDATE SET count = count + ?
            """,
            (reason, amount, amount),
        )
        self.conn.commit()

    def get_metrics(self) -> Dict[str, Any]:
        cur = self.conn.cursor()
        cur.execute("SELECT metric_name, metric_value FROM quality_metrics")
        counts = {row["metric_name"]: row["metric_value"] for row in cur.fetchall()}

        cur.execute("SELECT reason, count FROM rejection_counts")
        rejections_by_reason = {row["reason"]: row["count"] for row in cur.fetchall()}

        # Count current orphan events directly from orphan_events table for true final state
        cur.execute("SELECT COUNT(*) AS c FROM orphan_events")
        orphan_count = cur.fetchone()["c"]

        return {
            "lead_rows_total": counts.get("lead_rows_total", 0),
            "lead_rows_rejected": counts.get("lead_rows_rejected", 0),
            "rejections_by_reason": rejections_by_reason,
            "duplicate_lead_rows_merged": counts.get("duplicate_lead_rows_merged", 0),
            "field_warnings": counts.get("field_warnings", 0),
            "events_total": counts.get("events_total", 0),
            "malformed_events": counts.get("malformed_events", 0),
            "duplicate_events_dropped": counts.get("duplicate_events_dropped", 0),
            "orphan_events": orphan_count,
            "unknown_disposition_events": counts.get("unknown_disposition_events", 0),
        }

    def close(self):
        self.conn.close()
