#!/bin/bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

OUT="${TMP_DIR}/public"

PYTHONPYCACHEPREFIX=/tmp/pycache-agent python3 -m py_compile \
  "${ROOT_DIR}/tools/bin/collect_cloud_market_data.py"

python3 - "${ROOT_DIR}" "$OUT" <<'PY' >/tmp/a-share-cloud-p1-collector.out
import importlib.util
import json
import sys
from pathlib import Path

root, out = Path(sys.argv[1]), Path(sys.argv[2])
path = root / "tools/bin/collect_cloud_market_data.py"
spec = importlib.util.spec_from_file_location("collector", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def quote(name):
    return {
        "ok": True,
        "name": name,
        "url": "https://example.invalid/quotes",
        "data": {"sh_index": 3000.0, "sz_index": 10000.0, "cyb_index": 2000.0},
        "rows": [{"trade_date": "2026-06-25"}],
        "error": "",
    }

def capital(trade_date=None, quote_date_verified=True):
    assert trade_date == "2026-06-25"
    assert quote_date_verified is True
    def rows(kind):
        return [
            {
                "flow_type": kind,
                "target_name": f"{kind}方向{i}",
                "related_stocks": [
                    {"name": f"样本{i}{j}", "code": f"600{i}{j:02d}"}
                    for j in range(3)
                ],
            }
            for i in range(3)
        ]
    return {
        "inflow_top": rows("流入"),
        "outflow_top": rows("流出"),
        "market_coverage": {"total_board_count": 80, "positive_count": 40, "negative_count": 40, "zero_count": 0},
        "related_stock_requirement": {"min": 3, "max": 5, "status": "满足", "checked_directions": 6, "passed_directions": 6},
        "sources": [{
            "name": "测试资金源",
            "type": "capital_flow",
            "url": "https://example.invalid/capital",
            "ok": True,
            "error": "",
            "trade_date": trade_date,
            "date_verified": True,
        }],
    }

module.fetch_tencent_indexes = lambda: quote("测试指数主源")
module.fetch_sina_indexes = lambda: quote("测试指数备用")
module.build_capital_flow_payload = capital
payload = module.build_payload("2026-06-25")
data_path, manifest_path, manifest = module.write_payload(out, payload)
module.update_static_index(out, payload)
print(json.dumps({"ok": True, "data_path": str(data_path), "manifest_path": str(manifest_path), "manifest": manifest}, ensure_ascii=False))

module.fetch_tencent_indexes = lambda: {**quote("错日源"), "rows": [{"trade_date": "2026-06-26"}]}
module.fetch_sina_indexes = lambda: {**quote("错日备用"), "rows": [{"time": "20260626153000"}]}
capital_calls = []
module.build_capital_flow_payload = lambda trade_date=None, quote_date_verified=True: (
    capital_calls.append((trade_date, quote_date_verified))
    or {
        "inflow_top": [],
        "outflow_top": [],
        "notes": "错日保护",
        "related_stock_requirement": {"min": 3, "max": 5, "status": "资金流获取失败"},
        "sources": [{"name": "测试资金源", "type": "capital_flow", "url": "", "ok": False, "error": "错日保护"}],
    }
)
mismatch = module.build_payload("2026-06-25")
assert mismatch["market_index"]["sh_index"] is None
quote_sources = [source for source in mismatch["source_manifest"]["sources"] if source["type"] == "quote_index"]
assert quote_sources and all(not source["ok"] for source in quote_sources)
assert all("交易日不匹配" in source["error"] for source in quote_sources)
assert capital_calls == [("2026-06-25", False)]
assert mismatch["capital_flow"]["inflow_top"] == []
assert mismatch["capital_flow"]["outflow_top"] == []
PY

grep -q '"ok": true' /tmp/a-share-cloud-p1-collector.out
test -f "${OUT}/2026-06-25/data.json"
test -f "${OUT}/2026-06-25/manifest.json"
test -f "${OUT}/latest.json"
test -f "${OUT}/index.html"

python3 - "${ROOT_DIR}" "$OUT" <<'PY'
import importlib.util
import json
import sys
from pathlib import Path

root, out = Path(sys.argv[1]), Path(sys.argv[2])
payload = json.loads((out / "2026-06-25" / "data.json").read_text(encoding="utf-8"))
required = {
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
missing = required - set(payload)
assert not missing, missing
assert payload["trade_date"] == "2026-06-25"
assert isinstance(payload["source_manifest"]["sources"], list)
assert 0 <= payload["confidence_score"] <= 100
assert "related_stock_requirement" in payload["capital_flow"]
for key in ("inflow_top", "outflow_top"):
    assert isinstance(payload["capital_flow"][key], list)
    for row in payload["capital_flow"][key]:
        assert "related_stocks" in row
        assert len(row["related_stocks"]) <= 5
assert json.loads((out / "latest.json").read_text(encoding="utf-8"))["trade_date"] == "2026-06-25"

checker_path = root / "tools/bin/check_cloud_market_data.py"
spec = importlib.util.spec_from_file_location("checker", checker_path)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)
related = [{"name": f"标的{i}", "code": f"60000{i}"} for i in range(3)]
limited = {
    "capital_flow": {
        "inflow_top": [
            {"target_name": f"流入{i}", "related_stocks": related}
            for i in range(3)
        ],
        "outflow_top": [{"target_name": "唯一真实流出", "related_stocks": related}],
        "market_coverage": {
            "total_board_count": 80,
            "positive_count": 79,
            "negative_count": 1,
            "zero_count": 0,
        },
    }
}
quality = checker.capital_related_quality(limited)
assert quality["ok"], quality
assert quality["outflow_market_complete"] is True
limited["capital_flow"]["market_coverage"] = {}
assert checker.capital_related_quality(limited)["ok"] is False
PY

python3 "${ROOT_DIR}/tools/bin/check_cloud_market_data.py" \
  --db "${TMP_DIR}/missing.db" \
  --cloud-root "$OUT" \
  --date 2026-06-25 \
  --fail-on-missing \
  --require-capital-related >/tmp/a-share-cloud-p1-check.out

grep -q '"status": "ready"' /tmp/a-share-cloud-p1-check.out

echo "cloud P1 collector tests passed"
