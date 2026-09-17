#!/usr/bin/env python3
"""Small operating scorecard for Model Garden's first client engagements.

The input is deliberately a plain YAML file so it can live in the operator's private
business-operations workspace without introducing a CRM, ERP, or custom billing system.
No customer secrets belong in it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
from typing import Any

import yaml

EFFORT_FIELDS = ("sales", "discovery", "implementation", "tuning", "support")
COST_FIELDS = ("model", "voice", "hosting", "connectors", "other")
WORK_CLASSES = ("reusable", "configurable", "bespoke")


def _number(value: Any, label: str) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    return float(value)


def _date(value: Any, label: str) -> dt.date | None:
    if value in (None, ""):
        return None
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{label} must be YYYY-MM-DD") from exc


def load(path: pathlib.Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("scorecard must be a YAML mapping")
    clients = value.get("clients")
    if not isinstance(clients, list):
        raise ValueError("scorecard.clients must be a list")
    return value


def summarize(document: dict[str, Any]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    total_revenue = total_cash = total_direct_cost = 0.0
    for index, client in enumerate(document["clients"]):
        if not isinstance(client, dict):
            raise ValueError(f"clients[{index}] must be a mapping")
        slug = client.get("client")
        if not isinstance(slug, str) or not slug.strip():
            raise ValueError(f"clients[{index}].client must be non-empty")
        revenue = _number(client.get("revenue"), f"{slug}.revenue")
        cash = _number(client.get("cash_collected"), f"{slug}.cash_collected")
        effort = client.get("effort_hours") or {}
        costs = client.get("provider_costs") or {}
        work = client.get("work_hours") or {}
        if not isinstance(effort, dict) or not isinstance(costs, dict) or not isinstance(work, dict):
            raise ValueError(f"{slug}: effort_hours/provider_costs/work_hours must be mappings")
        loaded_rate = _number(client.get("loaded_hourly_cost"), f"{slug}.loaded_hourly_cost")
        total_hours = sum(_number(effort.get(name), f"{slug}.effort_hours.{name}") for name in EFFORT_FIELDS)
        labour_cost = total_hours * loaded_rate
        provider_cost = sum(_number(costs.get(name), f"{slug}.provider_costs.{name}") for name in COST_FIELDS)
        direct_cost = labour_cost + provider_cost + _number(client.get("other_direct_cost"), f"{slug}.other_direct_cost")
        gross_profit = revenue - direct_cost
        gross_margin = gross_profit / revenue if revenue else None
        signed = _date(client.get("signed_date"), f"{slug}.signed_date")
        dev = _date(client.get("working_dev_date"), f"{slug}.working_dev_date")
        prod = _date(client.get("production_date"), f"{slug}.production_date")
        time_to_dev = (dev - signed).days if signed and dev else None
        time_to_prod = (prod - signed).days if signed and prod else None
        classified_hours = {name: _number(work.get(name), f"{slug}.work_hours.{name}") for name in WORK_CLASSES}
        classified_total = sum(classified_hours.values())
        reuse_ratio = (
            (classified_hours["reusable"] + classified_hours["configurable"]) / classified_total
            if classified_total else None
        )
        receivable = max(revenue - cash, 0.0)
        results.append({
            "client": slug,
            "revenue": revenue,
            "cashCollected": cash,
            "receivable": receivable,
            "effortHours": total_hours,
            "directCost": direct_cost,
            "grossProfit": gross_profit,
            "grossMargin": gross_margin,
            "timeToWorkingDevDays": time_to_dev,
            "timeToProductionDays": time_to_prod,
            "reusableOrConfigurableRatio": reuse_ratio,
            "baselineMetricRecorded": bool(client.get("baseline_metric")),
            "postLaunchMetricRecorded": bool(client.get("post_launch_metric")),
            "nextExpansionQuantified": bool(client.get("next_expansion")),
        })
        total_revenue += revenue
        total_cash += cash
        total_direct_cost += direct_cost
    largest_share = max((row["revenue"] for row in results), default=0.0) / total_revenue if total_revenue else None
    return {
        "clients": results,
        "business": {
            "revenue": total_revenue,
            "cashCollected": total_cash,
            "receivables": max(total_revenue - total_cash, 0.0),
            "directCost": total_direct_cost,
            "grossProfit": total_revenue - total_direct_cost,
            "grossMargin": (total_revenue - total_direct_cost) / total_revenue if total_revenue else None,
            "largestClientRevenueShare": largest_share,
            "signedDeliveryBacklog": _number(document.get("signed_delivery_backlog"), "signed_delivery_backlog"),
            "availableDeliveryCapacityHours": _number(document.get("available_delivery_capacity_hours"), "available_delivery_capacity_hours"),
        },
    }


def template() -> str:
    return """version: 1
signed_delivery_backlog: 0
available_delivery_capacity_hours: 0
clients:
  - client: example-client
    signed_date:
    working_dev_date:
    production_date:
    revenue: 0
    cash_collected: 0
    loaded_hourly_cost: 0
    effort_hours:
      sales: 0
      discovery: 0
      implementation: 0
      tuning: 0
      support: 0
    provider_costs:
      model: 0
      voice: 0
      hosting: 0
      connectors: 0
      other: 0
    other_direct_cost: 0
    work_hours:
      reusable: 0
      configurable: 0
      bespoke: 0
    baseline_metric:
    post_launch_metric:
    next_expansion:
    case_study_permission: false
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("path", type=pathlib.Path)
    summary = sub.add_parser("summary")
    summary.add_argument("path", type=pathlib.Path)
    summary.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "init":
            if args.path.exists():
                raise ValueError(f"refusing to overwrite {args.path}")
            args.path.parent.mkdir(parents=True, exist_ok=True)
            args.path.write_text(template(), encoding="utf-8")
            print(f"Created {args.path}")
            return 0
        result = summarize(load(args.path))
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            business = result["business"]
            print(f"Clients: {len(result['clients'])}")
            print(f"Revenue: {business['revenue']:.2f}")
            print(f"Cash collected: {business['cashCollected']:.2f}")
            print(f"Receivables: {business['receivables']:.2f}")
            margin = business["grossMargin"]
            print("Gross margin: n/a" if margin is None else f"Gross margin: {margin:.1%}")
            concentration = business["largestClientRevenueShare"]
            print("Largest-client revenue share: n/a" if concentration is None else f"Largest-client revenue share: {concentration:.1%}")
        return 0
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"Operating scorecard failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
