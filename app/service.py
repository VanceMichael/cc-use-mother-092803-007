"""央企指标管理：口径版本、周期数据、快照锁定与重算的领域服务。

业务规则：
- 集团按组织层级维护指标、公式与口径版本，同一指标同一时刻只有一个启用口径；
- 下属单位按启用口径提交周期数据，系统按口径定义校验字段并计算单位值；
- 复核人员锁定快照后，任何角色（含集团管理员）都不能覆盖该范围的数据，
  如需调整只能启用新口径并发起重算；
- 重算保留原快照与原口径数据，逐单位记录影响与变更原因；
- 所有写操作按 request_id 幂等，周期数据按自然键去重，重复导入不会产生第二份；
- 汇总查询同时返回汇总值、参与计算的单位、口径版本与重算影响。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from .contracts import Request, Result, validate_request
from .formula import (
    FormulaError,
    coerce_values,
    evaluate,
    validate_formula,
    validate_inputs_spec,
)
from .store import init_schema

ROLE_GROUP_ADMIN = "group_admin"      # 集团管理员：维护组织、授权、指标与口径，发起重算
ROLE_ADMIN = "admin"                  # 普通管理员：在管辖范围内填报，不能管理口径、不能锁定
ROLE_REVIEWER = "reviewer"            # 复核人员：锁定快照
ROLE_REPORTER = "unit_reporter"       # 单位填报员：仅为本单位填报
ROLES = (ROLE_GROUP_ADMIN, ROLE_ADMIN, ROLE_REVIEWER, ROLE_REPORTER)

PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
MAX_IMPORT_ITEMS = 1000


class DomainError(Exception):
    """业务规则拒绝；code 供调用方区分拒绝类型。"""

    def __init__(self, message: str, code: str = "VALIDATION") -> None:
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


class SoeMetricsService:
    """指标管理服务入口；数据保存在调用方提供的 SQLite 文件中。"""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        init_schema(self._conn)
        self._lock = RLock()
        self._actions = {
            "system.bootstrap": self._bootstrap,
            "org.register": self._org_register,
            "actor.grant": self._actor_grant,
            "metric.define": self._metric_define,
            "caliber.create": self._caliber_create,
            "caliber.activate": self._caliber_activate,
            "data.submit": self._data_submit,
            "data.import": self._data_import,
            "snapshot.lock": self._snapshot_lock,
            "recalc.start": self._recalc_start,
            "query.aggregate": self._query_aggregate,
        }

    # ---------------------------------------------------------------- 入口

    def handle(self, request: Request) -> Result:
        validate_request(request)
        with self._lock:
            replay = self._replay(request)
            if replay is not None:
                return replay
            handler = self._actions.get(request.action)
            if handler is None:
                result = Result(False, "rejected", f"未知动作: {request.action}",
                                {"reason_code": "UNKNOWN_ACTION"})
            else:
                try:
                    with self._conn:  # 业务写入与幂等记录同事务提交，失败整体回滚
                        result = handler(request)
                        self._remember(request, result)
                except DomainError as exc:
                    result = Result(False, "rejected", str(exc), {"reason_code": exc.code})
                if not result.accepted:
                    with self._conn:
                        self._remember(request, result)
            return result

    def _replay(self, request: Request) -> Result | None:
        row = self._one(
            "SELECT actor, action, payload_json, result_json FROM requests WHERE request_id = ?",
            (request.request_id,))
        if row is None:
            return None
        if (row["actor"], row["action"], row["payload_json"]) != (
                request.actor, request.action, self._fingerprint(request.payload)):
            return Result(False, "rejected", "幂等键已被不同请求占用",
                          {"reason_code": "IDEMPOTENCY_CONFLICT"})
        return Result.from_dict(json.loads(row["result_json"]))

    def _remember(self, request: Request, result: Result) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO requests(request_id, actor, action, payload_json, result_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (request.request_id, request.actor, request.action,
             self._fingerprint(request.payload),
             json.dumps(result.to_dict(), ensure_ascii=False), _now()))

    @staticmethod
    def _fingerprint(payload: dict[str, Any]) -> str:
        return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)

    # ---------------------------------------------------------------- 通用

    def _one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, args).fetchone()

    def _all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, args).fetchall()

    def _require_actor(self, actor_id: str) -> sqlite3.Row:
        row = self._one("SELECT * FROM actors WHERE actor_id = ?", (actor_id,))
        if row is None:
            raise DomainError(f"未注册的操作人: {actor_id}", "FORBIDDEN")
        return row

    @staticmethod
    def _require_role(actor: sqlite3.Row, *roles: str) -> None:
        if actor["role"] not in roles:
            raise DomainError(f"权限不足：角色 {actor['role']} 不能执行该操作", "FORBIDDEN")

    def _require_org(self, org_id: str) -> sqlite3.Row:
        row = self._one("SELECT * FROM orgs WHERE org_id = ?", (org_id,))
        if row is None:
            raise DomainError(f"组织不存在: {org_id}", "NOT_FOUND")
        return row

    def _require_metric(self, metric_code: str) -> sqlite3.Row:
        row = self._one("SELECT * FROM metrics WHERE metric_code = ?", (metric_code,))
        if row is None:
            raise DomainError(f"指标不存在: {metric_code}", "NOT_FOUND")
        return row

    def _require_period(self, period: str) -> None:
        if not PERIOD_RE.match(period):
            raise DomainError(f"周期格式应为 YYYY-MM: {period or '(空)'}")

    def _subtree(self, org_id: str) -> list[str]:
        rows = self._all(
            "WITH RECURSIVE sub(org_id) AS ("
            "  SELECT org_id FROM orgs WHERE org_id = ?"
            "  UNION ALL"
            "  SELECT o.org_id FROM orgs o JOIN sub s ON o.parent_id = s.org_id"
            ") SELECT org_id FROM sub", (org_id,))
        return [row["org_id"] for row in rows]

    def _subtree_at_level(self, org_id: str, level: int) -> list[str]:
        rows = self._all(
            "WITH RECURSIVE sub(org_id) AS ("
            "  SELECT org_id FROM orgs WHERE org_id = ?"
            "  UNION ALL"
            "  SELECT o.org_id FROM orgs o JOIN sub s ON o.parent_id = s.org_id"
            ") SELECT o.org_id FROM orgs o JOIN sub s ON o.org_id = s.org_id"
            " WHERE o.level = ? AND o.active = 1 ORDER BY o.org_id",
            (org_id, level))
        return [row["org_id"] for row in rows]

    def _active_caliber(self, metric_code: str) -> sqlite3.Row | None:
        return self._one(
            "SELECT * FROM calibers WHERE metric_code = ? AND status = 'active'"
            " ORDER BY version DESC LIMIT 1", (metric_code,))

    def _assert_submit_scope(self, actor: sqlite3.Row, unit_id: str) -> None:
        role = actor["role"]
        if role == ROLE_GROUP_ADMIN:
            return
        if role == ROLE_REPORTER:
            if actor["org_id"] != unit_id:
                raise DomainError("权限不足：填报员只能为本单位提交数据", "FORBIDDEN")
            return
        if role == ROLE_ADMIN:
            if unit_id not in self._subtree(actor["org_id"]):
                raise DomainError("权限不足：超出所辖组织范围", "FORBIDDEN")
            return
        raise DomainError("权限不足：该角色不能提交数据", "FORBIDDEN")

    def _assert_not_locked(self, metric_code: str, period: str, unit_id: str, caliber_id: str) -> None:
        rows = self._all(
            "SELECT org_id FROM snapshots WHERE metric_code = ? AND period = ? AND caliber_id = ?",
            (metric_code, period, caliber_id))
        for row in rows:
            if unit_id in self._subtree(row["org_id"]):
                raise DomainError(
                    "快照已锁定，禁止覆盖；如需调整请启用新口径并发起重算", "LOCKED")

    # ---------------------------------------------------------------- 引导与主数据

    def _bootstrap(self, request: Request) -> Result:
        if self._one("SELECT 1 AS x FROM orgs LIMIT 1") or self._one("SELECT 1 AS x FROM actors LIMIT 1"):
            raise DomainError("系统已初始化，禁止重复引导", "CONFLICT")
        payload = request.payload
        root_id = _text(payload.get("root_org_id"))
        root_name = _text(payload.get("root_name"))
        admin_actor = _text(payload.get("admin_actor"))
        if not root_id or not root_name or not admin_actor:
            raise DomainError("引导参数不完整：需要 root_org_id、root_name、admin_actor")
        self._conn.execute(
            "INSERT INTO orgs(org_id, name, parent_id, level, active) VALUES (?, ?, NULL, 0, 1)",
            (root_id, root_name))
        self._conn.execute(
            "INSERT INTO actors(actor_id, role, org_id) VALUES (?, ?, ?)",
            (admin_actor, ROLE_GROUP_ADMIN, root_id))
        return Result(True, "bootstrapped", "系统初始化完成",
                      {"root_org_id": root_id, "admin_actor": admin_actor})

    def _org_register(self, request: Request) -> Result:
        actor = self._require_actor(request.actor)
        self._require_role(actor, ROLE_GROUP_ADMIN)
        payload = request.payload
        org_id = _text(payload.get("org_id"))
        name = _text(payload.get("name"))
        parent_id = _text(payload.get("parent_id")) or None
        if not org_id or not name:
            raise DomainError("组织编号与名称不能为空")
        existing = self._one("SELECT * FROM orgs WHERE org_id = ?", (org_id,))
        if existing is not None:
            if existing["name"] == name and existing["parent_id"] == parent_id:
                return Result(True, "exists", "组织已存在",
                              {"org_id": org_id, "level": existing["level"]})
            raise DomainError("组织已存在且属性不一致", "CONFLICT")
        if parent_id is None:
            raise DomainError("必须指定上级组织（根组织由系统引导创建）")
        parent = self._one("SELECT * FROM orgs WHERE org_id = ?", (parent_id,))
        if parent is None:
            raise DomainError(f"上级组织不存在: {parent_id}", "NOT_FOUND")
        level = parent["level"] + 1
        self._conn.execute(
            "INSERT INTO orgs(org_id, name, parent_id, level, active) VALUES (?, ?, ?, ?, 1)",
            (org_id, name, parent_id, level))
        return Result(True, "registered", "组织已登记",
                      {"org_id": org_id, "parent_id": parent_id, "level": level})

    def _actor_grant(self, request: Request) -> Result:
        actor = self._require_actor(request.actor)
        self._require_role(actor, ROLE_GROUP_ADMIN)
        payload = request.payload
        target = _text(payload.get("actor_id"))
        role = _text(payload.get("role"))
        org_id = _text(payload.get("org_id"))
        if not target:
            raise DomainError("被授权人不能为空")
        if role not in ROLES:
            raise DomainError(f"未知角色: {role}")
        self._require_org(org_id)
        existing = self._one("SELECT * FROM actors WHERE actor_id = ?", (target,))
        if existing is not None:
            if existing["role"] == role and existing["org_id"] == org_id:
                return Result(True, "granted", "授权已存在",
                              {"actor_id": target, "role": role, "org_id": org_id})
            self._conn.execute(
                "UPDATE actors SET role = ?, org_id = ? WHERE actor_id = ?",
                (role, org_id, target))
            return Result(True, "updated", "授权已更新",
                          {"actor_id": target, "role": role, "org_id": org_id})
        self._conn.execute(
            "INSERT INTO actors(actor_id, role, org_id) VALUES (?, ?, ?)",
            (target, role, org_id))
        return Result(True, "granted", "授权完成",
                      {"actor_id": target, "role": role, "org_id": org_id})

    def _metric_define(self, request: Request) -> Result:
        actor = self._require_actor(request.actor)
        self._require_role(actor, ROLE_GROUP_ADMIN)
        payload = request.payload
        metric_code = _text(payload.get("metric_code"))
        name = _text(payload.get("name"))
        unit = _text(payload.get("unit"))
        aggregation = _text(payload.get("aggregation")) or "SUM"
        if not metric_code or not name:
            raise DomainError("指标编码与名称不能为空")
        if aggregation not in ("SUM", "AVG"):
            raise DomainError(f"不支持的汇总方式: {aggregation}")
        existing = self._one("SELECT * FROM metrics WHERE metric_code = ?", (metric_code,))
        if existing is not None:
            if (existing["name"], existing["unit"], existing["aggregation"]) == (name, unit, aggregation):
                return Result(True, "exists", "指标已存在", {"metric_code": metric_code})
            raise DomainError("指标已存在且定义不一致", "CONFLICT")
        self._conn.execute(
            "INSERT INTO metrics(metric_code, name, unit, aggregation) VALUES (?, ?, ?, ?)",
            (metric_code, name, unit, aggregation))
        return Result(True, "defined", "指标已定义",
                      {"metric_code": metric_code, "aggregation": aggregation})

    # ---------------------------------------------------------------- 口径版本

    def _caliber_create(self, request: Request) -> Result:
        actor = self._require_actor(request.actor)
        self._require_role(actor, ROLE_GROUP_ADMIN)
        payload = request.payload
        metric_code = _text(payload.get("metric_code"))
        self._require_metric(metric_code)
        version = _positive_int(payload.get("version"))
        if version is None:
            raise DomainError("口径版本号必须是正整数")
        org_level = _positive_int(payload.get("org_level"))
        if org_level is None:
            raise DomainError("口径适用层级必须是正整数")
        formula = _text(payload.get("formula"))
        try:
            field_names = validate_inputs_spec(payload.get("inputs"))
            validate_formula(formula, field_names)
        except FormulaError as exc:
            raise DomainError(f"口径定义不合法：{exc}") from exc
        caliber_id = f"{metric_code}@v{version}"
        if self._one("SELECT 1 AS x FROM calibers WHERE caliber_id = ?", (caliber_id,)):
            raise DomainError(f"口径版本已存在: {caliber_id}", "CONFLICT")
        self._conn.execute(
            "INSERT INTO calibers(caliber_id, metric_code, version, formula, inputs_json,"
            " org_level, status, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?)",
            (caliber_id, metric_code, version, formula,
             json.dumps(payload["inputs"], ensure_ascii=False, sort_keys=True),
             org_level, request.actor, _now()))
        return Result(True, "draft", "口径版本已创建",
                      {"caliber_id": caliber_id, "version": version, "fields": field_names})

    def _caliber_activate(self, request: Request) -> Result:
        actor = self._require_actor(request.actor)
        self._require_role(actor, ROLE_GROUP_ADMIN)
        payload = request.payload
        metric_code = _text(payload.get("metric_code"))
        self._require_metric(metric_code)
        version = _positive_int(payload.get("version"))
        if version is None:
            raise DomainError("口径版本号必须是正整数")
        caliber = self._one(
            "SELECT * FROM calibers WHERE metric_code = ? AND version = ?",
            (metric_code, version))
        if caliber is None:
            raise DomainError(f"口径版本不存在: {metric_code}@v{version}", "NOT_FOUND")
        if caliber["status"] == "active":
            return Result(True, "active", "口径已处于启用状态",
                          {"caliber_id": caliber["caliber_id"], "version": version})
        previous = self._active_caliber(metric_code)
        self._conn.execute(
            "UPDATE calibers SET status = 'archived' WHERE metric_code = ? AND status = 'active'",
            (metric_code,))
        self._conn.execute(
            "UPDATE calibers SET status = 'active' WHERE caliber_id = ?",
            (caliber["caliber_id"],))
        return Result(True, "active", "口径已启用", {
            "caliber_id": caliber["caliber_id"],
            "version": version,
            "previous_active_version": previous["version"] if previous else None,
        })

    # ---------------------------------------------------------------- 周期数据

    def _data_submit(self, request: Request) -> Result:
        item = {
            "unit_id": request.payload.get("unit_id"),
            "metric_code": request.payload.get("metric_code"),
            "period": request.payload.get("period"),
            "values": request.payload.get("values"),
        }
        return self._import_items(request, [item], single=True)

    def _data_import(self, request: Request) -> Result:
        items = request.payload.get("items")
        if not isinstance(items, list) or not items:
            raise DomainError("导入内容不能为空")
        if len(items) > MAX_IMPORT_ITEMS:
            raise DomainError(f"单次导入不能超过 {MAX_IMPORT_ITEMS} 条")
        return self._import_items(request, items, single=False)

    def _import_items(self, request: Request, items: list[Any], single: bool) -> Result:
        actor = self._require_actor(request.actor)
        self._require_role(actor, ROLE_GROUP_ADMIN, ROLE_ADMIN, ROLE_REPORTER)
        prepared = []
        for index, raw in enumerate(items, start=1):
            try:
                prepared.append(self._prepare_item(actor, raw))
            except DomainError as exc:
                if single:
                    raise
                raise DomainError(f"第 {index} 条：{exc}", exc.code) from exc
        counts = {"created": 0, "updated": 0, "unchanged": 0}
        results = []
        for item in prepared:
            status = self._upsert_submission(item, request)
            counts[status] += 1
            results.append({
                "unit_id": item["unit_id"],
                "metric_code": item["metric_code"],
                "period": item["period"],
                "caliber_id": item["caliber"]["caliber_id"],
                "status": status,
                "computed_value": item["computed"],
            })
        state = "submitted" if single else "imported"
        message = "数据已受理" if single else (
            f"导入完成：新增 {counts['created']}，更新 {counts['updated']}，未变化 {counts['unchanged']}")
        return Result(True, state, message, {"items": results, **counts})

    def _prepare_item(self, actor: sqlite3.Row, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise DomainError("数据格式错误：必须是对象")
        unit_id = _text(raw.get("unit_id"))
        metric_code = _text(raw.get("metric_code"))
        period = _text(raw.get("period"))
        self._require_period(period)
        unit = self._require_org(unit_id)
        if not unit["active"]:
            raise DomainError(f"单位已停用: {unit_id}")
        self._require_metric(metric_code)
        caliber = self._active_caliber(metric_code)
        if caliber is None:
            raise DomainError(f"指标没有已启用的口径: {metric_code}", "CONFLICT")
        if unit["level"] != caliber["org_level"]:
            raise DomainError(
                f"单位层级 {unit['level']} 与口径适用层级 {caliber['org_level']} 不符")
        self._assert_submit_scope(actor, unit_id)
        self._assert_not_locked(metric_code, period, unit_id, caliber["caliber_id"])
        spec = json.loads(caliber["inputs_json"])
        try:
            values = coerce_values(spec, raw.get("values"))
            computed = evaluate(caliber["formula"], values)
        except FormulaError as exc:
            raise DomainError(f"数据校验失败：{exc}") from exc
        return {
            "unit_id": unit_id,
            "metric_code": metric_code,
            "period": period,
            "caliber": caliber,
            "values": values,
            "computed": computed,
        }

    def _upsert_submission(self, item: dict[str, Any], request: Request) -> str:
        caliber_id = item["caliber"]["caliber_id"]
        values_json = json.dumps(item["values"], ensure_ascii=False, sort_keys=True)
        key = (item["unit_id"], item["metric_code"], item["period"], caliber_id)
        existing = self._one(
            "SELECT values_json FROM submissions"
            " WHERE unit_id = ? AND metric_code = ? AND period = ? AND caliber_id = ?", key)
        if existing is None:
            self._conn.execute(
                "INSERT INTO submissions(unit_id, metric_code, period, caliber_id, values_json,"
                " computed_value, submitted_by, request_id, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*key, values_json, item["computed"], request.actor, request.request_id, _now()))
            return "created"
        if existing["values_json"] == values_json:
            return "unchanged"
        self._conn.execute(
            "UPDATE submissions SET values_json = ?, computed_value = ?, submitted_by = ?,"
            " request_id = ?, updated_at = ?"
            " WHERE unit_id = ? AND metric_code = ? AND period = ? AND caliber_id = ?",
            (values_json, item["computed"], request.actor, request.request_id, _now(), *key))
        return "updated"

    # ---------------------------------------------------------------- 快照锁定

    def _snapshot_lock(self, request: Request) -> Result:
        actor = self._require_actor(request.actor)
        self._require_role(actor, ROLE_REVIEWER)
        payload = request.payload
        metric_code = _text(payload.get("metric_code"))
        period = _text(payload.get("period"))
        org_id = _text(payload.get("org_id"))
        self._require_metric(metric_code)
        self._require_period(period)
        self._require_org(org_id)
        caliber = self._active_caliber(metric_code)
        if caliber is None:
            raise DomainError(f"指标没有已启用的口径，无法锁定: {metric_code}", "CONFLICT")
        if self._one(
                "SELECT 1 AS x FROM snapshots WHERE metric_code = ? AND period = ?"
                " AND org_id = ? AND caliber_id = ?",
                (metric_code, period, org_id, caliber["caliber_id"])):
            raise DomainError("该范围已锁定，请勿重复操作", "CONFLICT")
        units = self._subtree_at_level(org_id, caliber["org_level"])
        lines = self._submissions_in(metric_code, period, caliber["caliber_id"], units)
        if not lines:
            raise DomainError("该范围内没有可锁定的数据")
        snapshot_id = f"{metric_code}:{period}:{org_id}@v{caliber['version']}"
        self._conn.execute(
            "INSERT INTO snapshots(snapshot_id, metric_code, period, org_id, caliber_id,"
            " locked_by, locked_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (snapshot_id, metric_code, period, org_id, caliber["caliber_id"],
             request.actor, _now()))
        self._conn.executemany(
            "INSERT INTO snapshot_lines(snapshot_id, unit_id, computed_value) VALUES (?, ?, ?)",
            [(snapshot_id, row["unit_id"], row["computed_value"]) for row in lines])
        units_payload = [{"unit_id": row["unit_id"], "value": row["computed_value"]} for row in lines]
        return Result(True, "locked", "快照已锁定", {
            "snapshot_id": snapshot_id,
            "caliber_id": caliber["caliber_id"],
            "caliber_version": caliber["version"],
            "unit_count": len(lines),
            "units": units_payload,
            "total": sum(row["computed_value"] for row in lines),
        })

    def _submissions_in(
            self, metric_code: str, period: str, caliber_id: str, units: list[str]
    ) -> list[sqlite3.Row]:
        if not units:
            return []
        marks = ",".join("?" * len(units))
        return self._all(
            f"SELECT * FROM submissions WHERE metric_code = ? AND period = ? AND caliber_id = ?"
            f" AND unit_id IN ({marks}) ORDER BY unit_id",
            (metric_code, period, caliber_id, *units))

    # ---------------------------------------------------------------- 重算

    def _recalc_start(self, request: Request) -> Result:
        actor = self._require_actor(request.actor)
        self._require_role(actor, ROLE_GROUP_ADMIN)
        payload = request.payload
        metric_code = _text(payload.get("metric_code"))
        period = _text(payload.get("period"))
        reason = _text(payload.get("reason"))
        self._require_metric(metric_code)
        self._require_period(period)
        if not reason:
            raise DomainError("必须填写变更原因")
        to_caliber = self._resolve_to_caliber(metric_code, payload.get("to_version"))
        if self._one(
                "SELECT 1 AS x FROM recalcs WHERE metric_code = ? AND period = ? AND to_caliber_id = ?",
                (metric_code, period, to_caliber["caliber_id"])):
            raise DomainError("该口径已执行过重算，请勿重复发起", "DUPLICATE")
        if self._one(
                "SELECT 1 AS x FROM snapshots WHERE metric_code = ? AND period = ? AND caliber_id = ?",
                (metric_code, period, to_caliber["caliber_id"])):
            raise DomainError("目标口径下已存在锁定快照，禁止重算覆盖", "LOCKED")
        from_caliber = self._resolve_from_caliber(metric_code, period, to_caliber, payload.get("from_version"))
        sources = self._all(
            "SELECT * FROM submissions WHERE metric_code = ? AND period = ? AND caliber_id = ?"
            " ORDER BY unit_id", (metric_code, period, from_caliber["caliber_id"]))
        to_spec = json.loads(to_caliber["inputs_json"])
        impacts = []
        for source in sources:
            unit_id = source["unit_id"]
            old_value = source["computed_value"]
            existing = self._one(
                "SELECT computed_value FROM submissions WHERE unit_id = ? AND metric_code = ?"
                " AND period = ? AND caliber_id = ?",
                (unit_id, metric_code, period, to_caliber["caliber_id"]))
            if existing is not None:
                impacts.append((unit_id, old_value, existing["computed_value"], "kept_existing"))
                continue
            old_values = json.loads(source["values_json"])
            try:
                values = coerce_values(to_spec, old_values, strict=False)
                new_value = evaluate(to_caliber["formula"], values)
            except FormulaError:
                impacts.append((unit_id, old_value, None, "missing_input"))
                continue
            self._conn.execute(
                "INSERT INTO submissions(unit_id, metric_code, period, caliber_id, values_json,"
                " computed_value, submitted_by, request_id, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (unit_id, metric_code, period, to_caliber["caliber_id"],
                 json.dumps(values, ensure_ascii=False, sort_keys=True), new_value,
                 request.actor, request.request_id, _now()))
            impacts.append((unit_id, old_value, new_value, "recalculated"))
        recalc_id = f"{metric_code}:{period}:v{to_caliber['version']}"
        self._conn.execute(
            "INSERT INTO recalcs(recalc_id, metric_code, period, from_caliber_id, to_caliber_id,"
            " reason, initiated_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (recalc_id, metric_code, period, from_caliber["caliber_id"],
             to_caliber["caliber_id"], reason, request.actor, _now()))
        self._conn.executemany(
            "INSERT INTO recalc_impacts(recalc_id, unit_id, old_value, new_value, status)"
            " VALUES (?, ?, ?, ?, ?)",
            [(recalc_id, unit, old, new, status) for unit, old, new, status in impacts])
        return Result(True, "recalculated", "重算完成", {
            "recalc_id": recalc_id,
            "from_version": from_caliber["version"],
            "to_version": to_caliber["version"],
            "reason": reason,
            "affected_units": len(impacts),
            "impacts": [
                {"unit_id": unit, "old_value": old, "new_value": new, "status": status}
                for unit, old, new, status in impacts
            ],
        })

    def _resolve_to_caliber(self, metric_code: str, to_version: Any) -> sqlite3.Row:
        if to_version is None:
            caliber = self._active_caliber(metric_code)
            if caliber is None:
                raise DomainError(f"指标没有已启用的口径: {metric_code}", "CONFLICT")
            return caliber
        version = _positive_int(to_version)
        if version is None:
            raise DomainError("目标口径版本号必须是正整数")
        caliber = self._one(
            "SELECT * FROM calibers WHERE metric_code = ? AND version = ?", (metric_code, version))
        if caliber is None:
            raise DomainError(f"口径版本不存在: {metric_code}@v{version}", "NOT_FOUND")
        if caliber["status"] != "active":
            raise DomainError("目标口径未启用，请先启用再发起重算", "CONFLICT")
        return caliber

    def _resolve_from_caliber(
            self, metric_code: str, period: str, to_caliber: sqlite3.Row, from_version: Any
    ) -> sqlite3.Row:
        rows = self._all(
            "SELECT DISTINCT c.* FROM submissions s JOIN calibers c ON c.caliber_id = s.caliber_id"
            " WHERE s.metric_code = ? AND s.period = ? AND s.caliber_id != ?"
            " ORDER BY c.version", (metric_code, period, to_caliber["caliber_id"]))
        if from_version is not None:
            version = _positive_int(from_version)
            rows = [row for row in rows if row["version"] == version]
        if not rows:
            raise DomainError("没有可重算的数据")
        if len(rows) > 1:
            raise DomainError("存在多个来源口径，请指定 from_version")
        return rows[0]

    # ---------------------------------------------------------------- 汇总查询

    def _query_aggregate(self, request: Request) -> Result:
        self._require_actor(request.actor)  # 任何已注册角色均可查询
        payload = request.payload
        metric_code = _text(payload.get("metric_code"))
        period = _text(payload.get("period"))
        org_id = _text(payload.get("org_id"))
        metric = self._require_metric(metric_code)
        self._require_period(period)
        self._require_org(org_id)
        caliber = self._resolve_query_caliber(metric_code, payload)
        subtree = self._subtree(org_id)
        snapshot = self._one(
            "SELECT * FROM snapshots WHERE metric_code = ? AND period = ? AND org_id = ?"
            " AND caliber_id = ?", (metric_code, period, org_id, caliber["caliber_id"]))
        if snapshot is not None:
            source = "snapshot"
            rows = self._all(
                "SELECT unit_id, computed_value FROM snapshot_lines WHERE snapshot_id = ?"
                " ORDER BY unit_id", (snapshot["snapshot_id"],))
            lines = [(row["unit_id"], row["computed_value"]) for row in rows]
        else:
            source = "live"
            units = self._subtree_at_level(org_id, caliber["org_level"])
            lines = [(row["unit_id"], row["computed_value"])
                     for row in self._submissions_in(metric_code, period, caliber["caliber_id"], units)]
        value = self._aggregate(metric["aggregation"], [v for _, v in lines])
        recalcs = self._recalc_impacts_for(metric_code, period, set(subtree))
        data = {
            "metric_code": metric_code,
            "period": period,
            "org_id": org_id,
            "caliber_id": caliber["caliber_id"],
            "caliber_version": caliber["version"],
            "caliber_status": caliber["status"],
            "aggregation": metric["aggregation"],
            "unit": metric["unit"],
            "source": source,
            "value": value,
            "unit_count": len(lines),
            "units": [{"unit_id": unit, "value": val} for unit, val in lines],
            "recalcs": recalcs,
        }
        if snapshot is not None:
            data["snapshot_id"] = snapshot["snapshot_id"]
            data["locked_by"] = snapshot["locked_by"]
            data["locked_at"] = snapshot["locked_at"]
        message = "查询成功" if lines else "查询成功（暂无数据）"
        return Result(True, "ok", message, data)

    def _resolve_query_caliber(self, metric_code: str, payload: dict[str, Any]) -> sqlite3.Row:
        caliber_id = _text(payload.get("caliber_id"))
        if caliber_id:
            caliber = self._one(
                "SELECT * FROM calibers WHERE caliber_id = ? AND metric_code = ?",
                (caliber_id, metric_code))
            if caliber is None:
                raise DomainError(f"口径不存在: {caliber_id}", "NOT_FOUND")
            return caliber
        version = payload.get("caliber_version")
        if version is not None:
            if _positive_int(version) is None:
                raise DomainError("口径版本号必须是正整数")
            caliber = self._one(
                "SELECT * FROM calibers WHERE metric_code = ? AND version = ?",
                (metric_code, version))
            if caliber is None:
                raise DomainError(f"口径版本不存在: {metric_code}@v{version}", "NOT_FOUND")
            return caliber
        caliber = self._active_caliber(metric_code)
        if caliber is None:
            raise DomainError(f"指标没有已启用的口径: {metric_code}", "NOT_FOUND")
        return caliber

    @staticmethod
    def _aggregate(aggregation: str, values: list[float]) -> float | None:
        if not values:
            return None
        if aggregation == "AVG":
            return sum(values) / len(values)
        return sum(values)

    def _recalc_impacts_for(
            self, metric_code: str, period: str, subtree: set[str]
    ) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT r.*, cf.version AS from_version, ct.version AS to_version"
            " FROM recalcs r"
            " JOIN calibers cf ON cf.caliber_id = r.from_caliber_id"
            " JOIN calibers ct ON ct.caliber_id = r.to_caliber_id"
            " WHERE r.metric_code = ? AND r.period = ? ORDER BY r.created_at, r.recalc_id",
            (metric_code, period))
        recalcs = []
        for row in rows:
            impacts = self._all(
                "SELECT * FROM recalc_impacts WHERE recalc_id = ? ORDER BY unit_id",
                (row["recalc_id"],))
            recalcs.append({
                "recalc_id": row["recalc_id"],
                "from_version": row["from_version"],
                "to_version": row["to_version"],
                "reason": row["reason"],
                "initiated_by": row["initiated_by"],
                "created_at": row["created_at"],
                "impacts": [
                    {"unit_id": impact["unit_id"], "old_value": impact["old_value"],
                     "new_value": impact["new_value"], "status": impact["status"]}
                    for impact in impacts if impact["unit_id"] in subtree
                ],
            })
        return recalcs
