"""Machine-readable evidence contracts for external and cloud data."""

import json
from pathlib import Path

from .stock_policy import is_allowed_stock_code


def unavailable_cloud_evidence(path, expected_trade_date, issue, status="missing"):
    return {
        "status": status,
        "source_mode": "cloud_package",
        "trade_date": expected_trade_date or "",
        "package_path": str(path),
        "confidence_score": None,
        "source_count": 0,
        "successful_source_count": 0,
        "failed_source_count": 0,
        "issues": [issue],
    }


def cloud_evidence_contract(payload, path, expected_trade_date=""):
    """Summarize cloud-package provenance without making market judgments."""
    sources = (payload.get("source_manifest") or {}).get("sources") or []
    sources = [row for row in sources if isinstance(row, dict)]
    successful = [row for row in sources if row.get("ok") is True]
    failed = [row for row in sources if row.get("ok") is False]
    trade_date = str(payload.get("trade_date") or "")
    confidence = payload.get("confidence_score")
    issues = []
    warnings = []
    if expected_trade_date and trade_date != str(expected_trade_date):
        issues.append("trade_date_mismatch")
    if not sources:
        issues.append("source_manifest_empty")
    elif not successful:
        issues.append("no_successful_source")
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 100:
        issues.append("confidence_score_invalid")
    if failed:
        warnings.append("partial_source_failure")
    status = "verified" if not issues else "degraded"
    return {
        "status": status,
        "source_mode": "cloud_package",
        "trade_date": trade_date or str(expected_trade_date or ""),
        "package_path": str(path),
        "confidence_score": confidence if isinstance(confidence, (int, float)) else None,
        "source_count": len(sources),
        "successful_source_count": len(successful),
        "failed_source_count": len(failed),
        "issues": issues,
        "warnings": warnings,
    }


def read_cloud_evidence(path, expected_trade_date=""):
    """Read a cloud package and always return an explicit evidence state."""
    path = Path(path)
    if not path.exists():
        return {
            "payload": {},
            "evidence": unavailable_cloud_evidence(
                path, expected_trade_date, "cloud_package_missing"
            ),
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "payload": {},
            "evidence": unavailable_cloud_evidence(
                path,
                expected_trade_date,
                f"cloud_package_invalid:{type(exc).__name__}",
                status="invalid",
            ),
        }
    if not isinstance(payload, dict):
        return {
            "payload": {},
            "evidence": unavailable_cloud_evidence(
                path, expected_trade_date, "cloud_package_not_object", status="invalid"
            ),
        }
    return {
        "payload": payload,
        "evidence": cloud_evidence_contract(payload, path, expected_trade_date),
    }


def normalize_capital_rows(rows):
    """Apply access filtering and attach completeness to each capital direction."""
    if not isinstance(rows, list):
        return []
    cleaned = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        related = row.get("related_stocks") or []
        if isinstance(related, list):
            related = [
                stock
                for stock in related
                if isinstance(stock, dict)
                and is_allowed_stock_code(stock.get("code") or stock.get("stock_code"))
            ][:5]
        else:
            related = []
        cleaned.append(
            {
                **row,
                "related_stocks": related,
                "related_stock_count": len(related),
                "related_stock_requirement_status": (
                    "满足" if 3 <= len(related) <= 5 else "不足"
                ),
            }
        )
    return cleaned


def capital_evidence_contract(capital_flow, inflow_rows, outflow_rows):
    sources = sorted(
        {
            str(row.get("source"))
            for row in inflow_rows + outflow_rows
            if row.get("source")
        }
    )
    directions = inflow_rows[:3] + outflow_rows[:3]
    complete = len(inflow_rows) >= 3 and len(outflow_rows) >= 3 and all(
        row.get("related_stock_requirement_status") == "满足" for row in directions
    )
    if not inflow_rows and not outflow_rows:
        status = "unavailable"
    elif complete:
        status = "verified"
    else:
        status = "degraded"
    return {
        "status": status,
        "source_mode": "cloud_package",
        "sources": sources,
        "inflow_direction_count": len(inflow_rows),
        "outflow_direction_count": len(outflow_rows),
        "complete_direction_count": sum(
            1
            for row in directions
            if row.get("related_stock_requirement_status") == "满足"
        ),
        "issues": [] if complete else ["capital_flow_incomplete"],
        "notes": capital_flow.get("notes") or "",
    }
