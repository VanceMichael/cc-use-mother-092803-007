"""指标管理服务：口径版本、可校验上报、锁定快照、重算留痕。

存储默认在内存；传入 SQLite 路径即可持久化（请求日志保证幂等去重在
进程重启后仍然有效）。所有写操作按 request_id 幂等。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Optional

from .contracts import Request, Result, validate_request
from .formula import FormulaError, compile_formula, evaluate, extract_references
from .model import (
    CaliberState,
    CaliberVersion,
    DomainError,
    Metric,
    Organization,
    PermissionError_,
    Recalculation,
    Role,
    Snapshot,
    Submission,
    SubmissionState,
    User,
)

# 汇总支持的算子
_AGGREGATORS = {"sum": sum, "max": max, "min": min}


class SoeMetricsService:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self._orgs: dict[str, Organization] = {}
        self._users: dict[str, User] = {}
        self._metrics: dict[str, Metric] = {}
        self._calibers: dict[tuple[str, int], CaliberVersion] = {}
        self._submissions: dict[tuple[str, str, str], Submission] = {}
        # 快照按版本链保存：原始锁定在前，重算追加在后，原快照永不被覆盖
        self._snapshots: dict[tuple[str, str, Optional[str]], list[Snapshot]] = {}
        self._recalcs: list[Recalculation] = []
        self._request_log: dict[str, Result] = {}
        self._import_refs: set[tuple[str, str, str, str]] = set()
        self._lock = threading.RLock()
        self._db_path = db_path
        self._db: Optional[sqlite3.Connection] = None
        if db_path:
            self._db = sqlite3.connect(db_path, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS request_log ("
                "request_id TEXT PRIMARY KEY, result TEXT NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS events ("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, "
                "actor TEXT NOT NULL, action TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            self._db.commit()
            for row in self._db.execute("SELECT request_id, result FROM request_log"):
                self._request_log[row[0]] = Result.from_dict(json.loads(row[1]))
            # 重放历史命令重建领域状态（状态全部由命令确定性派生）
            for row in self._db.execute(
                "SELECT actor, action, payload FROM events ORDER BY seq"
            ):
                replayed = Request(row[0], row[1], json.loads(row[2]), request_id="")
                self._dispatch(replayed)

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.commit()
                self._db.close()
                self._db = None

    # ------------------------------------------------------------------ 入口

    def handle(self, request: Request) -> Result:
        validate_request(request)
        with self._lock:
            previous = self._request_log.get(request.request_id)
            if previous is not None:
                return previous  # 重复请求（含重复导入）原样返回，绝不产生第二份数据
            try:
                result = self._dispatch(request)
            except PermissionError_ as exc:
                result = Result(False, "forbidden", str(exc))
            except (DomainError, FormulaError, ValueError, KeyError) as exc:
                result = Result(False, "rejected", str(exc))
            self._request_log[request.request_id] = result
            if self._db is not None:
                self._db.execute(
                    "INSERT OR REPLACE INTO request_log(request_id, result) VALUES (?, ?)",
                    (request.request_id, json.dumps(result.to_dict(), ensure_ascii=False)),
                )
                if result.accepted:
                    self._db.execute(
                        "INSERT OR IGNORE INTO events(request_id, actor, action, payload) "
                        "VALUES (?, ?, ?, ?)",
                        (request.request_id, request.actor, request.action,
                         json.dumps(request.payload, ensure_ascii=False)),
                    )
                self._db.commit()
            return result

    def _dispatch(self, request: Request) -> Result:
        p = request.payload
        handlers = {
            "add_org": self._add_org,
            "add_user": self._add_user,
            "define_metric": self._define_metric,
            "extend_metric_inputs": self._extend_metric_inputs,
            "create_caliber": self._create_caliber,
            "activate_caliber": self._activate_caliber,
            "submit_data": self._submit_data,
            "import_data": self._import_data,
            "lock_snapshot": self._lock_snapshot,
            "recalculate": self._recalculate,
            "query_summary": self._query_summary,
        }
        handler = handlers.get(request.action)
        if handler is None:
            return Result(False, "rejected", f"未知动作: {request.action}")
        return handler(request.actor, p)

    # ------------------------------------------------------------- 基础维护

    def register_org(self, org_id: str, name: str, parent_id: Optional[str] = None) -> None:
        if org_id in self._orgs:
            raise DomainError(f"组织已存在: {org_id}")
        if parent_id is not None and parent_id not in self._orgs:
            raise DomainError(f"上级组织不存在: {parent_id}")
        if parent_id == org_id:
            raise DomainError("组织不能以自身为上级")
        self._orgs[org_id] = Organization(org_id, name, parent_id)

    def register_user(self, user_id: str, role: str | Role, org_id: Optional[str] = None) -> None:
        role = Role(role)
        if role in (Role.UNIT_ADMIN,) and (not org_id or org_id not in self._orgs):
            raise DomainError("单位管理员必须归属一个已登记组织")
        if org_id is not None and org_id not in self._orgs:
            raise DomainError(f"组织不存在: {org_id}")
        self._users[user_id] = User(user_id, role, org_id)

    def define_metric(self, code: str, name: str, unit: str, inputs: list[str]) -> None:
        if code in self._metrics:
            raise DomainError(f"指标已存在: {code}")
        self._metrics[code] = Metric(code, name, unit, list(inputs))

    def _add_org(self, actor: str, p: dict[str, Any]) -> Result:
        self._require_role(actor, Role.HQ_ADMIN)
        org_id = _need_str(p, "org_id")
        name = _need_str(p, "name")
        parent_id = p.get("parent_id") or None
        self.register_org(org_id, name, parent_id)
        return Result(True, "org_added", "组织已登记", {"org_id": org_id, "parent_id": parent_id})

    def _add_user(self, actor: str, p: dict[str, Any]) -> Result:
        bootstrap = not self._users  # 系统中尚无用户：允许引导登记首位集团管理员
        if not bootstrap:
            self._require_role(actor, Role.HQ_ADMIN)
        user_id = _need_str(p, "user_id")
        role = _need_str(p, "role")
        if bootstrap and Role(role) != Role.HQ_ADMIN:
            raise DomainError("引导登记的首位用户必须是集团管理员")
        org_id = p.get("org_id") or None
        self.register_user(user_id, role, org_id)
        return Result(True, "user_added",
                      "首位集团管理员已引导登记" if bootstrap else "用户已登记",
                      {"user_id": user_id, "role": role, "org_id": org_id})

    def _define_metric(self, actor: str, p: dict[str, Any]) -> Result:
        self._require_role(actor, Role.HQ_ADMIN)
        code = _need_str(p, "metric_code")
        name = _need_str(p, "name")
        unit = _need_str(p, "unit")
        inputs = list(p.get("inputs") or [])
        if not all(isinstance(i, str) and i for i in inputs):
            raise DomainError("输入指标编码必须是非空字符串")
        self.define_metric(code, name, unit, inputs)
        return Result(True, "metric_defined", "指标已定义", {"metric_code": code, "inputs": inputs})

    # ------------------------------------------------------------- 口径版本

    def _extend_metric_inputs(self, actor: str, p: dict[str, Any]) -> Result:
        """新口径需要新增输入项时，由集团追加指标输入定义（只增不删，旧口径仍可计算）。"""
        self._require_role(actor, Role.HQ_ADMIN)
        metric_code = _need_str(p, "metric_code")
        metric = self._metrics.get(metric_code)
        if metric is None:
            raise DomainError(f"指标不存在: {metric_code}")
        new_inputs = list(_need(p, "new_inputs"))
        if not new_inputs or not all(isinstance(i, str) and i for i in new_inputs):
            raise DomainError("新增输入项必须是非空字符串列表")
        added = [i for i in new_inputs if i not in metric.inputs]
        metric.inputs.extend(added)
        return Result(True, "inputs_extended", "指标输入项已扩展",
                      {"metric_code": metric_code, "added": added, "inputs": list(metric.inputs)})

    def _create_caliber(self, actor: str, p: dict[str, Any]) -> Result:
        self._require_role(actor, Role.HQ_ADMIN)
        metric_code = _need_str(p, "metric_code")
        metric = self._metrics.get(metric_code)
        if metric is None:
            raise DomainError(f"指标不存在: {metric_code}")
        formula = _need_str(p, "formula")
        reason = str(p.get("reason", "")).strip()
        if not reason:
            raise DomainError("新建口径必须说明变更原因")
        compile_formula(formula, set(metric.inputs))  # 失败抛 FormulaError
        version = 1 + max(
            (cal.version for (code, _v), cal in self._calibers.items() if code == metric_code),
            default=0,
        )
        self._calibers[(metric_code, version)] = CaliberVersion(
            metric_code=metric_code, version=version, formula=formula,
            reason=reason, created_by=actor,
        )
        auto_activate = bool(p.get("activate", True))
        state = CaliberState.DRAFT.value
        if auto_activate:
            self._activate(metric_code, version, actor)
            state = CaliberState.ACTIVE.value
        return Result(True, "caliber_created", "口径版本已创建" + ("并启用" if auto_activate else ""),
                      {"metric_code": metric_code, "version": version, "state": state,
                       "references": extract_references(formula), "reason": reason})

    def _activate_caliber(self, actor: str, p: dict[str, Any]) -> Result:
        self._require_role(actor, Role.HQ_ADMIN)
        metric_code = _need_str(p, "metric_code")
        version = int(_need(p, "version"))
        self._activate(metric_code, version, actor)
        return Result(True, "caliber_active", "口径已启用",
                      {"metric_code": metric_code, "version": version})

    def _activate(self, metric_code: str, version: int, actor: str) -> None:
        target = self._calibers.get((metric_code, version))
        if target is None:
            raise DomainError(f"口径版本不存在: {metric_code} v{version}")
        if target.state == CaliberState.RETIRED:
            raise DomainError("已退役口径不能重新启用")
        for cal in self._calibers.values():
            if cal.metric_code == metric_code and cal.state == CaliberState.ACTIVE and cal.version != version:
                cal.state = CaliberState.RETIRED
                cal.superseded_by = version
        target.state = CaliberState.ACTIVE
        target.activated_by = actor

    def _active_caliber(self, metric_code: str) -> CaliberVersion:
        active = [c for c in self._calibers.values()
                  if c.metric_code == metric_code and c.state == CaliberState.ACTIVE]
        if not active:
            raise DomainError(f"指标尚无启用中的口径: {metric_code}")
        return active[0]

    # --------------------------------------------------------------- 上报

    def _submit_data(self, actor: str, p: dict[str, Any]) -> Result:
        return self._ingest(actor, p, import_ref="")

    def _import_data(self, actor: str, p: dict[str, Any]) -> Result:
        import_ref = _need_str(p, "import_ref")
        return self._ingest(actor, p, import_ref=import_ref)

    def _ingest(self, actor: str, p: dict[str, Any], import_ref: str) -> Result:
        user = self._require_role(actor, Role.UNIT_ADMIN)
        metric_code = _need_str(p, "metric_code")
        period = _need_str(p, "period")
        metric = self._metrics.get(metric_code)
        if metric is None:
            raise DomainError(f"指标不存在: {metric_code}")
        org_id = str(p.get("org_id") or user.org_id)
        # 权限越界：单位管理员只能写本单位；本单位及其下级之外一律拒绝
        if org_id != user.org_id and org_id not in self._descendants(user.org_id):
            raise PermissionError_(f"无权为归属范围外的单位上报: {org_id}")
        if org_id not in self._orgs:
            raise DomainError(f"组织不存在: {org_id}")

        key = (metric_code, period, org_id)
        existing = self._submissions.get(key)

        # 已成功落库的批次：重试（含内容被改动、或周期随后被锁定的重试）一律忽略，
        # 绝不产生第二份数据
        dedup_key: Optional[tuple[str, str, str, str]] = None
        if import_ref:
            dedup_key = (import_ref, metric_code, period, org_id)
            if dedup_key in self._import_refs:
                return Result(True, "duplicate_ignored", "重复导入已忽略，未产生新数据",
                              {"metric_code": metric_code, "period": period, "org_id": org_id,
                               "import_ref": import_ref,
                               "caliber_version": existing.caliber_version if existing else None})

        if existing is not None and existing.state == SubmissionState.LOCKED:
            raise PermissionError_("该周期数据已被复核快照锁定，普通管理员不得覆盖")

        inputs = dict(p.get("inputs") or {})
        self._validate_inputs(metric, inputs)
        caliber = self._active_caliber(metric_code)
        computed = evaluate(caliber.formula, inputs)
        stated = float(_need(p, "value"))
        if abs(computed - stated) > 1e-9:
            raise DomainError(
                f"上报值未通过口径校验: 上报 {stated}，按口径 v{caliber.version} 应为 {computed}"
            )

        # 首次校验通过后才登记导入批次（校验失败不占用 import_ref，允许修正后重导）
        if import_ref and dedup_key is not None:
            self._import_refs.add(dedup_key)

        if existing is not None:  # 未锁定前允许更正，仍只有一份数据
            existing.inputs = inputs
            existing.stated_value = stated
            existing.computed_value = computed
            existing.caliber_version = caliber.version
            existing.submitted_by = actor
            if import_ref:
                existing.import_ref = import_ref
            action_state = "resubmitted"
            message = "上报已更正（仍为同一份数据）"
        else:
            self._submissions[key] = Submission(
                metric_code=metric_code, period=period, org_id=org_id, inputs=inputs,
                stated_value=stated, computed_value=computed, caliber_version=caliber.version,
                submitted_by=actor, import_ref=import_ref,
            )
            action_state = "submitted"
            message = "周期数据已受理并通过口径校验"
        return Result(True, action_state, message,
                      {"metric_code": metric_code, "period": period, "org_id": org_id,
                       "value": computed, "caliber_version": caliber.version,
                       "import_ref": import_ref or None})

    def _validate_inputs(self, metric: Metric, inputs: dict[str, Any]) -> None:
        if set(inputs) != set(metric.inputs):
            missing = set(metric.inputs) - set(inputs)
            extra = set(inputs) - set(metric.inputs)
            raise DomainError(f"输入项与指标定义不符（缺: {sorted(missing)}，多: {sorted(extra)}）")
        for name, value in inputs.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise DomainError(f"输入项必须是数字: {name}")

    # ------------------------------------------------------------- 快照锁定

    def _lock_snapshot(self, actor: str, p: dict[str, Any]) -> Result:
        self._require_role(actor, Role.REVIEWER)
        metric_code = _need_str(p, "metric_code")
        period = _need_str(p, "period")
        caliber = self._active_caliber(metric_code)
        scope_org = p.get("scope_org")  # 锁定该组织及其下级；缺省为全集团
        if scope_org is not None and scope_org not in self._orgs:
            raise DomainError(f"组织不存在: {scope_org}")
        scope_key = scope_org or None

        chain = self._snapshots.get((metric_code, period, scope_key))
        if chain:
            raise DomainError("该范围周期快照已锁定（重算会生成新版本，不改变既有快照）")

        targets = {
            key: sub for key, sub in self._submissions.items()
            if key[0] == metric_code and key[1] == period
            and (scope_key is None or self._is_self_or_descendant(scope_key, key[2]))
        }
        if not targets:
            raise DomainError("范围内没有可锁定的周期上报数据")

        # 未锁定数据若仍按旧口径上报：按新口径自动重算（输入未冻结，无需保留旧值）；
        # 若新口径引用了单位没有提供的输入，则拒绝并要求该单位补报
        stale_units: list[str] = []
        for sub in targets.values():
            if sub.caliber_version == caliber.version:
                continue
            try:
                sub.computed_value = evaluate(caliber.formula, sub.inputs)
            except FormulaError:
                stale_units.append(f"{sub.org_id}@v{sub.caliber_version}")
                continue
            sub.caliber_version = caliber.version
        if stale_units:
            raise DomainError(
                f"新口径缺少以下单位的必要输入，需补报后再锁定: {sorted(stale_units)}"
            )

        org_ids = sorted(key[2] for key in targets)
        contributions = {oid: targets[(metric_code, period, oid)].computed_value for oid in org_ids}
        frozen_inputs = {oid: dict(targets[(metric_code, period, oid)].inputs) for oid in org_ids}
        aggregate = sum(contributions.values())
        snapshot = Snapshot(
            metric_code=metric_code, period=period, caliber_version=caliber.version,
            formula=caliber.formula, aggregate_value=aggregate, org_ids=org_ids,
            contributions=contributions, frozen_inputs=frozen_inputs, locked_by=actor,
            reason=str(p.get("reason", "")),
        )
        self._snapshots.setdefault((metric_code, period, scope_key), []).append(snapshot)
        for sub in targets.values():
            sub.state = SubmissionState.LOCKED
        return Result(True, "snapshot_locked", "周期快照已锁定，普通管理员不得覆盖",
                      _snapshot_data(snapshot, scope_key))

    # --------------------------------------------------------------- 重算

    def _recalculate(self, actor: str, p: dict[str, Any]) -> Result:
        self._require_role(actor, Role.REVIEWER)
        metric_code = _need_str(p, "metric_code")
        period = _need_str(p, "period")
        reason = _need_str(p, "reason")
        scope_org = p.get("scope_org") or None
        if scope_org is not None and scope_org not in self._orgs:
            raise DomainError(f"组织不存在: {scope_org}")

        chain = self._snapshots.get((metric_code, period, scope_org))
        if not chain:
            raise DomainError("未找到该范围的已锁定快照，无法发起重算")
        old_snapshot = chain[-1]
        new_caliber = self._active_caliber(metric_code)
        if new_caliber.version == old_snapshot.caliber_version:
            raise DomainError("当前启用口径与最新快照口径相同，无需重算")

        affected: list[str] = []
        excluded: list[str] = []
        new_contributions: dict[str, float] = {}
        for oid in old_snapshot.org_ids:
            frozen = old_snapshot.frozen_inputs.get(oid, {})
            try:
                new_value = evaluate(new_caliber.formula, frozen)
            except FormulaError:
                # 新口径需要该单位没有的输入：列为受影响单位，但暂不计入新值，需补充重报
                affected.append(oid)
                excluded.append(oid)
                continue
            new_contributions[oid] = new_value
            old_value = old_snapshot.contributions.get(oid)
            if old_value is None or abs(new_value - old_value) > 1e-9:
                affected.append(oid)
        affected.sort()
        excluded.sort()

        recalc_id = f"recalc-{metric_code}-{period}-{len(self._recalcs) + 1}"
        new_snapshot = Snapshot(
            metric_code=metric_code, period=period, caliber_version=new_caliber.version,
            formula=new_caliber.formula,
            aggregate_value=sum(new_contributions.values()),
            org_ids=sorted(new_contributions),
            contributions=new_contributions,
            frozen_inputs={oid: dict(old_snapshot.frozen_inputs.get(oid, {})) for oid in new_contributions},
            excluded_org_ids=excluded,
            locked_by=actor, reason=f"重算: {reason}", recalculation_id=recalc_id,
        )
        recalc = Recalculation(
            recalc_id=recalc_id, metric_code=metric_code, period=period,
            old_caliber_version=old_snapshot.caliber_version,
            new_caliber_version=new_caliber.version,
            old_value=old_snapshot.aggregate_value, new_value=new_snapshot.aggregate_value,
            affected_org_ids=affected, excluded_org_ids=excluded,
            reason=reason, requested_by=actor,
            old_snapshot_ref=f"{metric_code}|{period}|{scope_org or ''}#v{old_snapshot.caliber_version}",
        )
        self._recalcs.append(recalc)
        # 原快照保留在链上，新快照追加为最新版本，查询默认取最新
        chain.append(new_snapshot)
        return Result(True, "recalculated", "重算完成，原快照与受影响单位已保留",
                      {"recalc_id": recalc_id, "metric_code": metric_code, "period": period,
                       "old_caliber_version": recalc.old_caliber_version,
                       "new_caliber_version": recalc.new_caliber_version,
                       "old_value": recalc.old_value, "new_value": recalc.new_value,
                       "affected_org_ids": affected, "excluded_org_ids": excluded,
                       "reason": reason,
                       "old_snapshot_ref": recalc.old_snapshot_ref,
                       "old_snapshot": _snapshot_data(old_snapshot, scope_org),
                       "new_snapshot": _snapshot_data(new_snapshot, scope_org)})

    # --------------------------------------------------------------- 查询

    def _query_summary(self, actor: str, p: dict[str, Any]) -> Result:
        self._require_role(actor, Role.VIEWER, Role.UNIT_ADMIN, Role.HQ_ADMIN, Role.REVIEWER)
        metric_code = _need_str(p, "metric_code")
        period = _need_str(p, "period")
        scope_org = p.get("scope_org") or None
        agg_name = str(p.get("aggregate", "sum"))
        if agg_name not in _AGGREGATORS:
            raise DomainError(f"不支持的汇总方式: {agg_name}（可选 {sorted(_AGGREGATORS)}）")

        # 单位管理员只能看本单位及下级
        user = self._users[actor]
        if user.role == Role.UNIT_ADMIN:
            if scope_org is not None and not self._is_self_or_descendant(user.org_id, scope_org):
                raise PermissionError_("无权查询归属范围外的数据")
            effective_scope = scope_org or user.org_id
        else:
            effective_scope = scope_org
        if effective_scope is not None and effective_scope not in self._orgs:
            raise DomainError(f"组织不存在: {effective_scope}")

        locked_key = (metric_code, period, effective_scope)
        chain = self._snapshots.get(locked_key)
        if chain is None and effective_scope is not None:
            # 范围快照缺失时回退到全集团快照链再裁剪
            chain = self._snapshots.get((metric_code, period, None))
        # 取链上最近一份在本范围内有参与单位的快照（最新一份可能因待补报而为空）
        snapshot = None
        if chain:
            for candidate in reversed(chain):
                in_scope = [oid for oid in candidate.org_ids
                            if effective_scope is None or self._is_self_or_descendant(effective_scope, oid)]
                if in_scope:
                    snapshot = candidate
                    break
            if snapshot is None:
                snapshot = chain[0]  # 链上都为空时仍返回首份（其贡献裁剪后为空，交由下方报错）

        contributions: dict[str, float]
        caliber_version: Optional[int]
        formula: Optional[str]
        if snapshot is not None:
            in_scope = [oid for oid in snapshot.org_ids
                        if effective_scope is None or self._is_self_or_descendant(effective_scope, oid)]
            contributions = {oid: snapshot.contributions[oid] for oid in in_scope}
            caliber_version = snapshot.caliber_version
            formula = snapshot.formula
            source = "locked_snapshot"
        else:
            subs = [s for key, s in self._submissions.items()
                    if key[0] == metric_code and key[1] == period
                    and (effective_scope is None or self._is_self_or_descendant(effective_scope, key[2]))]
            contributions = {s.org_id: s.computed_value for s in sorted(subs, key=lambda s: s.org_id)}
            versions = {s.caliber_version for s in subs}
            caliber_version = next(iter(versions)) if len(versions) == 1 else None
            formula = None
            source = "live_submissions"
        if not contributions:
            raise DomainError("该范围周期内没有数据")

        values = list(contributions.values())
        aggregate = _AGGREGATORS[agg_name](values)
        recalcs = [r for r in self._recalcs
                   if r.metric_code == metric_code and r.period == period]
        if effective_scope is not None:
            scope_set = self._scope_set(effective_scope)
            recalcs = [r for r in recalcs if any(oid in scope_set for oid in r.affected_org_ids)]
        return Result(True, "summary", "查询成功", {
            "metric_code": metric_code, "period": period, "scope_org": effective_scope,
            "aggregate": agg_name, "value": aggregate,
            "source": source, "caliber_version": caliber_version, "formula": formula,
            "participating_orgs": sorted(contributions),
            "contributions": contributions,
            "snapshot_versions": [
                {"caliber_version": snap.caliber_version,
                 "aggregate_value": snap.aggregate_value,
                 "participating_count": len(snap.org_ids),
                 "excluded_org_ids": list(snap.excluded_org_ids),
                 "recalculation_id": snap.recalculation_id,
                 "locked_by": snap.locked_by, "reason": snap.reason}
                for snap in chain
            ] if snapshot is not None else [],
            "recalculation_impact": [_recalc_data(r) for r in recalcs],
        })

    # ------------------------------------------------------------- 权限/组织

    def _require_role(self, actor: str, *roles: Role) -> User:
        user = self._users.get(actor)
        if user is None:
            raise PermissionError_(f"未登记的用户: {actor}")
        if user.role not in roles:
            raise PermissionError_(f"角色 {user.role.value} 无权执行该操作")
        return user

    def _ancestors(self, org_id: str) -> list[str]:
        chain = []
        current = self._orgs[org_id].parent_id if org_id in self._orgs else None
        while current is not None:
            chain.append(current)
            current = self._orgs[current].parent_id if current in self._orgs else None
        return chain

    def _descendants(self, org_id: Optional[str]) -> set[str]:
        if org_id is None:
            return set()
        return {
            org.org_id for org in self._orgs.values()
            if org.parent_id is not None and org_id in self._ancestors(org.org_id)
        }

    def _scope_set(self, org_id: str) -> set[str]:
        return {org_id} | self._descendants(org_id)

    def _is_self_or_descendant(self, ancestor: str, org_id: str) -> bool:
        return org_id == ancestor or org_id in self._descendants(ancestor)


# ---------------------------------------------------------------- 辅助函数

def _need(payload: dict[str, Any], key: str) -> Any:
    if key not in payload or payload[key] is None:
        raise DomainError(f"缺少必填字段: {key}")
    return payload[key]


def _need_str(payload: dict[str, Any], key: str) -> str:
    value = _need(payload, key)
    text = str(value).strip()
    if not text:
        raise DomainError(f"字段不能为空: {key}")
    return text


def _snapshot_data(snapshot: Snapshot, scope_org: Optional[str]) -> dict[str, Any]:
    return {
        "metric_code": snapshot.metric_code, "period": snapshot.period,
        "scope_org": scope_org, "caliber_version": snapshot.caliber_version,
        "formula": snapshot.formula, "aggregate_value": snapshot.aggregate_value,
        "org_ids": list(snapshot.org_ids), "contributions": dict(snapshot.contributions),
        "excluded_org_ids": list(snapshot.excluded_org_ids),
        "locked_by": snapshot.locked_by, "reason": snapshot.reason,
        "recalculation_id": snapshot.recalculation_id,
    }


def _recalc_data(recalc: Recalculation) -> dict[str, Any]:
    return {
        "recalc_id": recalc.recalc_id, "old_caliber_version": recalc.old_caliber_version,
        "new_caliber_version": recalc.new_caliber_version,
        "old_value": recalc.old_value, "new_value": recalc.new_value,
        "affected_org_ids": list(recalc.affected_org_ids),
        "excluded_org_ids": list(recalc.excluded_org_ids),
        "reason": recalc.reason, "requested_by": recalc.requested_by,
        "old_snapshot_ref": recalc.old_snapshot_ref,
    }
