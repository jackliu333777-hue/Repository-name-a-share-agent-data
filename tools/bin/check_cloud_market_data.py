#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path


ROOT = Path(os.environ.get("A_SHARE_AGENT_ROOT", Path(__file__).resolve().parents[2])).resolve()
TOOLS_DIR = Path(__file__).resolve().parents[1]
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from a_share_agent.evidence import read_cloud_evidence  # noqa: E402
from a_share_agent.stock_policy import is_restricted_stock_code  # noqa: E402

DEFAULT_DB = ROOT / "data" / "stock.db"
DEFAULT_CLOUD_ROOT = ROOT / "reports" / "cloud-data"
DEFAULT_CONFIG = ROOT / "config" / "cloud-data.env"


def load_env_file(path=DEFAULT_CONFIG):
    path = Path(path)
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def latest_formal_trade_date(db_path):
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            """
            SELECT trade_date
            FROM agent_market_daily
            WHERE COALESCE(first_main_theme, '') NOT IN ('', '无法确认')
              AND COALESCE(summary, '') NOT LIKE '%休市%'
              AND COALESCE(source_notes, '') NOT LIKE '%休市%'
            ORDER BY trade_date DESC
            LIMIT 1
            """
        ).fetchone()
    return row[0] if row else ""


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def validate_manifest_integrity(data_path, manifest, trade_date):
    problems = []
    if not isinstance(manifest, dict):
        return ["manifest.json 必须是 object"]
    if manifest.get("trade_date") != trade_date:
        problems.append("manifest trade_date 不匹配")
    if not data_path.is_file():
        return problems
    raw = data_path.read_bytes()
    expected_sha = manifest.get("sha256")
    if not expected_sha:
        problems.append("manifest sha256 缺失")
    elif hashlib.sha256(raw).hexdigest() != expected_sha:
        problems.append("manifest sha256 校验失败")
    expected_bytes = manifest.get("bytes")
    if not isinstance(expected_bytes, int):
        problems.append("manifest bytes 缺失或非法")
    elif len(raw) != expected_bytes:
        problems.append("manifest bytes 校验失败")
    return problems


def capital_related_quality(payload):
    capital = payload.get("capital_flow") if isinstance(payload, dict) else {}
    if not isinstance(capital, dict):
        return {"ok": False, "status": "missing", "problems": ["capital_flow 必须是 object"], "directions": []}
    problems = []
    directions = []
    inflow = capital.get("inflow_top") or []
    outflow = capital.get("outflow_top") or []
    coverage = capital.get("market_coverage") or {}
    actual_negative_count = coverage.get("negative_count")
    total_board_count = coverage.get("total_board_count")
    outflow_market_complete = (
        isinstance(total_board_count, int)
        and total_board_count >= 20
        and isinstance(actual_negative_count, int)
        and actual_negative_count < 3
        and len(outflow) == actual_negative_count
    )
    if len(inflow) < 3:
        problems.append("资金流入Top3不足")
    if len(outflow) < 3 and not outflow_market_complete:
        problems.append("资金流出Top3不足")
    for kind, rows in (("流入", inflow[:3]), ("流出", outflow[:3])):
        for row in rows:
            name = row.get("target_name") or "无法确认"
            related = row.get("related_stocks") or []
            count = len(related) if isinstance(related, list) else 0
            restricted = [
                f"{stock.get('name') or ''}({stock.get('code') or stock.get('stock_code') or ''})"
                for stock in related
                if isinstance(stock, dict) and is_restricted_stock_code(stock.get("code") or stock.get("stock_code"))
            ] if isinstance(related, list) else []
            item = {
                "flow_type": kind,
                "target_name": name,
                "related_stock_count": count,
                "restricted_stock_count": len(restricted),
                "ok": 3 <= count <= 5 and not restricted,
            }
            directions.append(item)
            if restricted:
                problems.append(f"{kind} {name} 含无权限标的: {', '.join(restricted)}")
            if not (3 <= count <= 5):
                problems.append(f"{kind} {name} 关联标的数量{count}，要求3-5")
    return {
        "ok": not problems,
        "status": "ready" if not problems else "insufficient",
        "problems": problems,
        "directions": directions,
        "requirement": capital.get("related_stock_requirement") or {},
        "market_coverage": coverage,
        "outflow_market_complete": outflow_market_complete,
    }


