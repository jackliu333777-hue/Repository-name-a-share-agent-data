#!/usr/bin/env python3
import argparse
import hashlib
import json
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode


TOOLS_DIR = Path(__file__).resolve().parents[1]
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from a_share_agent.stock_policy import is_restricted_stock_code  # noqa: E402


TENCENT_INDEX_CODES = {
    "sh_index": "sh000001",
    "sz_index": "sz399001",
    "cyb_index": "sz399006",
}

EASTMONEY_CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
EASTMONEY_BOARD_FS = "m:90+t:2"
EASTMONEY_STOCK_FIELDS = "f12,f14,f62,f184,f3,f2"
EASTMONEY_BOARD_FIELDS = "f12,f14,f62,f184,f3,f2"
SINA_MONEYFLOW_URL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php"


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
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read().decode(encoding, errors="replace")
    except Exception as first_exc:
        try:
            completed = subprocess.run(
                [
                    "curl",
                    "-fsSL",
                    "--compressed",
                    "--max-time",
                    str(timeout),
                    "-A",
                    "Mozilla/5.0 a-share-agent-cloud-p1/1.0",
                    "-e",
                    "https://data.eastmoney.com/",
                    url,
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            return completed.stdout.decode(encoding, errors="replace")
        except Exception:
            raise first_exc


def http_get_json(url, timeout=15):
    return json.loads(http_get(url, timeout=timeout))


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
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def source_trade_dates(rows):
    dates = set()
    for row in rows or []:
        value = str(row.get("trade_date") or row.get("time") or "").strip()
        digits = "".join(ch for ch in value if ch.isdigit())
        if len(digits) >= 8:
            dates.add(f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}")
    return sorted(dates)


def amount_yuan_to_billion(value):
    number = safe_float(value)
    if number is None:
        return None
    return round(number / 100000000, 3)


def eastmoney_clist(params, timeout=15):
    query = urlencode(params, safe=":,+")
    data = http_get_json(f"{EASTMONEY_CLIST_URL}?{query}", timeout=timeout)
    rows = ((data.get("data") or {}).get("diff") or [])
    if not isinstance(rows, list):
        rows = []
    return rows


def fetch_eastmoney_board_capital():
    base = {
        "fid": "f62",
        "pz": "80",
        "pn": "1",
        "np": "1",
        "fltt": "2",
        "invt": "2",
        "fs": EASTMONEY_BOARD_FS,
        "fields": EASTMONEY_BOARD_FIELDS,
    }
    rows = eastmoney_clist({**base, "po": "1"})
    if not rows:
        rows = eastmoney_clist({**base, "po": "0"})
    parsed = []
    for row in rows:
        amount = amount_yuan_to_billion(row.get("f62"))
        if amount is None:
            continue
        parsed.append({
            "target_name": row.get("f14") or "",
            "target_code": row.get("f12") or "",
            "target_type": "板块",
            "net_amount_billion": amount,
            "net_amount_yuan": safe_float(row.get("f62")),
            "main_net_pct": safe_float(row.get("f184")),
            "source": "东方财富板块资金流",
        })
    inflow = sorted([row for row in parsed if (row.get("net_amount_billion") or 0) > 0], key=lambda r: r["net_amount_billion"], reverse=True)[:3]
    outflow = sorted([row for row in parsed if (row.get("net_amount_billion") or 0) < 0], key=lambda r: r["net_amount_billion"])[:3]
    return inflow, outflow, {
        "total_board_count": len(parsed),
        "positive_count": sum(1 for row in parsed if (row.get("net_amount_billion") or 0) > 0),
        "negative_count": sum(1 for row in parsed if (row.get("net_amount_billion") or 0) < 0),
        "zero_count": sum(1 for row in parsed if (row.get("net_amount_billion") or 0) == 0),
    }


def fetch_eastmoney_board_related_stocks(board_code, direction):
    if not board_code:
        return []
    rows = eastmoney_clist({
        "fid": "f62",
        "po": "1" if direction == "流入" else "0",
        "pz": "30",
        "pn": "1",
        "np": "1",
        "fltt": "2",
        "invt": "2",
        "fs": f"b:{board_code}",
        "fields": EASTMONEY_STOCK_FIELDS,
    })
    parsed = []
    for row in rows:
        name = row.get("f14") or ""
        code = row.get("f12") or ""
        if not name or not code or is_restricted_stock_code(code):
            continue
        net_amount_billion = amount_yuan_to_billion(row.get("f62"))
        parsed.append({
            "name": name,
            "code": code,
            "net_amount_billion": net_amount_billion,
            "main_net_pct": safe_float(row.get("f184")),
            "change_pct": safe_float(row.get("f3")),
            "latest_price": safe_float(row.get("f2")),
            "rank_basis": "板块成分股主力净额排序",
            "source": "东方财富板块成分股资金流",
        })
    return parsed[:5]


def fetch_sina_json(service, params):
    query = urlencode(params, safe=":,/")
    url = f"{SINA_MONEYFLOW_URL}/{service}?{query}"
    text = http_get(url, encoding="utf-8")
    return json.loads(text)


def fetch_sina_board_capital():
    rows = fetch_sina_json("MoneyFlow.ssl_bkzj_bk", {"fenlei": 0, "num": 80})
    if not isinstance(rows, list):
        rows = []
    parsed = []
    for row in rows:
        amount = amount_yuan_to_billion(row.get("netamount"))
        if amount is None:
            continue
        parsed.append({
            "target_name": row.get("name") or "",
            "target_code": f"{row.get('cate_type')}/{row.get('category')}",
            "target_type": "板块",
            "net_amount_billion": amount,
            "net_amount_yuan": safe_float(row.get("netamount")),
            "main_net_pct": safe_float(row.get("ratioamount")),
            "source": "新浪资金流向",
            "leading_stock_name": row.get("ts_name") or "",
            "leading_stock_code": row.get("ts_symbol") or "",
        })
    inflow = sorted([row for row in parsed if (row.get("net_amount_billion") or 0) > 0], key=lambda r: r["net_amount_billion"], reverse=True)[:3]
    outflow = sorted([row for row in parsed if (row.get("net_amount_billion") or 0) < 0], key=lambda r: r["net_amount_billion"])[:3]
    return inflow, outflow, {
        "total_board_count": len(parsed),
        "positive_count": sum(1 for row in parsed if (row.get("net_amount_billion") or 0) > 0),
        "negative_count": sum(1 for row in parsed if (row.get("net_amount_billion") or 0) < 0),
        "zero_count": sum(1 for row in parsed if (row.get("net_amount_billion") or 0) == 0),
    }


def fetch_sina_board_related_stocks(board_row, direction):
    bankuai = board_row.get("target_code") or ""
    if not bankuai:
        return []
    rows = fetch_sina_json(
        "MoneyFlow.ssl_bkzj_ssggzj",
        {
            "bankuai": bankuai,
            "num": 30,
            "sort": "r0_net",
            "asc": 0 if direction == "流入" else 1,
        },
    )
    if not isinstance(rows, list):
        rows = []
    parsed = []
    for row in rows:
        symbol = row.get("symbol") or ""
        code = symbol[-6:] if len(symbol) >= 6 else symbol
        name = row.get("name") or ""
        if not name or not code or is_restricted_stock_code(code):
            continue
        parsed.append({
            "name": name,
            "code": code,
            "symbol": symbol,
            "net_amount_billion": amount_yuan_to_billion(row.get("netamount")),
            "main_net_amount_billion": amount_yuan_to_billion(row.get("r0_net")),
            "main_net_pct": safe_float(row.get("r0_ratio")),
            "change_pct": round((safe_float(row.get("changeratio")) or 0) * 100, 2),
            "latest_price": safe_float(row.get("trade")),
            "rank_basis": "板块内主力净额排序",
            "source": "新浪资金流向",
        })
    return parsed[:5]


def build_capital_flow_payload(trade_date=None, quote_date_verified=True):
    def empty_payload(error, source_name="东方财富板块资金流"):
        return {
            "inflow_top": [],
            "outflow_top": [],
            "notes": f"云端P1资金流获取失败：{error}",
            "related_stock_requirement": {"min": 3, "max": 5, "status": "资金流获取失败"},
            "sources": [{
                "name": source_name,
                "type": "capital_flow",
                "url": EASTMONEY_CLIST_URL if "东方财富" in source_name else SINA_MONEYFLOW_URL,
                "ok": False,
                "error": str(error),
                "trade_date": trade_date or "",
                "date_verified": False,
            }],
        }

    if not quote_date_verified:
        return empty_payload(
            f"无法确认资金所属交易日：请求{trade_date or '未知'}，当日行情日期未通过校验",
            "多源资金流",
        )

    def build_from_rows(inflow, outflow, coverage, source_name, related_fetcher):
        sources = [{
            "name": source_name,
            "type": "capital_flow",
            "url": EASTMONEY_CLIST_URL if "东方财富" in source_name else SINA_MONEYFLOW_URL,
            "ok": bool(inflow or outflow),
            "error": "" if (inflow or outflow) else "未解析到板块资金流",
            "trade_date": trade_date or "",
            "date_verified": True,
            "observation_type": "current_snapshot",
        }]

        def enrich(rows, direction):
            enriched = []
            for idx, row in enumerate(rows[:3], 1):
                related_error = ""
                try:
                    related = related_fetcher(row, direction)
                except Exception as exc:
                    related = []
                    related_error = str(exc)
                status = "满足" if 3 <= len(related) <= 5 else "不足"
                enriched.append({
                    **row,
                    "flow_type": direction,
                    "rank_no": idx,
                    "related_stocks": related[:5],
                    "related_stock_count": len(related[:5]),
                    "related_stock_requirement_status": status,
                    "related_stock_notes": "" if status == "满足" else (related_error or "关联标的不足3只"),
                })
            return enriched

        inflow_top = enrich(inflow, "流入")
        outflow_top = enrich(outflow, "流出")
        all_rows = inflow_top + outflow_top
        ok_rows = [row for row in all_rows if 3 <= len(row.get("related_stocks") or []) <= 5]
        negative_count = int((coverage or {}).get("negative_count") or 0)
        total_board_count = int((coverage or {}).get("total_board_count") or 0)
        outflow_market_complete = (
            total_board_count >= 20
            and negative_count < 3
            and len(outflow_top) == negative_count
        )
        direction_count_ok = len(inflow_top) >= 3 and (
            len(outflow_top) >= 3 or outflow_market_complete
        )
        requirement_status = "满足" if direction_count_ok and len(ok_rows) == len(all_rows) else "不足"
        if requirement_status == "满足" and outflow_market_complete:
            notes = (
                f"云端P1已通过{source_name}采集资金流入Top3；覆盖{total_board_count}个板块，"
                f"当日真实净流出方向仅{negative_count}个，均已采集且每方向关联3-5只标的。"
            )
        elif requirement_status == "满足":
            notes = f"云端P1已通过{source_name}采集资金流入/流出Top3及每方向3-5只关联标的。"
        else:
            notes = f"云端P1通过{source_name}采集资金流，但资金方向或关联标的不足，P2必须二次核验。"
        return {
            "inflow_top": inflow_top,
            "outflow_top": outflow_top,
            "notes": notes,
            "market_coverage": coverage or {},
            "outflow_status": (
                "真实净流出方向不足3个，已完整采集"
                if outflow_market_complete
                else ("真实净流出Top3" if len(outflow_top) >= 3 else "净流出数据不足")
            ),
            "related_stock_requirement": {
                "min": 3,
                "max": 5,
                "status": requirement_status,
                "checked_directions": len(all_rows),
                "passed_directions": len(ok_rows),
            },
            "sources": sources,
        }

    try:
        inflow, outflow, coverage = fetch_eastmoney_board_capital()
        payload = build_from_rows(inflow, outflow, coverage, "东方财富板块资金流", lambda row, direction: fetch_eastmoney_board_related_stocks(row.get("target_code"), direction))
        if payload.get("related_stock_requirement", {}).get("status") == "满足":
            return payload
    except Exception as exc:
        eastmoney_error = exc
    try:
        inflow, outflow, coverage = fetch_sina_board_capital()
        payload = build_from_rows(inflow, outflow, coverage, "新浪资金流向", fetch_sina_board_related_stocks)
        if "eastmoney_error" in locals():
            payload["sources"].insert(0, {
                "name": "东方财富板块资金流",
                "type": "capital_flow",
                "url": EASTMONEY_CLIST_URL,
                "ok": False,
                "error": str(eastmoney_error),
                "trade_date": trade_date or "",
                "date_verified": True,
                "observation_type": "current_snapshot",
            })
        return payload
    except Exception as exc:
        if "eastmoney_error" in locals():
            return empty_payload(f"东方财富失败：{eastmoney_error}；新浪失败：{exc}", "多源资金流")
        return empty_payload(exc, "新浪资金流向")


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
        observed_dates = source_trade_dates(result.get("rows") or [])
        if result.get("ok") and observed_dates and trade_date not in observed_dates:
            result["ok"] = False
            result["error"] = (
                f"行情交易日不匹配: 请求{trade_date}，来源{','.join(observed_dates)}"
            )
            result["data"] = {}
        sources.append({
            "name": result["name"],
            "type": "quote_index",
            "url": result.get("url", ""),
            "ok": result["ok"],
            "error": result.get("error", ""),
            "trade_date": trade_date,
            "observed_trade_dates": observed_dates,
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

    quote_date_verified = any(
        source.get("ok")
        and source.get("type") == "quote_index"
        and trade_date in (source.get("observed_trade_dates") or [])
        for source in sources
    )
    capital_flow = build_capital_flow_payload(
        trade_date=trade_date,
        quote_date_verified=quote_date_verified,
    )
    sources.extend(capital_flow.pop("sources", []))

    confirmed_fields = sum(1 for value in market_index.values() if value is not None)
    ok_sources = sum(1 for source in sources if source.get("ok"))
    capital_requirement = capital_flow.get("related_stock_requirement") or {}
    capital_ok = capital_requirement.get("status") == "满足"
    confidence = 35 + ok_sources * 12 + min(confirmed_fields, 6) * 3 + (18 if capital_ok else 0)
    confidence = max(0, min(100, confidence))
    data_status = "云端自动采集-收盘行情与资金流" if ok_sources and capital_ok else ("云端自动采集-资金流待核验" if ok_sources else "云端自动采集失败-占位包")

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
        "capital_flow": capital_flow,
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
<p>URL 模板: <code>https://raw.githubusercontent.com/jackliu333777-hue/Repository-name-a-share-agent-data/main/{{trade_date}}/data.json</code></p>
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
