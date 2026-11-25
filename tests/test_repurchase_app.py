"""Behavior tests for governed identity, refunds, maturity, and campaigns."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repurchase.analysis import diff_analysis, run_analysis
from repurchase.campaigns import CampaignService
from repurchase.io import load_input
from repurchase.reporting import write_analysis_outputs


class WorkspaceBuilder:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.temp.cleanup()

    def write(self, name: str, header: list[str], rows: list[list[object]]):
        path = self.root / name
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows(rows)
        return path

    def write_standard(self, identity, consent, orders, **optional):
        self.write("identity.csv", ["member", "source", "person", "from", "to", "version", "state"], identity)
        self.write("consent.csv", ["person", "channel", "state", "at"], consent)
        self.write("orders.csv", ["order_id", "member", "channel", "paid", "state", "at"], orders)
        for raw_name, spec in optional.items():
            header, rows = spec
            filename = raw_name if raw_name.endswith(".csv") else f"{raw_name}.csv"
            self.write(filename, header, rows)
        return self.root


def confirmed_link(member, person, start="2026-01-01", end="", version=1, state="confirmed", source="web"):
    return [member, source, person, start, end, version, state]


class RepurchaseAnalysisTests(unittest.TestCase):
    def test_cross_channel_merge_and_90_day_boundary(self):
        with WorkspaceBuilder() as ws:
            ws.write_standard(
                identity=[
                    confirmed_link("M1", "P1"),
                    confirmed_link("M2", "P1", source="store"),
                    confirmed_link("M3", "P3"),
                    confirmed_link("M4", "P4"),
                ],
                consent=[],
                orders=[
                    ["O1", "M1", "web", "100", "completed", "2026-01-01"],
                    ["O2", "M2", "store", "50", "completed", "2026-03-12"],  # day 70
                    ["O3", "M3", "web", "100", "completed", "2026-01-01"],
                    ["O4", "M3", "live", "50", "completed", "2026-04-01"],  # exactly day 90
                    ["O5", "M4", "web", "100", "completed", "2026-01-01"],
                    ["O6", "M4", "web", "50", "completed", "2026-04-02"],  # day 91
                ],
            )
            result = run_analysis(load_input(ws.root), as_of="2026-06-01")
            by_person = {c.person_id: c for c in result.customers if c.person_id}
            self.assertEqual(["M1", "M2"], by_person["P1"].member_ids)
            self.assertEqual("repeat", by_person["P1"].repeat_status)
            self.assertEqual(70, by_person["P1"].days_to_repeat)
            self.assertEqual("repeat", by_person["P3"].repeat_status)
            self.assertEqual(90, by_person["P3"].days_to_repeat)
            self.assertEqual("no_repeat", by_person["P4"].repeat_status)
            self.assertIsNone(by_person["P4"].second_order_id)

    def test_cancelled_and_full_refund_are_excluded(self):
        with WorkspaceBuilder() as ws:
            ws.write_standard(
                identity=[confirmed_link("M1", "P1"), confirmed_link("M2", "P2")],
                consent=[],
                orders=[
                    ["O1", "M1", "web", "100", "completed", "2026-01-01"],
                    ["O2", "M1", "web", "90", "cancelled", "2026-01-20"],
                    ["O3", "M1", "web", "80", "refunded", "2026-02-01"],
                    ["O4", "M2", "web", "100", "completed", "2026-01-01"],
                    ["O5", "M2", "web", "80", "completed", "2026-01-20"],
                ],
                refunds=(
                    ["refund_id", "order_id", "amount", "at"],
                    [["R1", "O5", "80", "2026-02-10"]],
                ),
            )
            data = load_input(ws.root)
            result = run_analysis(data, as_of="2026-06-01")
            customers = {c.person_id: c for c in result.customers}
            p1 = customers["P1"]
            self.assertEqual(1, p1.order_count_valid)
            self.assertEqual(["O2", "O3"], p1.excluded_order_ids)
            self.assertEqual("no_repeat", p1.repeat_status)
            p2 = customers["P2"]
            self.assertEqual(["O5"], p2.excluded_order_ids)
            self.assertEqual("no_repeat", p2.repeat_status)

    def test_late_refund_of_first_order_shifts_window(self):
        with WorkspaceBuilder() as ws:
            ws.write_standard(
                identity=[confirmed_link("M1", "P1")],
                consent=[],
                orders=[
                    ["O1", "M1", "web", "100", "completed", "2026-01-01"],
                    ["O2", "M1", "web", "80", "completed", "2026-01-20"],
                    ["O3", "M1", "web", "60", "completed", "2026-04-20"],
                ],
                refunds=(
                    ["refund_id", "order_id", "amount", "at"],
                    [["R1", "O1", "100", "2026-05-01"]],
                ),
            )
            before = run_analysis(load_input(ws.root), as_of="2026-04-21")
            before_customer = before.customers[0]
            self.assertEqual("repeat", before_customer.repeat_status)
            self.assertEqual("O1", before_customer.first_order_id)

            after = run_analysis(load_input(ws.root), as_of="2026-05-02")
            customer = after.customers[0]
            self.assertEqual("O2", customer.first_order_id)
            self.assertEqual("O3", customer.second_order_id)
            self.assertEqual(90, customer.days_to_repeat)
            diff = diff_analysis(before, after)
            self.assertTrue(diff["customer_changes"])

    def test_disputed_and_missing_identity_are_indeterminate_not_no_repeat(self):
        with WorkspaceBuilder() as ws:
            ws.write_standard(
                identity=[
                    confirmed_link("D1", "PD", state="disputed"),
                    confirmed_link("D2", "PD", state="confirmed"),
                ],
                consent=[],
                orders=[
                    ["O1", "D1", "web", "100", "completed", "2026-01-01"],
                    ["O2", "D2", "store", "100", "completed", "2026-02-01"],
                    ["O3", "X1", "web", "100", "completed", "2026-01-01"],
                ],
            )
            result = run_analysis(load_input(ws.root), as_of="2026-06-01")
            by_id = {c.customer_id: c for c in result.customers}
            disputed = by_id["M:D1"]
            self.assertEqual("indeterminate", disputed.repeat_status)
            self.assertIn("O2", disputed.possible_extra_order_ids)
            self.assertTrue(disputed.identity_material)
            missing = by_id["M:X1"]
            self.assertEqual("incomplete", missing.identity_status)
            self.assertEqual("indeterminate", missing.repeat_status)
            self.assertEqual(2, result.summary["indeterminate_customers"])
            self.assertGreaterEqual(
                sum(row["indeterminate_customers"] for row in result.segments),
                2,
            )
            for segment in result.segments:
                if segment["mature_customers"]:
                    self.assertIsNotNone(segment["repurchase_rate_upper_bound_90d"])
                    self.assertGreaterEqual(
                        segment["repurchase_rate_upper_bound_90d"],
                        segment["repurchase_rate_lower_bound_90d"],
                    )

    def test_immature_customers_and_common_horizon(self):
        with WorkspaceBuilder() as ws:
            ws.write_standard(
                identity=[confirmed_link("M1", "P1"), confirmed_link("M2", "P2")],
                consent=[],
                orders=[
                    ["O1", "M1", "web", "100", "completed", "2026-10-01"],
                    ["O2", "M2", "web", "100", "completed", "2026-10-01"],
                    ["O3", "M2", "web", "50", "completed", "2026-10-11"],
                ],
                members=(
                    ["member", "joined_at", "region"],
                    [["M1", "2026-09-20", "East"], ["M2", "2026-09-25", "North"]],
                ),
            )
            result = run_analysis(load_input(ws.root), as_of="2026-11-25", horizons=[10, 30, 60, 90])
            rows = {(r["region"], r["horizon_days"]): r for r in result.common_period}
            # M1 has 55 observed days but no repeat.
            self.assertEqual(1, rows[("East", 30)]["eligible_customers"])
            self.assertEqual(0, rows[("East", 30)]["repeat_customers"])
            self.assertEqual(1, rows[("East", 30)]["certain_no_repeat_customers"])
            self.assertEqual(0, rows[("East", 60)]["eligible_customers"])
            # M2 repeated on day 10 and is eligible for the 10-day comparison.
            self.assertEqual(1, rows[("North", 10)]["repeat_customers"])
            north_segment = next(r for r in result.segments if r["region"] == "North")
            self.assertEqual(1, north_segment["window_open_customers"])
            self.assertEqual(1, north_segment["early_repeat_window_open"])

    def test_outputs_are_written(self):
        with WorkspaceBuilder() as ws:
            ws.write_standard(
                identity=[confirmed_link("M1", "P1")],
                consent=[["P1", "web", "active", "2026-01-01"]],
                orders=[["O1", "M1", "web", "100", "completed", "2026-01-01"]],
            )
            data = load_input(ws.root)
            result = run_analysis(data, as_of="2026-06-01")
            out = ws.root / "out"
            paths = write_analysis_outputs(result, out)
            for path in paths.values():
                if isinstance(path, Path):
                    self.assertTrue(path.exists() and path.stat().st_size >= 0)
            payload = json.loads((out / "analysis.json").read_text())
            self.assertEqual(1, payload["summary"]["customers_total"])


class CampaignTests(unittest.TestCase):
    def _write_campaign_workspace(self, ws: WorkspaceBuilder, identity_state="confirmed", consent_state="active"):
        ws.write_standard(
            identity=[
                ["M1", "web", "P1", "2026-01-01", "", 1, identity_state],
            ],
            consent=[["P1", "web", consent_state, "2026-01-01"]],
            orders=[["O1", "M1", "web", "100", "completed", "2026-01-02"]],
            campaign_lists=(
                ["list_id", "name", "channel", "owner", "status"],
                [["L1", "Winback", "web", "alice", "draft"]],
            ),
            campaign_recipients=(
                ["list_id", "person", "member"],
                [["L1", "P1", ""]],
            ),
            exclusions=(
                ["person", "code", "reason", "from", "to"],
                [["P1", "employee", "staff test", "2026-01-01", ""]],
            ),
        )

    def test_freeze_enforces_consent_identity_and_exclusions(self):
        with WorkspaceBuilder() as ws:
            self._write_campaign_workspace(ws)
            data = load_input(ws.root)
            service = CampaignService(data, ws.root / "state")
            snapshot = service.freeze_list("L1", at="2026-02-01")
            row = snapshot["rows"][0]
            self.assertFalse(row["eligible"])
            self.assertIn("exclusion_employee", row["reasons"])

        with WorkspaceBuilder() as ws:
            ws.write_standard(
                identity=[["M1", "web", "P1", "2026-01-01", "", 1, "confirmed"]],
                consent=[["P1", "web", "withdrawn", "2026-01-15"]],
                orders=[],
                campaign_lists=(["list_id", "channel", "owner"], [["L1", "web", "bob"]]),
                campaign_recipients=(["list_id", "person"], [["L1", "P1"]]),
            )
            snapshot = CampaignService(load_input(ws.root), ws.root / "state").freeze_list("L1", at="2026-02-01")
            self.assertFalse(snapshot["rows"][0]["eligible"])
            self.assertEqual("withdrawn", snapshot["rows"][0]["consent_state"])

    def test_sent_list_is_immutable_but_owner_is_alerted(self):
        with WorkspaceBuilder() as ws:
            ws.write_standard(
                identity=[["M1", "web", "P1", "2026-01-01", "", 1, "confirmed"]],
                consent=[["P1", "web", "active", "2026-01-01"]],
                orders=[["O1", "M1", "web", "100", "completed", "2026-01-02"]],
                campaign_lists=(["list_id", "channel", "owner"], [["L1", "web", "alice"]]),
                campaign_recipients=(["list_id", "person"], [["L1", "P1"]]),
            )
            state_dir = ws.root / "state"
            service = CampaignService(load_input(ws.root), state_dir)
            frozen = service.freeze_list("L1", at="2026-02-01")
            self.assertTrue(frozen["rows"][0]["eligible"])
            sent = service.mark_sent("L1", at="2026-02-02")
            self.assertTrue(sent["sent_record_locked"])
            sent_file = state_dir / "sent" / "L1.sent.json"
            original = sent_file.read_text(encoding="utf-8")

            # Customer withdraws after send.
            with (ws.root / "consent.csv").open("a", encoding="utf-8", newline="") as handle:
                csv.writer(handle).writerow(["P1", "web", "withdrawn", "2026-03-01"])
            new_data = load_input(ws.root)
            review = CampaignService(new_data, state_dir).review_lists(at="2026-03-10", analysis_result=run_analysis(new_data, "2026-03-10"))
            alert = next(a for a in review["alerts"] if a["list_id"] == "L1")
            self.assertEqual("sent_immutable", alert["record_type"])
            self.assertTrue(alert["eligibility_changed"])
            self.assertFalse(alert["current_eligible"])
            self.assertEqual(original, sent_file.read_text(encoding="utf-8"))
            self.assertTrue((state_dir / "alerts" / "owner_alice.md").exists())

    def test_identity_and_order_corrections_alert_sent_list_owner(self):
        with WorkspaceBuilder() as ws:
            ws.write_standard(
                identity=[["M1", "web", "P1", "2026-01-01", "", 1, "confirmed"]],
                consent=[["P1", "web", "active", "2026-01-01"]],
                orders=[["O1", "M1", "web", "100", "completed", "2026-01-02"]],
                campaign_lists=(
                    ["list_id", "channel", "owner", "exclude_recent_days"],
                    [["L9", "web", "dana", "30"]],
                ),
                campaign_recipients=(["list_id", "person", "member"], [["L9", "P1", ""]]),
            )
            state_dir = ws.root / "state"
            service = CampaignService(load_input(ws.root), state_dir)
            frozen = service.freeze_list("L9", at="2026-03-01")
            self.assertTrue(frozen["rows"][0]["eligible"])
            service.mark_sent("L9", at="2026-03-01T12:00")

            # Governance publishes version 2 disputing the merge.
            ws.write(
                "identity.csv",
                ["member", "source", "person", "from", "to", "version", "state"],
                [["M1", "web", "P1", "2026-01-01", "", 2, "disputed"]],
            )
            # A later full refund correction arrives.
            ws.write(
                "refunds.csv",
                ["refund_id", "order_id", "amount", "at"],
                [["R9", "O1", "100", "2026-03-05"]],
            )
            new_data = load_input(ws.root)
            analysis = run_analysis(new_data, as_of="2026-03-10")
            review = CampaignService(new_data, state_dir).review_lists(at="2026-03-10", analysis_result=analysis)
            alerts = [a for a in review["alerts"] if a["list_id"] == "L9"]
            self.assertTrue(any("identity_disputed" in a.get("current_reasons", []) for a in alerts))
            self.assertTrue(any(a.get("related_impacts") for a in alerts))
            sent_file = state_dir / "sent" / "L9.sent.json"
            sent = json.loads(sent_file.read_text())
            self.assertTrue(sent["rows"][0]["eligible"])
            self.assertEqual(1, sent["snapshot_version"])

    def test_unsent_list_is_reverified_with_new_snapshot(self):
        with WorkspaceBuilder() as ws:
            ws.write_standard(
                identity=[["M1", "web", "P1", "2026-01-01", "", 1, "confirmed"]],
                consent=[["P1", "web", "active", "2026-01-01"]],
                orders=[],
                campaign_lists=(["list_id", "channel", "owner"], [["L2", "web", "carol"]]),
                campaign_recipients=(["list_id", "person"], [["L2", "P1"]]),
            )
            state_dir = ws.root / "state"
            service = CampaignService(load_input(ws.root), state_dir)
            first = service.freeze_list("L2", at="2026-02-01")
            self.assertTrue(first["rows"][0]["eligible"])

            with (ws.root / "consent.csv").open("a", encoding="utf-8", newline="") as handle:
                csv.writer(handle).writerow(["P1", "web", "withdrawn", "2026-02-10"])
            new_data = load_input(ws.root)
            analysis = run_analysis(new_data, as_of="2026-02-20")
            review = CampaignService(new_data, state_dir).review_lists(at="2026-02-20", analysis_result=analysis)
            unsent = next(item for item in review["lists"] if item["list_id"] == "L2")
            self.assertEqual("unsent_reverified", unsent["status"])
            self.assertEqual(1, unsent["excluded"])
            self.assertTrue(unsent["changes"])
            alert = next(a for a in review["alerts"] if a["list_id"] == "L2")
            self.assertEqual("unsent_reverification", alert["record_type"])
            self.assertTrue(alert["eligibility_changed"])
            self.assertFalse(alert["current_eligible"])
            consent_impact = next(i for i in analysis.impacts if i["trigger_type"] == "consent_withdrawn")
            self.assertEqual(["L2"], consent_impact["affected_list_ids"])
            state = json.loads((state_dir / "state.json").read_text())
            self.assertIn("reverification", state["lists"]["L2"]["current_snapshot"])
            self.assertTrue((state_dir / "alerts" / "owner_carol.md").exists())


if __name__ == "__main__":
    unittest.main()
