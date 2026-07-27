#!/usr/bin/env python3
"""Verify and replay a downloaded cloud P1 package without network or DB writes."""

import argparse
import hashlib
import importlib.util
import json
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CLOUD_ROOT = ROOT / "reports" / "cloud-data"
DEFAULT_REPORT_ROOT = ROOT / "reports" / "cloud-replay"


def load_checker():
    path = Path(__file__).with_name("check_cloud_market_data.py")
    spec = importlib.util.spec_from_file_location("check_cloud_market_data", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def canonical_fingerprint(payload):
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def replay_package(cloud_root, trade_date, require_capital_related=True):
    checker = load_checker()
    package = checker.validate_package(
        cloud_root, trade_date, require_capital_related=require_capital_related
    )
    data_path = Path(cloud_root) / trade_date / "data.json"
    payload = checker.load_json(data_path) or {}
    receipt_path = Path(cloud_root) / trade_date / "replay.json"
    retrieval = checker.load_json(receipt_path) or {}
    result = {
        "ok": bool(package["ok"]),
        "trade_date": trade_date,
        "replayed_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "offline_local_package",
        "package_fingerprint": canonical_fingerprint(payload) if payload else "",
        "manifest_integrity": not any(
            "manifest" in problem for problem in package.get("problems") or []
        ),
        "capital_related_ready": bool(
            package.get("capital_related_quality", {}).get("ok")
        ),
        "source_manifest_verified": bool(
            retrieval.get("source_manifest_verified")
        ),
        "package": package,
        "boundary": "read_only_replay_no_formal_database_write",
    }
    return result


def main():
    parser = argparse.ArgumentParser(description="离线重放并校验云端 P1 数据包")
    parser.add_argument("--cloud-root", default=str(DEFAULT_CLOUD_ROOT))
    parser.add_argument("--date", required=True)
    parser.add_argument("--output", default="")
    parser.add_argument("--allow-incomplete-capital", action="store_true")
    args = parser.parse_args()
    result = replay_package(
        Path(args.cloud_root),
        args.date,
        require_capital_related=not args.allow_incomplete_capital,
    )
    output = Path(args.output) if args.output else DEFAULT_REPORT_ROOT / f"{args.date}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
