"""央企指标管理 的轻量本地调用入口。

用法：
    echo '{"actor": "...", "action": "...", "payload": {...}, "request_id": "..."}' \
        | python3 -m app.api [数据库文件]

数据库文件默认取环境变量 SOE_METRICS_DB，缺省为 ./soe_metrics.db。
"""
import json
import os
import sys

from .contracts import Request
from .service import SoeMetricsService


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SOE_METRICS_DB", "soe_metrics.db")
    raw = sys.stdin.read().strip()
    if not raw:
        return 2
    item = json.loads(raw)
    request = Request(
        str(item.get("actor", "")),
        str(item.get("action", "")),
        dict(item.get("payload", {})),
        str(item.get("request_id", "")),
    )
    service = SoeMetricsService(db_path)
    try:
        result = service.handle(request)
    except (ValueError, TypeError) as exc:
        print(json.dumps({"accepted": False, "state": "rejected", "message": str(exc), "data": {}},
                         ensure_ascii=False))
        return 2
    print(json.dumps(result.to_dict(), ensure_ascii=False))
    return 0 if result.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
