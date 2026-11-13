from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from solution.db import Database
from solution.schema_loader import SchemaRegistry


@dataclass
class CallFact:
    call_id: str
    workflow_id: str
    lead_id: int
    initiated_at: str
    ist_date: str
    is_connected: bool
    has_telephony_failed: bool
    effective_disposition: Optional[str]
    is_terminal: bool
    is_success: bool


@dataclass
class LeadFact:
    id: int
    workflow_id: str
    batch_id: str
    phone_masked: str
    external_ref: str
    bucket: str
    total_calls: int
    connected_calls: int
    last_call_at: str
    current_disposition: str
    ever_success: bool


class ReportEngine:
    def __init__(self, db: Database, registry: SchemaRegistry):
        self.db = db
        self.registry = registry

    def compute_call_facts(self) -> Dict[str, CallFact]:
        """
        Computes CallFact for every valid call.
        A call is counted ONLY if its call.initiated event is present (and linked).
        """
        cur = self.db.conn.cursor()

        # 1. Fetch all linked events ordered by occurred_at_ist asc
        cur.execute(
            """
            SELECT event_id, event_type, workflow_id, call_id, occurred_at_ist, ist_date, payload_json, lead_id
            FROM events
            WHERE lead_id IS NOT NULL
            ORDER BY occurred_at_ist ASC, id ASC
            """
        )
        events = cur.fetchall()

        # Group by call_id
        calls_events: Dict[str, List[sqlite3.Row]] = defaultdict(list)
        for ev in events:
            calls_events[ev["call_id"]].append(ev)

        call_facts: Dict[str, CallFact] = {}

        for call_id, ev_list in calls_events.items():
            # Find call.initiated event
            initiated_ev = next((e for e in ev_list if e["event_type"] == "call.initiated"), None)
            if not initiated_ev:
                # Not counted if call.initiated is missing
                continue

            workflow_id = initiated_ev["workflow_id"]
            lead_id = initiated_ev["lead_id"]
            initiated_at = initiated_ev["occurred_at_ist"]
            ist_date = initiated_ev["ist_date"]

            is_connected = any(e["event_type"] == "call.connected" for e in ev_list)
            has_telephony_failed = any(e["event_type"] == "call.telephony_failed" for e in ev_list)

            # Determine effective disposition
            # Latest disposition.override by occurred_at; fallback to latest disposition.set
            overrides = [e for e in ev_list if e["event_type"] == "disposition.override"]
            sets = [e for e in ev_list if e["event_type"] == "disposition.set"]

            chosen_disp_code = None
            if overrides:
                # Last in sorted order is latest
                override_payload = json.loads(overrides[-1]["payload_json"])
                chosen_disp_code = override_payload.get("disposition_code")
            elif sets:
                set_payload = json.loads(sets[-1]["payload_json"])
                chosen_disp_code = set_payload.get("disposition_code")

            is_terminal = False
            is_success = False
            if chosen_disp_code:
                disp_def = self.registry.get_disposition(workflow_id, chosen_disp_code)
                if disp_def:
                    is_terminal = disp_def.is_terminal
                    is_success = disp_def.is_success

            call_facts[call_id] = CallFact(
                call_id=call_id,
                workflow_id=workflow_id,
                lead_id=lead_id,
                initiated_at=initiated_at,
                ist_date=ist_date,
                is_connected=is_connected,
                has_telephony_failed=has_telephony_failed,
                effective_disposition=chosen_disp_code,
                is_terminal=is_terminal,
                is_success=is_success,
            )

        return call_facts

    def compute_lead_facts(self, call_facts: Dict[str, CallFact]) -> List[LeadFact]:
        cur = self.db.conn.cursor()
        cur.execute("SELECT id, workflow_id, batch_id, phone_masked, external_ref FROM leads")
        leads = cur.fetchall()

        # Group calls by lead_id
        lead_calls: Dict[int, List[CallFact]] = defaultdict(list)
        for cf in call_facts.values():
            lead_calls[cf.lead_id].append(cf)

        lead_facts: List[LeadFact] = []

        for lead in leads:
            l_id = lead["id"]
            wf_id = lead["workflow_id"]
            b_id = lead["batch_id"]
            p_masked = lead["phone_masked"]
            ext_ref = lead["external_ref"]

            calls = lead_calls.get(l_id, [])
            total_calls = len(calls)
            connected_calls = sum(1 for c in calls if c.is_connected)

            # Sort calls by initiated_at asc
            calls_sorted = sorted(calls, key=lambda c: c.initiated_at)

            last_call_at = calls_sorted[-1].initiated_at if calls_sorted else ""

            # current_disposition: effective disposition of the most recent call (by initiated time) that has one. Empty if none.
            calls_with_disp = [c for c in calls_sorted if c.effective_disposition is not None]
            current_disposition = calls_with_disp[-1].effective_disposition if calls_with_disp else ""

            ever_success = any(c.is_success for c in calls)

            # Lead bucket resolution (first that matches):
            # 1. yet_to_reach_out: no calls.
            # 2. completed: any call's effective disposition is terminal (is_terminal=true).
            # 3. in_progress_telephony_failed: every call has a call.telephony_failed event.
            # 4. in_progress_dialed: everything else.
            if total_calls == 0:
                bucket = "yet_to_reach_out"
            elif any(c.is_terminal for c in calls):
                bucket = "completed"
            elif all(c.has_telephony_failed for c in calls):
                bucket = "in_progress_telephony_failed"
            else:
                bucket = "in_progress_dialed"

            lead_facts.append(
                LeadFact(
                    id=l_id,
                    workflow_id=wf_id,
                    batch_id=b_id,
                    phone_masked=p_masked,
                    external_ref=ext_ref,
                    bucket=bucket,
                    total_calls=total_calls,
                    connected_calls=connected_calls,
                    last_call_at=last_call_at,
                    current_disposition=current_disposition,
                    ever_success=ever_success,
                )
            )

        return lead_facts

    def generate_batch_summary(self, lead_facts: List[LeadFact], out_path: Path):
        """
        1. batch_summary.csv, sorted by workflow_id, batch_id
        workflow_id,batch_id,total_leads,yet_to_reach_out,in_progress_telephony_failed,in_progress_dialed,completed
        """
        # Group leads by (workflow_id, batch_id)
        batches: Dict[Tuple[str, str], Dict[str, int]] = defaultdict(lambda: {
            "total_leads": 0,
            "yet_to_reach_out": 0,
            "in_progress_telephony_failed": 0,
            "in_progress_dialed": 0,
            "completed": 0,
        })

        for lf in lead_facts:
            key = (lf.workflow_id, lf.batch_id)
            batches[key]["total_leads"] += 1
            batches[key][lf.bucket] += 1

        sorted_keys = sorted(batches.keys(), key=lambda k: (k[0], k[1]))

        with open(out_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "workflow_id",
                "batch_id",
                "total_leads",
                "yet_to_reach_out",
                "in_progress_telephony_failed",
                "in_progress_dialed",
                "completed",
            ])
            for k in sorted_keys:
                counts = batches[k]
                writer.writerow([
                    k[0],
                    k[1],
                    counts["total_leads"],
                    counts["yet_to_reach_out"],
                    counts["in_progress_telephony_failed"],
                    counts["in_progress_dialed"],
                    counts["completed"],
                ])

    def generate_daily_overview(self, call_facts: Dict[str, CallFact], out_path: Path):
        """
        2. daily_overview.csv: one row per workflow per IST date that has at least one call, sorted by workflow_id, date
        workflow_id,date,total_calls,connected_calls,connectivity_rate,success_leads
        """
        # Group calls by (workflow_id, ist_date)
        daily_groups: Dict[Tuple[str, str], List[CallFact]] = defaultdict(list)
        for cf in call_facts.values():
            daily_groups[(cf.workflow_id, cf.ist_date)].append(cf)

        sorted_keys = sorted(daily_groups.keys(), key=lambda k: (k[0], k[1]))

        with open(out_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "workflow_id",
                "date",
                "total_calls",
                "connected_calls",
                "connectivity_rate",
                "success_leads",
            ])
            for k in sorted_keys:
                calls = daily_groups[k]
                total_calls = len(calls)
                connected_calls = sum(1 for c in calls if c.is_connected)
                rate = (connected_calls / total_calls) if total_calls > 0 else 0.0
                rate_str = f"{rate:.4f}"

                # success_leads = distinct leads with at least one call on that date whose effective disposition has is_success=true
                success_lead_ids = {c.lead_id for c in calls if c.is_success}
                success_leads_count = len(success_lead_ids)

                writer.writerow([
                    k[0],
                    k[1],
                    total_calls,
                    connected_calls,
                    rate_str,
                    success_leads_count,
                ])

    def generate_lead_report(self, lead_facts: List[LeadFact], out_path: Path):
        """
        3. lead_report.csv: one row per lead, sorted by workflow_id, external_ref
        workflow_id,external_ref,batch_id,phone_masked,bucket,total_calls,connected_calls,last_call_at,current_disposition,ever_success
        """
        sorted_leads = sorted(lead_facts, key=lambda l: (l.workflow_id, l.external_ref))

        with open(out_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "workflow_id",
                "external_ref",
                "batch_id",
                "phone_masked",
                "bucket",
                "total_calls",
                "connected_calls",
                "last_call_at",
                "current_disposition",
                "ever_success",
            ])
            for lf in sorted_leads:
                ever_succ_str = "true" if lf.ever_success else "false"
                writer.writerow([
                    lf.workflow_id,
                    lf.external_ref,
                    lf.batch_id,
                    lf.phone_masked,
                    lf.bucket,
                    lf.total_calls,
                    lf.connected_calls,
                    lf.last_call_at,
                    lf.current_disposition,
                    ever_succ_str,
                ])

    def generate_rejections_report(self, out_path: Path):
        """
        4. rejections.csv, sorted by workflow_id, batch_id, row_number. No PII.
        workflow_id,batch_id,row_number,reason
        """
        cur = self.db.conn.cursor()
        cur.execute(
            """
            SELECT workflow_id, batch_id, row_number, reason
            FROM rejections
            ORDER BY workflow_id ASC, batch_id ASC, row_number ASC
            """
        )
        rows = cur.fetchall()

        with open(out_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["workflow_id", "batch_id", "row_number", "reason"])
            for r in rows:
                writer.writerow([r["workflow_id"], r["batch_id"], r["row_number"], r["reason"]])

    def generate_data_quality_report(self, out_path: Path):
        """
        5. data_quality.json
        """
        metrics = self.db.get_metrics()
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(metrics, f, indent=2)

    def generate_all_reports(self, out_dir: Path | str):
        out_path = Path(out_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        call_facts = self.compute_call_facts()
        lead_facts = self.compute_lead_facts(call_facts)

        self.generate_batch_summary(lead_facts, out_path / "batch_summary.csv")
        self.generate_daily_overview(call_facts, out_path / "daily_overview.csv")
        self.generate_lead_report(lead_facts, out_path / "lead_report.csv")
        self.generate_rejections_report(out_path / "rejections.csv")
        self.generate_data_quality_report(out_path / "data_quality.json")
