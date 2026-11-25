"""CSV, JSON, and Markdown reporting."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from .analysis import AnalysisResult
from .models import CustomerMetric


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    if fieldnames is None:
        fieldnames = list(rows[0].keys()) if rows else []
    if not fieldnames:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fieldnames})


CUSTOMER_FIELDS = [field.name for field in fields(CustomerMetric)]
SEGMENT_FIELDS = [
    "cohort_period", "region", "category", "customers_total", "member_accounts",
    "no_valid_order_customers", "first_order_customers", "repeat_customers",
    "no_repeat_customers", "observing_customers", "indeterminate_customers",
    "identity_unresolved_customers", "mature_customers", "window_open_customers",
    "pending_customers", "early_repeat_window_open", "immature_share",
    "decided_customers", "repurchase_rate_decided",
    "repurchase_rate_decided_90d", "repurchase_rate_lower_bound_90d",
    "repurchase_rate_upper_bound_90d", "consent_reachable_customers",
    "min_observed_days", "max_observed_days", "complete_observation_days",
    "common_horizon_days",
]
COMMON_FIELDS = [
    "cohort_period", "region", "category", "horizon_days", "eligible_customers",
    "repeat_customers", "certain_no_repeat_customers", "indeterminate_customers",
    "repurchase_rate_common_period", "lower_bound_rate", "upper_bound_rate",
]
IMPACT_FIELDS = [
    "impact_id", "trigger_type", "severity", "subject", "person", "channel",
    "effective_at", "order_id", "customer_id", "member_ids",
    "possible_person_ids", "segment_cohort", "segment_region", "segment_category",
    "affected_customer_metric", "affected_segment_metrics", "affected_list_ids",
    "owners", "owner", "detail", "possible_extra_orders", "unbounded_identity",
]
DIFF_FIELDS = ["level", "id", "field", "before", "after"]


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple, set)):
        return ";".join(str(v) for v in value)
    return value


def write_analysis_outputs(result: AnalysisResult, out_dir: str | Path, diff: dict[str, Any] | None = None) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": out / "analysis.json",
        "customers": out / "customer_metrics.csv",
        "segments": out / "segment_metrics.csv",
        "common": out / "common_period_comparison.csv",
        "impacts": out / "impact_register.csv",
        "report": out / "analysis_report.md",
    }
    paths["json"].write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
    _write_csv(paths["customers"], [asdict(c) for c in result.customers], CUSTOMER_FIELDS)
    _write_csv(paths["segments"], result.segments, SEGMENT_FIELDS)
    _write_csv(paths["common"], result.common_period, COMMON_FIELDS)
    _write_csv(paths["impacts"], result.impacts, IMPACT_FIELDS)
    paths["report"].write_text(render_markdown(result), encoding="utf-8")
    if diff is not None:
        diff_path = out / "analysis_diff.json"
        diff_path.write_text(json.dumps(diff, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
        flat_changes = []
        for change in diff.get("customer_changes", []):
            for field, values in change["changes"].items():
                flat_changes.append({"level": "customer", "id": change["customer_id"], "field": field, **values})
        for change in diff.get("segment_changes", []):
            segment = "/".join(change["segment"])
            for field, values in change["changes"].items():
                flat_changes.append({"level": "segment", "id": segment, "field": field, **values})
        _write_csv(out / "analysis_diff.csv", flat_changes, DIFF_FIELDS)
        paths["diff_json"] = diff_path
        paths["diff_csv"] = out / "analysis_diff.csv"
    return paths


def _pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.1f}%"


def render_markdown(result: AnalysisResult) -> str:
    s = result.summary
    lines = [
        "# 全渠道会员复购分析",
        "",
        f"- 观察截止：**{result.as_of}**",
        f"- 复购窗口：首次有效订单后 **{result.window_days} 天**；第 {result.window_days} 天当天计入。",
        f"- 数据指纹：`{result.data_hash}`",
        "",
        "## 总览",
        "",
        f"- 顾客/身份组：{s['customers_total']}（会员账号 {s['member_accounts']} 个）",
        f"- 已有有效首单：{s['first_order_customers']}",
        f"- 已满 {result.window_days} 天观察期：{s['mature_customers']}",
        f"- 窗口未满：{s['window_open_customers']}，其中尚未复购、需继续观察：{s['pending_customers']}",
        f"- 已确认复购：{s['repeat_customers']}",
        f"- 满期且确认未复购：{s['no_repeat_customers']}",
        f"- 身份存疑导致无法判定：{s['indeterminate_customers']}",
        f"- 身份关系未完全确认的顾客：{s['identity_unresolved_customers']}",
        f"- 影响登记：{s['impact_count']} 条",
        "",
        "> 无法确认跨渠道身份时，顾客进入 `indeterminate` 或继续观察，不会被计为确定未复购。",
        "",
        "## 群组指标（入会期 / 地区 / 首单品类）",
        "",
        "| 入会期 | 地区 | 品类 | 顾客 | 首单 | 复购 | 确定未复购 | 身份待定 | 满期 | 未成熟 | 90天确定口径 | 下界 | 上界 | 群组完整观察天数 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in result.segments:
        lines.append(
            "| {cohort_period} | {region} | {category} | {customers_total} | {first_order_customers} | {repeat_customers} | "
            "{no_repeat_customers} | {indeterminate_customers} | {mature_customers} | {window_open_customers} | "
            "{decided} | {lower} | {upper} | {min_obs} |".format(
                cohort_period=row["cohort_period"],
                region=row["region"],
                category=row["category"],
                customers_total=row["customers_total"],
                first_order_customers=row["first_order_customers"],
                repeat_customers=row["repeat_customers"],
                no_repeat_customers=row["no_repeat_customers"],
                indeterminate_customers=row["indeterminate_customers"],
                mature_customers=row["mature_customers"],
                window_open_customers=row["window_open_customers"],
                decided=_pct(row["repurchase_rate_decided_90d"]),
                lower=_pct(row["repurchase_rate_lower_bound_90d"]),
                upper=_pct(row["repurchase_rate_upper_bound_90d"]),
                min_obs=row.get("complete_observation_days", row.get("min_observed_days")) if row.get("complete_observation_days", row.get("min_observed_days")) is not None else "—",
            )
        )

    lines.extend(["", "## 共同观察期比较", "", "仅让首单后已满 H 天的顾客进入 H 天口径；身份待定者同时给出比率区间。", ""])
    for horizon in sorted({row["horizon_days"] for row in result.common_period}):
        lines.extend([f"### {horizon} 天共同窗口", "", "| 入会期 | 地区 | 品类 | 已满H天 | 复购 | 确定未复购 | 身份待定 | 共同期比率 | 下界 | 上界 |", "|---|---|---|---:|---:|---:|---:|---:|---:|---:|"])
        for row in [r for r in result.common_period if r["horizon_days"] == horizon]:
            lines.append(
                f"| {row['cohort_period']} | {row['region']} | {row['category']} | {row['eligible_customers']} | "
                f"{row['repeat_customers']} | {row['certain_no_repeat_customers']} | {row['indeterminate_customers']} | "
                f"{_pct(row['repurchase_rate_common_period'])} | {_pct(row['lower_bound_rate'])} | {_pct(row['upper_bound_rate'])} |"
            )
        lines.append("")

    high_impacts = [i for i in result.impacts if i.get("severity") == "high"]
    lines.extend(["## 重点影响登记", ""])
    if not high_impacts:
        lines.append("无高严重度身份、授权或退款影响。")
    else:
        for impact in high_impacts:
            segment = "/".join(str(x) for x in [impact.get("segment_cohort"), impact.get("segment_region"), impact.get("segment_category")] if x)
            lines.append(f"- `{impact['impact_id']}` {impact['trigger_type']} → 顾客 `{impact.get('customer_id')}`；群组 `{segment or '未归属'}`；{impact.get('detail', '')}")
    lines.append("")
    return "\n".join(lines)