def validate_package(cloud_root, trade_date, require_capital_related=False):
    day_dir = Path(cloud_root) / trade_date
    data_path = day_dir / "data.json"
    manifest_path = day_dir / "manifest.json"
    problems = []
    payload = load_json(data_path)
    manifest = load_json(manifest_path)
    evidence = read_cloud_evidence(data_path, expected_trade_date=trade_date)["evidence"]
    if payload is None:
        problems.append(f"缺少或无法读取 data.json: {data_path}")
    if manifest is None:
        problems.append(f"缺少或无法读取 manifest.json: {manifest_path}")
    else:
        problems.extend(validate_manifest_integrity(data_path, manifest, trade_date))
    if evidence["status"] != "verified":
        problems.extend(f"evidence:{issue}" for issue in evidence["issues"])
    if payload:
        if payload.get("trade_date") != trade_date:
            problems.append("data.json trade_date 不匹配")
        score = payload.get("confidence_score")
        if not isinstance(score, (int, float)) or not 0 <= score <= 100:
            problems.append("confidence_score 非 0-100 数值")
        if not isinstance(payload.get("source_manifest"), dict):
            problems.append("source_manifest 缺失")
        capital_quality = capital_related_quality(payload)
        if require_capital_related:
            if not capital_quality["ok"]:
                problems.extend(capital_quality["problems"])
            capital_sources = [
                source
                for source in (payload.get("source_manifest") or {}).get("sources") or []
                if isinstance(source, dict)
                and source.get("type") == "capital_flow"
                and source.get("ok") is True
            ]
            if not any(
                source.get("date_verified") is True
                and source.get("trade_date") == trade_date
                for source in capital_sources
            ):
                problems.append("资金流来源交易日未验证")
    else:
        capital_quality = {"ok": False, "status": "missing", "problems": ["payload 缺失"], "directions": []}
    return {
        "trade_date": trade_date,
        "data_path": str(data_path),
        "manifest_path": str(manifest_path),
        "exists": data_path.exists() and manifest_path.exists(),
        "ok": not problems,
        "problems": problems,
        "confidence_score": payload.get("confidence_score") if isinstance(payload, dict) else None,
        "data_status": payload.get("data_status") if isinstance(payload, dict) else "",
        "source_count": len((payload.get("source_manifest") or {}).get("sources") or []) if isinstance(payload, dict) else 0,
        "capital_related_quality": capital_quality,
        "evidence": evidence,
    }


def main():
    load_env_file()
    parser = argparse.ArgumentParser(description="检查 A股 Agent 云端数据层标准包状态")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--cloud-root", default=str(DEFAULT_CLOUD_ROOT))
    parser.add_argument("--date", default="")
    parser.add_argument("--fail-on-missing", action="store_true")
    parser.add_argument("--require-capital-related", action="store_true", help="要求资金流入Top3及最多3个真实净流出方向，每方向都有3-5只关联标的")
    args = parser.parse_args()

    trade_date = args.date or latest_formal_trade_date(args.db)
    if not trade_date:
        result = {"ok": False, "status": "no_formal_trade_date", "generated_at": datetime.now().isoformat(timespec="seconds")}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(1 if args.fail_on_missing else 0)
    package = validate_package(args.cloud_root, trade_date, require_capital_related=args.require_capital_related)
    result = {
        "ok": package["ok"] or (not args.fail_on_missing and not package["exists"]),
        "strict_ok": package["ok"],
        "status": "ready" if package["ok"] else ("missing" if not package["exists"] else "invalid"),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "cloud_root": str(Path(args.cloud_root)),
        "latest_formal_trade_date": trade_date,
        "package": package,
        "configured": bool(os.environ.get("A_SHARE_CLOUD_DATA_URL_TEMPLATE", "").strip()) or package["exists"],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
