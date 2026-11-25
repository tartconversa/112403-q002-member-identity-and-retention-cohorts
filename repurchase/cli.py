"""Command line interface for analysts and campaign operators."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Sequence

from .analysis import diff_analysis, run_analysis
from .campaigns import CampaignService
from .errors import RepurchaseAppError
from .io import load_input
from .reporting import write_analysis_outputs


def _csv_ints(value: str) -> list[int]:
    try:
        return [int(part.strip()) for part in value.split(",") if part.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("horizons must be comma-separated integers") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m repurchase", description=__doc__)
    parser.add_argument("--data", default="samples", help="directory containing identity/consent/orders CSV files")
    sub = parser.add_subparsers(dest="command")

    analyze = sub.add_parser("analyze", help="run cohort/region/category repurchase analysis")
    analyze.add_argument("--as-of", default=None, help="cutoff date or ISO timestamp; default: now")
    analyze.add_argument("--out", default="output/analysis", help="report output directory")
    analyze.add_argument("--horizons", type=_csv_ints, default=[30, 60, 90], help="comma-separated common horizons")
    analyze.add_argument("--cohort-grain", choices=["month", "quarter", "date"], default="month")
    analyze.add_argument("--window-days", type=int, default=90)

    freeze = sub.add_parser("freeze-list", help="freeze an unsent campaign roster at a point in time")
    freeze.add_argument("--list", required=True, dest="list_id")
    freeze.add_argument("--at", default=None)
    freeze.add_argument("--state", default="output/campaign-state")

    send = sub.add_parser("send-list", help="lock the current frozen snapshot as an immutable sent record")
    send.add_argument("--list", required=True, dest="list_id")
    send.add_argument("--at", default=None)
    send.add_argument("--state", default="output/campaign-state")

    review = sub.add_parser("review-lists", help="reverify unsent lists and alert owners for sent lists")
    review.add_argument("--at", default=None)
    review.add_argument("--state", default="output/campaign-state")
    review.add_argument("--write-analysis", default="output/analysis", help="directory for refreshed analysis reports")
    return parser


def _load_or_error(data_dir: str):
    path = Path(data_dir)
    if not path.exists():
        raise RepurchaseAppError(f"data directory does not exist: {path}")
    return load_input(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    try:
        data = _load_or_error(args.data)

        if args.command == "analyze":
            result = run_analysis(
                data,
                as_of=args.as_of,
                horizons=args.horizons,
                cohort_grain=args.cohort_grain,
                window_days=args.window_days,
            )
            out = Path(args.out)
            previous = out / "analysis.json"
            diff = None
            if previous.exists():
                try:
                    diff = diff_analysis(json.loads(previous.read_text(encoding="utf-8")), result)
                except (json.JSONDecodeError, KeyError):
                    diff = None
            paths = write_analysis_outputs(result, out, diff)
            print(json.dumps({"summary": result.summary, "paths": {k: str(v) for k, v in paths.items()}}, ensure_ascii=False, indent=2))
            return 0

        service = CampaignService(data, args.state)
        if args.command == "freeze-list":
            snapshot = service.freeze_list(args.list_id, args.at)
            print(json.dumps({"snapshot_id": snapshot["snapshot_id"], "summary": snapshot["summary"], "state_dir": args.state}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "send-list":
            record = service.mark_sent(args.list_id, args.at)
            print(json.dumps({"sent_at": record["sent_at"], "snapshot_id": record["snapshot_id"], "locked": True}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "review-lists":
            result = run_analysis(data, as_of=args.at)
            report = service.review_lists(args.at, analysis_result=result)
            if args.write_analysis:
                alerts_by_impact: dict[str, list[dict]] = {}
                for alert in report["alerts"]:
                    for related in alert.get("related_impacts", []):
                        impact_id = related.get("impact_id")
                        if impact_id:
                            alerts_by_impact.setdefault(impact_id, []).append(alert)
                for impact in result.impacts:
                    related_alerts = alerts_by_impact.get(impact["impact_id"], [])
                    if related_alerts:
                        impact["affected_list_ids"] = sorted({a["list_id"] for a in related_alerts})
                        impact["owners"] = sorted({a.get("owner") or "unassigned" for a in related_alerts})
                out = Path(args.write_analysis)
                previous = out / "analysis.json"
                diff = None
                if previous.exists():
                    try:
                        diff = diff_analysis(json.loads(previous.read_text(encoding="utf-8")), result)
                    except (json.JSONDecodeError, KeyError):
                        diff = None
                write_analysis_outputs(result, out, diff)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        parser.error(f"unknown command {args.command}")
        return 2
    except (RepurchaseAppError, FileNotFoundError, KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
