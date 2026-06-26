#!/usr/bin/env python3
import argparse
import hashlib
import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path


TENCENT_INDEX_CODES = {
    "sh_index": "sh000001",
    "sz_index": "sz399001",
    "cyb_index": "sz399006",
}


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def http_get(url, encoding="utf-8", timeout=15):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 a-share-agent-cloud-p1/1.0",
            "Referer": "https://finance.sina.com.cn/",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode(encoding, errors="replace")


def parse_tencent_index(text):
    result = {}
    source_rows = []
    for chunk in text.split(";"):
        chunk = chunk.strip()
        if not chunk or '="' not in chunk:
            continue
        var_name, raw = chunk.split('="', 1)
        symbol = var_name.replace("v_", "").strip()
        body = raw.rsplit('"', 1)[0]
        parts = body.split("~")
        if len(parts) < 33:
            continue
        item = {
            "symbol": symbol,
            "name": parts[1],
            "code": parts[2],
            "latest": safe_float(parts[3]),
            "prev_close": safe_float(parts[4]),
            "open": safe_float(parts[5]),
            "volume": safe_float(parts[6]),
            "time": parts[30] if len(parts) > 30 else "",
            "change": safe_float(parts[31]) if len(parts) > 31 else None,
            "change_pct": safe_float(parts[32]) if len(parts) > 32 else None,
        }
        source_rows.append(item)
        for key, code in TENCENT_INDEX_CODES.items():
            if symbol == code:
                result[key] = item["latest"]
                result[key.replace("_index", "_change_pct")] = item["change_pct"]
    return result, source_rows


