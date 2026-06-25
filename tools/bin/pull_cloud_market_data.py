#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import shutil
import sys
import urllib.request
from datetime import datetime
from pathlib import Path


ROOT = Path(os.environ.get("A_SHARE_AGENT_ROOT", Path(__file__).resolve().parents[2])).resolve()
DEFAULT_OUTPUT_ROOT = ROOT / "reports" / "cloud-data"
DEFAULT_INBOX = ROOT / "reports" / "cloud-data-inbox"
DEFAULT_CONFIG = ROOT / "config" / "cloud-data.env"
REQUIRED_TOP_LEVEL = {
    "trade_date",
    "data_layer_version",
    "generated_at",
    "data_status",
    "market_index",
    "market_breadth",
    "limit_up_down",
    "capital_flow",
    "theme_candidates",
    "leader_candidates",
    "hotspot_factors",
    "source_manifest",
    "data_conflicts",
    "confidence_score",
}


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


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def today_et():
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/Toronto")).date().isoformat()
    except Exception:
        return datetime.now().date().isoformat()


def read_source(source):
    if source.startswith("http://") or source.startswith("https://"):
        req = urllib.request.Request(source, headers={"User-Agent": "a-share-agent-cloud-data/1.0"})
        with urllib.request.urlopen(req, timeout=45) as res:
            return res.read(), source
    path = Path(source).expanduser()
    if not path.exists():
        raise SystemExit(f"云端数据包不存在: {path}")
    return path.read_bytes(), str(path)


def infer_source(args):
    if args.source:
        return args.source
    template = os.environ.get("A_SHARE_CLOUD_DATA_URL_TEMPLATE", "").strip()
    if template:
        return template.format(trade_date=args.date, yyyymmdd=args.date.replace("-", ""))
    local = DEFAULT_INBOX / args.date / "data.json"
    if local.exists():
        return str(local)
    return ""


def validate_payload(payload, expected_date):
    if not isinstance(payload, dict):
        raise SystemExit("云端数据包必须是 JSON object")
    missing = sorted(REQUIRED_TOP_LEVEL - set(payload))
    if missing:
        raise SystemExit("云端数据包缺少字段: " + ", ".join(missing))
    if payload.get("trade_date") != expected_date:
        raise SystemExit(f"云端数据包日期不匹配: {payload.get('trade_date')} != {expected_date}")
    score = payload.get("confidence_score")
    if not isinstance(score, (int, float)) or not (0 <= score <= 100):
        raise SystemExit("confidence_score 必须是 0-100 数值")
    manifest = payload.get("source_manifest")
    if not isinstance(manifest, dict):
        raise SystemExit("source_manifest 必须是 object")
    sources = manifest.get("sources", [])
    if not isinstance(sources, list):
        raise SystemExit("source_manifest.sources 必须是数组")
    return missing


def write_package(args, payload, raw_bytes, source_ref):
    out_root = Path(args.output_root)
    day_dir = out_root / args.date
    day_dir.mkdir(parents=True, exist_ok=True)
    digest = sha256_bytes(raw_bytes)
    data_path = day_dir / "data.json"
    manifest_path = day_dir / "manifest.json"
    data_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "version": 1,
        "trade_date": args.date,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source": source_ref,
        "data_path": str(data_path),
        "sha256": digest,
        "bytes": len(raw_bytes),
        "confidence_score": payload.get("confidence_score"),
        "data_status": payload.get("data_status"),
        "source_count": len((payload.get("source_manifest") or {}).get("sources") or []),
        "boundary": "cloud_data_layer_only_no_formal_judgement",
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "data_path": str(data_path), "manifest_path": str(manifest_path), "manifest": manifest}


def main():
    load_env_file()
    parser = argparse.ArgumentParser(description="拉取/落盘 A股 Agent 云端数据层标准包，不写正式复盘库")
    parser.add_argument("--date", default=today_et(), help="YYYY-MM-DD，默认 America/Toronto 今日")
    parser.add_argument("--source", default="", help="本地 JSON 文件或 HTTPS URL；为空时使用 A_SHARE_CLOUD_DATA_URL_TEMPLATE")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--allow-missing", action="store_true", help="未配置来源时返回 ok=false 但退出码 0")
    args = parser.parse_args()

    if not args.date or len(args.date) != 10:
        raise SystemExit("--date 必须是 YYYY-MM-DD")
    source = infer_source(args)
    if not source:
        result = {
            "ok": False,
            "status": "not_configured",
            "date": args.date,
            "message": "未配置云端数据源；设置 A_SHARE_CLOUD_DATA_URL_TEMPLATE 或传入 --source",
            "output_root": str(Path(args.output_root)),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0 if args.allow_missing else 2)

    try:
        raw, source_ref = read_source(source)
        payload = json.loads(raw.decode("utf-8"))
        validate_payload(payload, args.date)
    except SystemExit as exc:
        if args.allow_missing:
            result = {
                "ok": False,
                "status": "fetch_failed",
                "date": args.date,
                "source": source,
                "message": str(exc),
                "output_root": str(Path(args.output_root)),
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
            raise SystemExit(0)
        raise
    except Exception as exc:
        if args.allow_missing:
            result = {
                "ok": False,
                "status": "fetch_failed",
                "date": args.date,
                "source": source,
                "message": str(exc),
                "output_root": str(Path(args.output_root)),
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
            raise SystemExit(0)
        if isinstance(exc, json.JSONDecodeError):
            raise SystemExit(f"云端数据包不是有效 JSON: {exc}")
        raise
    result = write_package(args, payload, raw, source_ref)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
