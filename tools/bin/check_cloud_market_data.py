#!/usr/bin/env python3
import argparse
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path


ROOT = Path(os.environ.get("A_SHARE_AGENT_ROOT", Path(__file__).resolve().parents[2])).resolve()
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


def validate_package(cloud_root, trade_date):
    day_dir = Path(cloud_root) / trade_date
    data_path = day_dir / "data.json"
    manifest_path = day_dir / "manifest.json"
    problems = []
    payload = load_json(data_path)
    manifest = load_json(manifest_path)
    if payload is None:
        problems.append(f"缺少或无法读取 data.json: {data_path}")
    if manifest is None:
        problems.append(f"缺少或无法读取 manifest.json: {manifest_path}")
    if payload:
        if payload.get("trade_date") != trade_date:
            problems.append("data.json trade_date 不匹配")
        score = payload.get("confidence_score")
        if not isinstance(score, (int, float)) or not 0 <= score <= 100:
            problems.append("confidence_score 非 0-100 数值")
        if not isinstance(payload.get("source_manifest"), dict):
            problems.append("source_manifest 缺失")
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
    }


def main():
    load_env_file()
    parser = argparse.ArgumentParser(description="检查 A股 Agent 云端数据层标准包状态")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--cloud-root", default=str(DEFAULT_CLOUD_ROOT))
    parser.add_argument("--date", default="")
    parser.add_argument("--fail-on-missing", action="store_true")
    args = parser.parse_args()

    trade_date = args.date or latest_formal_trade_date(args.db)
    if not trade_date:
        result = {"ok": False, "status": "no_formal_trade_date", "generated_at": datetime.now().isoformat(timespec="seconds")}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(1 if args.fail_on_missing else 0)
    package = validate_package(args.cloud_root, trade_date)
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