def safe_float(value):
    if value in (None, "", "-", "--"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_tencent_indexes():
    symbols = ",".join(TENCENT_INDEX_CODES.values())
    url = f"https://qt.gtimg.cn/q={symbols}"
    text = http_get(url, encoding="gbk")
    parsed, rows = parse_tencent_index(text)
    return {
        "ok": bool(parsed),
        "name": "腾讯行情指数接口",
        "url": url,
        "data": parsed,
        "rows": rows,
        "error": "" if parsed else "未解析到指数数据",
    }


def fetch_sina_indexes():
    symbols = ",".join(TENCENT_INDEX_CODES.values())
    url = f"https://hq.sinajs.cn/list={symbols}"
    text = http_get(url, encoding="gbk")
    rows = []
    parsed = {}
    for chunk in text.split(";"):
        chunk = chunk.strip()
        if not chunk or '="' not in chunk:
            continue
        var_name, raw = chunk.split('="', 1)
        symbol = var_name.replace("var hq_str_", "").strip()
        body = raw.rsplit('"', 1)[0]
        parts = body.split(",")
        if len(parts) < 32:
            continue
        latest = safe_float(parts[3])
        prev_close = safe_float(parts[2])
        change_pct = round((latest - prev_close) / prev_close * 100, 2) if latest is not None and prev_close else None
        item = {
            "symbol": symbol,
            "name": parts[0],
            "latest": latest,
            "prev_close": prev_close,
            "volume": safe_float(parts[8]),
            "amount": safe_float(parts[9]),
            "trade_date": parts[30] if len(parts) > 30 else "",
            "trade_time": parts[31] if len(parts) > 31 else "",
            "change_pct": change_pct,
        }
        rows.append(item)
        for key, code in TENCENT_INDEX_CODES.items():
            if symbol == code:
                parsed.setdefault(key, latest)
                parsed.setdefault(key.replace("_index", "_change_pct"), change_pct)
    return {
        "ok": bool(parsed),
        "name": "新浪指数行情接口",
        "url": url,
        "data": parsed,
        "rows": rows,
        "error": "" if parsed else "未解析到指数数据",
    }


def build_payload(trade_date):
    sources = []
    conflicts = []
    market_index = {
        "sh_index": None,
        "sh_change_pct": None,
        "sz_index": None,
        "sz_change_pct": None,
        "cyb_index": None,
        "cyb_change_pct": None,
        "turnover_amount_billion": None,
        "turnover_delta_billion": None,
    }
    raw_snapshots = []

    for fetcher in (fetch_tencent_indexes, fetch_sina_indexes):
        try:
            result = fetcher()
        except Exception as exc:
            result = {"ok": False, "name": fetcher.__name__, "url": "", "data": {}, "rows": [], "error": str(exc)}
        sources.append({
            "name": result["name"],
            "type": "quote_index",
            "url": result.get("url", ""),
            "ok": result["ok"],
            "error": result.get("error", ""),
            "trade_date": trade_date,
        })
        raw_snapshots.append({
            "name": result["name"],
            "rows": result.get("rows", []),
        })
        for key, value in (result.get("data") or {}).items():
            if value is None:
                continue
            if market_index.get(key) is not None and market_index[key] != value:
                conflicts.append({
                    "field": key,
                    "existing": market_index[key],
                    "incoming": value,
                    "source": result["name"],
                    "adopted": market_index[key],
                    "reason": "优先采用首个成功行情源，冲突保留供 P2 判断。",
                })
                continue
            market_index[key] = value

    confirmed_fields = sum(1 for value in market_index.values() if value is not None)
    ok_sources = sum(1 for source in sources if source.get("ok"))
    confidence = 35 + ok_sources * 15 + min(confirmed_fields, 6) * 3
    confidence = max(0, min(100, confidence))
    data_status = "云端自动采集-收盘基础行情" if ok_sources else "云端自动采集失败-占位包"

    return {
        "trade_date": trade_date,
        "data_layer_version": "cloud-p1-v2-github-actions",
        "generated_at": now_iso(),
        "data_status": data_status,
        "market_index": market_index,
        "market_breadth": {
            "up_count": None,
            "down_count": None,
            "notes": "云端P1当前未接入稳定上涨/下跌家数源，P2必须二次核验。",
        },
        "limit_up_down": {
            "limit_up_count": None,
            "limit_down_count": None,
            "max_board_height": None,
            "max_board_stock_name": "",
            "max_board_stock_code": "",
            "notes": "云端P1当前未接入稳定涨停/跌停/连板高度源，P2必须二次核验。",
        },
        "capital_flow": {
            "inflow_top": [],
            "outflow_top": [],
            "notes": "云端P1当前未接入稳定资金流源，禁止据此推断资金迁移。",
        },
        "theme_candidates": [],
        "leader_candidates": [],
        "hotspot_factors": [],
        "source_manifest": {
            "sources": sources,
            "snapshots": raw_snapshots,
            "boundary": "cloud_p1_data_only_no_trade_advice_no_formal_judgement",
        },
        "data_conflicts": conflicts,
        "confidence_score": confidence,
    }


def write_payload(out_root, payload):
    out_root = Path(out_root)
    trade_date = payload["trade_date"]
    day_dir = out_root / trade_date
    day_dir.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    data_path = day_dir / "data.json"
    manifest_path = day_dir / "manifest.json"
    data_path.write_bytes(raw)
    manifest = {
        "version": 2,
        "trade_date": trade_date,
        "created_at": now_iso(),
        "data_path": str(data_path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "confidence_score": payload.get("confidence_score"),
        "data_status": payload.get("data_status"),
        "source_count": len((payload.get("source_manifest") or {}).get("sources") or []),
        "boundary": "cloud_p1_primary_generated_by_github_actions",
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data_path, manifest_path, manifest


def update_static_index(out_root, latest):
    out_root = Path(out_root)
    entries = []
    for data_path in sorted(out_root.glob("20*/data.json"), reverse=True):
        try:
            data = json.loads(data_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        entries.append({
            "trade_date": data.get("trade_date") or data_path.parent.name,
            "confidence_score": data.get("confidence_score"),
            "data_status": data.get("data_status"),
        })
    latest_info = {
        "trade_date": latest["trade_date"],
        "data_path": f"{latest['trade_date']}/data.json",
        "manifest_path": f"{latest['trade_date']}/manifest.json",
        "confidence_score": latest.get("confidence_score"),
        "data_status": latest.get("data_status"),
    }
    (out_root / "latest.json").write_text(json.dumps(latest_info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    links = "\n".join(
        f'<li><a href="./{item["trade_date"]}/data.json">{item["trade_date"]}</a> '
        f'可信度 {item.get("confidence_score")}｜{item.get("data_status")}</li>'
        for item in entries
    )
    html = f"""<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<title>A股 Agent 云端P1数据包</title>
<body>
<h1>A股 Agent 云端P1数据包</h1>
<p>本目录由 GitHub Actions 云端优先生成。P1 只提供数据证据层，不生成正式复盘，不给交易建议。</p>
<p>URL 模板: <code>https://jackliu333777-hue.github.io/Repository-name-a-share-agent-data/{{trade_date}}/data.json</code></p>
<ul>{links}</ul>
</body>
</html>
"""
    (out_root / "index.html").write_text(html, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="云端优先采集 A股 Agent P1 标准数据包")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--output-root", default="reports/cloud-data-public")
    parser.add_argument("--update-index", action="store_true")
    args = parser.parse_args()
    if len(args.date) != 10:
        raise SystemExit("--date 必须是 YYYY-MM-DD")
    payload = build_payload(args.date)
    data_path, manifest_path, manifest = write_payload(args.output_root, payload)
    if args.update_index:
        update_static_index(args.output_root, payload)
    print(json.dumps({
        "ok": True,
        "trade_date": args.date,
        "data_path": str(data_path),
        "manifest_path": str(manifest_path),
        "manifest": manifest,
    }, ensure_ascii=False, indent=2))
    if payload["confidence_score"] <= 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
