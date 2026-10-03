"""央企指标管理 的轻量本地调用入口。

用法::

    cat request.json | python3 -m app.api [--db metrics.db]

request.json 形如::

    {"actor": "hq", "action": "create_caliber",
     "payload": {"metric_code": "sei", "formula": "base * rate", "reason": "初版"},
     "request_id": "r-001"}

不传 --db 时数据仅保存在内存；传入文件路径后通过事件重放持久化，
进程重启后状态与请求幂等性均保留。
"""
import argparse
import json
import sys

from .contracts import Request
from .service import SoeMetricsService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="央企指标管理本地入口")
    parser.add_argument("--db", default=None, help="SQLite 数据库文件路径（缺省为纯内存）")
    args = parser.parse_args(argv)

    raw = sys.stdin.read().strip()
    if not raw:
        return 2
    try:
        item = json.loads(raw)
        request = Request(
            str(item.get("actor", "")),
            str(item.get("action", "")),
            dict(item.get("payload", {})),
            str(item.get("request_id", "")),
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        print(json.dumps({"accepted": False, "state": "rejected",
                          "message": "输入不是合法的请求 JSON", "data": {}}, ensure_ascii=False))
        return 1

    service = SoeMetricsService(args.db)
    try:
        result = service.handle(request)
    finally:
        service.close()
    print(json.dumps(result.to_dict(), ensure_ascii=False))
    return 0 if result.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
