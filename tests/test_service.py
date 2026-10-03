"""央企指标管理 行为测试。

 fixture：集团 GRP 下辖 SUB1（U1、U2）与 SUB2（U3）；
 指标 SEI（战略性新兴产业收入，SUM，单位亿元），口径 v1 = rev - adjust，适用层级 2。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

from app.contracts import Request
from app.service import SoeMetricsService

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

V1_INPUTS = {"fields": [
    {"name": "rev", "min": 0},
    {"name": "adjust", "required": False, "default": 0},
]}
V2_INPUTS = {"fields": [
    {"name": "rev", "min": 0},
    {"name": "adjust", "required": False, "default": 0},
    {"name": "tax", "required": False, "default": 0},
]}


class Harness:
    """搭好组织、角色、指标与口径 v1 的服务实例。"""

    def __init__(self):
        self.service = SoeMetricsService()
        self._seq = 0
        self._must("installer", "system.bootstrap",
                   {"root_org_id": "GRP", "root_name": "集团总部", "admin_actor": "ga"})
        for org_id, name, parent in [
            ("SUB1", "第一子集团", "GRP"), ("SUB2", "第二子集团", "GRP"),
            ("U1", "甲公司", "SUB1"), ("U2", "乙公司", "SUB1"), ("U3", "丙公司", "SUB2"),
        ]:
            self._must("ga", "org.register", {"org_id": org_id, "name": name, "parent_id": parent})
        for actor, role, org in [
            ("rev", "reviewer", "GRP"), ("adm", "admin", "SUB1"),
            ("rep1", "unit_reporter", "U1"), ("rep2", "unit_reporter", "U2"),
            ("rep3", "unit_reporter", "U3"),
        ]:
            self._must("ga", "actor.grant", {"actor_id": actor, "role": role, "org_id": org})
        self._must("ga", "metric.define", {
            "metric_code": "SEI", "name": "战略性新兴产业收入", "unit": "亿元", "aggregation": "SUM"})
        self._must("ga", "caliber.create", {
            "metric_code": "SEI", "version": 1, "org_level": 2,
            "formula": "rev - adjust", "inputs": V1_INPUTS})
        self._must("ga", "caliber.activate", {"metric_code": "SEI", "version": 1})

    def __call__(self, actor, action, payload=None, rid=None):
        self._seq += 1
        return self.service.handle(
            Request(actor, action, payload or {}, rid or f"auto-{self._seq}"))

    def _must(self, actor, action, payload):
        result = self(actor, action, payload)
        assert result.accepted, f"fixture 失败: {action} -> {result.message}"

    def submit(self, actor, unit, values, period="2030-01", metric="SEI", rid=None):
        return self(actor, "data.submit",
                    {"unit_id": unit, "metric_code": metric, "period": period, "values": values},
                    rid=rid)

    def submit_all(self):
        self._must_submit("rep1", "U1", {"rev": 100})
        self._must_submit("rep2", "U2", {"rev": 200, "adjust": 50})
        self._must_submit("rep3", "U3", {"rev": 300})

    def _must_submit(self, actor, unit, values):
        result = self.submit(actor, unit, values)
        assert result.accepted, f"fixture 填报失败: {result.message}"

    def lock(self, org="GRP", rid=None):
        return self("rev", "snapshot.lock",
                    {"metric_code": "SEI", "period": "2030-01", "org_id": org}, rid=rid)

    def aggregate(self, actor="ga", org="GRP", **extra):
        payload = {"metric_code": "SEI", "period": "2030-01", "org_id": org}
        payload.update(extra)
        return self(actor, "query.aggregate", payload)

    def advance_to_v2(self):
        """v1 数据锁定后启用 v2（新增 tax 字段），U1 按新口径补报，再发起重算。"""
        self.submit_all()
        self._must("rev", "snapshot.lock", {"metric_code": "SEI", "period": "2030-01", "org_id": "GRP"})
        self._must("ga", "caliber.create", {
            "metric_code": "SEI", "version": 2, "org_level": 2,
            "formula": "rev - adjust - tax", "inputs": V2_INPUTS})
        self._must("ga", "caliber.activate", {"metric_code": "SEI", "version": 2})
        self._must_submit("rep1", "U1", {"rev": 100, "tax": 5})
        return self("ga", "recalc.start",
                    {"metric_code": "SEI", "period": "2030-01", "reason": "口径升级：扣除税费"})


class BootstrapAndMasterDataTest(unittest.TestCase):
    def test_bootstrap_twice_rejected(self):
        h = Harness()
        again = h("installer", "system.bootstrap",
                  {"root_org_id": "X", "root_name": "X", "admin_actor": "y"})
        self.assertFalse(again.accepted)
        self.assertEqual(again.data["reason_code"], "CONFLICT")

    def test_unknown_actor_rejected(self):
        h = Harness()
        result = h.aggregate(actor="nobody")
        self.assertFalse(result.accepted)
        self.assertEqual(result.data["reason_code"], "FORBIDDEN")

    def test_unknown_action_rejected(self):
        h = Harness()
        result = h("ga", "explode", {})
        self.assertFalse(result.accepted)
        self.assertEqual(result.data["reason_code"], "UNKNOWN_ACTION")

    def test_org_register_rules(self):
        h = Harness()
        same = h("ga", "org.register", {"org_id": "SUB1", "name": "第一子集团", "parent_id": "GRP"})
        self.assertTrue(same.accepted)
        self.assertEqual(same.state, "exists")
        conflict = h("ga", "org.register", {"org_id": "SUB1", "name": "改名", "parent_id": "GRP"})
        self.assertFalse(conflict.accepted)
        missing_parent = h("ga", "org.register", {"org_id": "X", "name": "X公司", "parent_id": "NOPE"})
        self.assertEqual(missing_parent.data["reason_code"], "NOT_FOUND")
        no_parent = h("ga", "org.register", {"org_id": "Y", "name": "Y公司"})
        self.assertFalse(no_parent.accepted)
        forbidden = h("rep1", "org.register", {"org_id": "Z", "name": "Z公司", "parent_id": "GRP"})
        self.assertEqual(forbidden.data["reason_code"], "FORBIDDEN")

    def test_actor_grant_rules(self):
        h = Harness()
        bad_role = h("ga", "actor.grant", {"actor_id": "x", "role": "boss", "org_id": "GRP"})
        self.assertFalse(bad_role.accepted)
        bad_org = h("ga", "actor.grant", {"actor_id": "x", "role": "admin", "org_id": "NOPE"})
        self.assertEqual(bad_org.data["reason_code"], "NOT_FOUND")
        updated = h("ga", "actor.grant", {"actor_id": "rep1", "role": "admin", "org_id": "SUB1"})
        self.assertEqual(updated.state, "updated")
        forbidden = h("adm", "actor.grant", {"actor_id": "y", "role": "admin", "org_id": "SUB1"})
        self.assertEqual(forbidden.data["reason_code"], "FORBIDDEN")

    def test_metric_define_conflict(self):
        h = Harness()
        same = h("ga", "metric.define", {
            "metric_code": "SEI", "name": "战略性新兴产业收入", "unit": "亿元", "aggregation": "SUM"})
        self.assertEqual(same.state, "exists")
        conflict = h("ga", "metric.define", {"metric_code": "SEI", "name": "改名"})
        self.assertEqual(conflict.data["reason_code"], "CONFLICT")
        bad_agg = h("ga", "metric.define", {"metric_code": "M2", "name": "m", "aggregation": "TOTAL"})
        self.assertFalse(bad_agg.accepted)


class CaliberDefinitionTest(unittest.TestCase):
    def test_formula_must_reference_declared_fields(self):
        h = Harness()
        result = h("ga", "caliber.create", {
            "metric_code": "SEI", "version": 2, "org_level": 2,
            "formula": "rev - tax", "inputs": V1_INPUTS})
        self.assertFalse(result.accepted)
        self.assertIn("未定义的字段", result.message)

    def test_formula_rejects_unsafe_elements(self):
        h = Harness()
        result = h("ga", "caliber.create", {
            "metric_code": "SEI", "version": 2, "org_level": 2,
            "formula": "__import__('os')", "inputs": V1_INPUTS})
        self.assertFalse(result.accepted)

    def test_inputs_spec_validated(self):
        h = Harness()
        not_dict = h("ga", "caliber.create", {
            "metric_code": "SEI", "version": 2, "org_level": 2,
            "formula": "rev", "inputs": "not-a-spec"})
        self.assertFalse(not_dict.accepted)
        bad_range = h("ga", "caliber.create", {
            "metric_code": "SEI", "version": 2, "org_level": 2, "formula": "rev",
            "inputs": {"fields": [{"name": "rev", "min": 10, "max": 1}]}})
        self.assertFalse(bad_range.accepted)

    def test_duplicate_version_rejected(self):
        h = Harness()
        result = h("ga", "caliber.create", {
            "metric_code": "SEI", "version": 1, "org_level": 2,
            "formula": "rev", "inputs": V1_INPUTS})
        self.assertEqual(result.data["reason_code"], "CONFLICT")

    def test_activate_archives_previous(self):
        h = Harness()
        h("ga", "caliber.create", {
            "metric_code": "SEI", "version": 2, "org_level": 2,
            "formula": "rev - adjust - tax", "inputs": V2_INPUTS})
        activated = h("ga", "caliber.activate", {"metric_code": "SEI", "version": 2})
        self.assertEqual(activated.data["previous_active_version"], 1)
        again = h("ga", "caliber.activate", {"metric_code": "SEI", "version": 2})
        self.assertEqual(again.state, "active")


class SubmissionTest(unittest.TestCase):
    def test_requires_active_caliber(self):
        h = Harness()
        h("ga", "metric.define", {"metric_code": "NEW1", "name": "新指标"})
        no_caliber = h.submit("rep1", "U1", {"rev": 1}, metric="NEW1")
        self.assertEqual(no_caliber.data["reason_code"], "CONFLICT")
        h("ga", "caliber.create", {
            "metric_code": "NEW1", "version": 1, "org_level": 2,
            "formula": "rev", "inputs": V1_INPUTS})
        still_draft = h.submit("rep1", "U1", {"rev": 1}, metric="NEW1")
        self.assertFalse(still_draft.accepted)

    def test_field_validation(self):
        h = Harness()
        bad_period = h.submit("rep1", "U1", {"rev": 1}, period="2030-13")
        self.assertIn("周期格式", bad_period.message)
        missing = h.submit("rep1", "U1", {})
        self.assertIn("缺少必填字段", missing.message)
        unknown = h.submit("rep1", "U1", {"rev": 1, "foo": 2})
        self.assertIn("未定义的字段", unknown.message)
        below_min = h.submit("rep1", "U1", {"rev": -5})
        self.assertIn("低于下限", below_min.message)
        not_number = h.submit("rep1", "U1", {"rev": "100"})
        self.assertIn("必须是数字", not_number.message)

    def test_unit_level_must_match_caliber(self):
        h = Harness()
        result = h("ga", "data.submit",
                   {"unit_id": "SUB1", "metric_code": "SEI", "period": "2030-01", "values": {"rev": 1}})
        self.assertFalse(result.accepted)
        self.assertIn("层级", result.message)

    def test_submit_permission_scope(self):
        h = Harness()
        own = h.submit("rep1", "U1", {"rev": 1})
        self.assertTrue(own.accepted)
        other_unit = h.submit("rep1", "U2", {"rev": 1})
        self.assertEqual(other_unit.data["reason_code"], "FORBIDDEN")
        admin_in_scope = h.submit("adm", "U2", {"rev": 1})
        self.assertTrue(admin_in_scope.accepted)
        admin_out_of_scope = h.submit("adm", "U3", {"rev": 1})
        self.assertEqual(admin_out_of_scope.data["reason_code"], "FORBIDDEN")
        reviewer = h.submit("rev", "U1", {"rev": 1})
        self.assertEqual(reviewer.data["reason_code"], "FORBIDDEN")
        group_admin = h.submit("ga", "U3", {"rev": 1})
        self.assertTrue(group_admin.accepted)

    def test_formula_division_by_zero_rejected(self):
        h = Harness()
        h("ga", "metric.define", {"metric_code": "RATIO", "name": "投入产出比"})
        h("ga", "caliber.create", {
            "metric_code": "RATIO", "version": 1, "org_level": 2,
            "formula": "rev / adjust", "inputs": V1_INPUTS})
        h("ga", "caliber.activate", {"metric_code": "RATIO", "version": 1})
        result = h.submit("rep1", "U1", {"rev": 10}, metric="RATIO")
        self.assertFalse(result.accepted)
        self.assertIn("除数为零", result.message)


class IdempotencyTest(unittest.TestCase):
    def test_same_request_replays_identically(self):
        h = Harness()
        first = h.submit("rep1", "U1", {"rev": 100}, rid="idem-1")
        second = h.submit("rep1", "U1", {"rev": 100}, rid="idem-1")
        self.assertEqual(first, second)
        self.assertEqual(first.data["created"], 1)  # 重放返回首次结果，而不是“未变化”
        self.assertEqual(h.aggregate().data["value"], 100.0)

    def test_idempotency_key_conflict(self):
        h = Harness()
        h.submit("rep1", "U1", {"rev": 100}, rid="k-1")
        conflict = h.submit("rep1", "U1", {"rev": 999}, rid="k-1")
        self.assertFalse(conflict.accepted)
        self.assertEqual(conflict.data["reason_code"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(h.aggregate().data["value"], 100.0)

    def test_reimport_never_duplicates(self):
        h = Harness()
        items = [
            {"unit_id": "U1", "metric_code": "SEI", "period": "2030-01", "values": {"rev": 100}},
            {"unit_id": "U2", "metric_code": "SEI", "period": "2030-01", "values": {"rev": 200, "adjust": 50}},
        ]
        first = h("adm", "data.import", {"items": items})
        self.assertEqual((first.data["created"], first.data["updated"], first.data["unchanged"]), (2, 0, 0))
        second = h("adm", "data.import", {"items": items})  # 换幂等键重复导入同一批
        self.assertEqual((second.data["created"], second.data["updated"], second.data["unchanged"]), (0, 0, 2))
        agg = h.aggregate(org="SUB1")
        self.assertEqual(agg.data["value"], 250.0)
        self.assertEqual(agg.data["unit_count"], 2)
        corrected = h("adm", "data.import", {"items": [dict(items[0], values={"rev": 120})]})
        self.assertEqual(corrected.data["updated"], 1)
        self.assertEqual(h.aggregate(org="SUB1").data["value"], 270.0)

    def test_import_is_all_or_nothing(self):
        h = Harness()
        items = [
            {"unit_id": "U1", "metric_code": "SEI", "period": "2030-01", "values": {"rev": 100}},
            {"unit_id": "U2", "metric_code": "SEI", "period": "2030-01", "values": {"adjust": 1}},
        ]
        result = h("adm", "data.import", {"items": items})
        self.assertFalse(result.accepted)
        self.assertIn("第 2 条", result.message)
        self.assertIsNone(h.aggregate().data["value"])  # 第一条也没有落库


class SnapshotLockTest(unittest.TestCase):
    def test_only_reviewer_can_lock(self):
        h = Harness()
        h.submit_all()
        by_admin = h("adm", "snapshot.lock", {"metric_code": "SEI", "period": "2030-01", "org_id": "SUB1"})
        self.assertEqual(by_admin.data["reason_code"], "FORBIDDEN")
        by_group_admin = h("ga", "snapshot.lock", {"metric_code": "SEI", "period": "2030-01", "org_id": "GRP"})
        self.assertEqual(by_group_admin.data["reason_code"], "FORBIDDEN")

    def test_lock_requires_data(self):
        h = Harness()
        result = h.lock()
        self.assertFalse(result.accepted)
        self.assertIn("没有可锁定的数据", result.message)

    def test_locked_snapshot_blocks_overwrite_for_everyone(self):
        h = Harness()
        h.submit_all()
        locked = h.lock()
        self.assertTrue(locked.accepted)
        self.assertEqual(locked.data["total"], 550.0)
        by_reporter = h.submit("rep1", "U1", {"rev": 111})
        self.assertEqual(by_reporter.data["reason_code"], "LOCKED")
        by_admin = h.submit("adm", "U1", {"rev": 111})
        self.assertEqual(by_admin.data["reason_code"], "LOCKED")
        by_group_admin = h.submit("ga", "U1", {"rev": 111})
        self.assertEqual(by_group_admin.data["reason_code"], "LOCKED")
        self.assertEqual(h.aggregate().data["value"], 550.0)

    def test_lock_scope_is_subtree(self):
        h = Harness()
        h.submit_all()
        h.lock(org="SUB1")
        blocked = h.submit("rep1", "U1", {"rev": 111})
        self.assertEqual(blocked.data["reason_code"], "LOCKED")
        outside = h.submit("rep3", "U3", {"rev": 333})
        self.assertTrue(outside.accepted)

    def test_double_lock_conflicts_but_replay_is_stable(self):
        h = Harness()
        h.submit_all()
        first = h.lock(rid="lock-1")
        replay = h.lock(rid="lock-1")
        self.assertEqual(first, replay)
        second = h.lock()
        self.assertEqual(second.data["reason_code"], "CONFLICT")

    def test_query_reads_snapshot_after_lock(self):
        h = Harness()
        h.submit_all()
        h.lock()
        agg = h.aggregate()
        self.assertEqual(agg.data["source"], "snapshot")
        self.assertEqual(agg.data["snapshot_id"], "SEI:2030-01:GRP@v1")
        self.assertEqual(agg.data["locked_by"], "rev")
        self.assertEqual(agg.data["value"], 550.0)


class AggregateQueryTest(unittest.TestCase):
    def test_returns_value_units_caliber_and_recalc_context(self):
        h = Harness()
        h.submit_all()
        agg = h.aggregate()
        self.assertEqual(agg.data["value"], 550.0)
        self.assertEqual(agg.data["caliber_version"], 1)
        self.assertEqual(agg.data["caliber_id"], "SEI@v1")
        self.assertEqual(agg.data["aggregation"], "SUM")
        self.assertEqual(agg.data["unit"], "亿元")
        self.assertEqual(agg.data["source"], "live")
        self.assertEqual([u["unit_id"] for u in agg.data["units"]], ["U1", "U2", "U3"])
        self.assertEqual(agg.data["recalcs"], [])

    def test_aggregate_respects_subtree(self):
        h = Harness()
        h.submit_all()
        sub1 = h.aggregate(org="SUB1")
        self.assertEqual(sub1.data["value"], 250.0)
        self.assertEqual([u["unit_id"] for u in sub1.data["units"]], ["U1", "U2"])

    def test_avg_aggregation(self):
        h = Harness()
        h("ga", "metric.define", {"metric_code": "RATE", "name": "某比率", "unit": "%", "aggregation": "AVG"})
        h("ga", "caliber.create", {
            "metric_code": "RATE", "version": 1, "org_level": 2, "formula": "score",
            "inputs": {"fields": [{"name": "score", "min": 0, "max": 100}]}})
        h("ga", "caliber.activate", {"metric_code": "RATE", "version": 1})
        h.submit("rep1", "U1", {"score": 80}, metric="RATE")
        h.submit("rep2", "U2", {"score": 90}, metric="RATE")
        agg = h("ga", "query.aggregate", {"metric_code": "RATE", "period": "2030-01", "org_id": "SUB1"})
        self.assertEqual(agg.data["value"], 85.0)

    def test_unknown_caliber_rejected(self):
        h = Harness()
        h.submit_all()
        by_version = h.aggregate(caliber_version=99)
        self.assertEqual(by_version.data["reason_code"], "NOT_FOUND")
        by_id = h.aggregate(caliber_id="SEI@v99")
        self.assertEqual(by_id.data["reason_code"], "NOT_FOUND")


class RecalcTest(unittest.TestCase):
    def test_requires_reason(self):
        h = Harness()
        h.submit_all()
        result = h("ga", "recalc.start", {"metric_code": "SEI", "period": "2030-01", "reason": "  "})
        self.assertFalse(result.accepted)
        self.assertIn("变更原因", result.message)

    def test_requires_group_admin(self):
        h = Harness()
        h.submit_all()
        for actor in ("rev", "adm", "rep1"):
            result = h(actor, "recalc.start",
                       {"metric_code": "SEI", "period": "2030-01", "reason": "越权测试"})
            self.assertEqual(result.data["reason_code"], "FORBIDDEN", actor)

    def test_target_caliber_must_be_active(self):
        h = Harness()
        h.submit_all()
        h("ga", "caliber.create", {
            "metric_code": "SEI", "version": 2, "org_level": 2,
            "formula": "rev - adjust - tax", "inputs": V2_INPUTS})
        result = h("ga", "recalc.start",
                   {"metric_code": "SEI", "period": "2030-01", "reason": "x", "to_version": 2})
        self.assertFalse(result.accepted)
        self.assertIn("未启用", result.message)

    def test_recalc_recomputes_and_preserves_original(self):
        h = Harness()
        recalc = h.advance_to_v2()
        self.assertTrue(recalc.accepted)
        self.assertEqual(recalc.data["from_version"], 1)
        self.assertEqual(recalc.data["to_version"], 2)
        impacts = {i["unit_id"]: i for i in recalc.data["impacts"]}
        # U1 已按新口径自行补报，重算不得覆盖
        self.assertEqual(impacts["U1"]["status"], "kept_existing")
        self.assertEqual(impacts["U1"]["new_value"], 95.0)
        self.assertEqual(impacts["U2"]["status"], "recalculated")
        self.assertEqual(impacts["U2"]["new_value"], 150.0)
        self.assertEqual(impacts["U3"]["new_value"], 300.0)
        # 原快照保留：按 v1 查询仍返回锁定值
        old = h.aggregate(caliber_version=1)
        self.assertEqual(old.data["source"], "snapshot")
        self.assertEqual(old.data["value"], 550.0)
        # 新口径汇总 = 95 + 150 + 300，并带回重算影响
        new = h.aggregate()
        self.assertEqual(new.data["caliber_version"], 2)
        self.assertEqual(new.data["source"], "live")
        self.assertEqual(new.data["value"], 545.0)
        self.assertEqual(len(new.data["recalcs"]), 1)
        self.assertEqual(new.data["recalcs"][0]["reason"], "口径升级：扣除税费")
        self.assertEqual(len(new.data["recalcs"][0]["impacts"]), 3)
        # 子树查询只带回该范围的重算影响
        sub2 = h.aggregate(org="SUB2")
        self.assertEqual([i["unit_id"] for i in sub2.data["recalcs"][0]["impacts"]], ["U3"])

    def test_recalc_duplicate_rejected(self):
        h = Harness()
        h.advance_to_v2()
        again = h("ga", "recalc.start",
                  {"metric_code": "SEI", "period": "2030-01", "reason": "重复发起"})
        self.assertEqual(again.data["reason_code"], "DUPLICATE")

    def test_recalc_records_missing_input(self):
        h = Harness()
        h.advance_to_v2()
        h("ga", "caliber.create", {
            "metric_code": "SEI", "version": 3, "org_level": 2,
            "formula": "rev - adjust - deduction",
            "inputs": {"fields": [
                {"name": "rev", "min": 0},
                {"name": "adjust", "required": False, "default": 0},
                {"name": "deduction"},
            ]}})
        h("ga", "caliber.activate", {"metric_code": "SEI", "version": 3})
        ambiguous = h("ga", "recalc.start",
                      {"metric_code": "SEI", "period": "2030-01", "reason": "新增扣减项"})
        self.assertFalse(ambiguous.accepted)
        self.assertIn("多个来源口径", ambiguous.message)
        recalc = h("ga", "recalc.start",
                   {"metric_code": "SEI", "period": "2030-01", "reason": "新增扣减项", "from_version": 2})
        self.assertTrue(recalc.accepted)
        self.assertTrue(all(i["status"] == "missing_input" for i in recalc.data["impacts"]))
        current = h.aggregate()
        self.assertEqual(current.data["caliber_version"], 3)
        self.assertIsNone(current.data["value"])
        self.assertEqual(len(current.data["recalcs"]), 2)
        # 历史口径数据始终保留
        self.assertEqual(h.aggregate(caliber_version=1).data["value"], 550.0)
        self.assertEqual(h.aggregate(caliber_version=2).data["value"], 545.0)

    def test_recalc_cannot_overwrite_locked_target(self):
        h = Harness()
        h.submit_all()
        h("ga", "caliber.create", {
            "metric_code": "SEI", "version": 2, "org_level": 2,
            "formula": "rev - adjust - tax", "inputs": V2_INPUTS})
        h("ga", "caliber.activate", {"metric_code": "SEI", "version": 2})
        h.submit("rep1", "U1", {"rev": 100, "tax": 5})
        h.lock()  # 锁定的是启用口径 v2 的快照
        result = h("ga", "recalc.start",
                   {"metric_code": "SEI", "period": "2030-01", "reason": "试图覆盖已锁定口径"})
        self.assertEqual(result.data["reason_code"], "LOCKED")


class CliTest(unittest.TestCase):
    def test_cli_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, SOE_METRICS_DB=os.path.join(tmp, "soe.db"))

            def run(item):
                proc = subprocess.run(
                    [sys.executable, "-m", "app.api"], input=json.dumps(item),
                    capture_output=True, text=True, env=env, cwd=ROOT)
                return proc, json.loads(proc.stdout)

            proc, boot = run({"actor": "installer", "action": "system.bootstrap",
                              "payload": {"root_org_id": "GRP", "root_name": "集团", "admin_actor": "ga"},
                              "request_id": "c-1"})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(boot["accepted"])
            proc, replay = run({"actor": "installer", "action": "system.bootstrap",
                                "payload": {"root_org_id": "GRP", "root_name": "集团", "admin_actor": "ga"},
                                "request_id": "c-1"})
            self.assertEqual(boot, replay)  # 换进程重放同一幂等键，结果一致
            proc, rejected = run({"actor": "ga", "action": "nope", "payload": {}, "request_id": "c-2"})
            self.assertEqual(proc.returncode, 1)
            self.assertFalse(rejected["accepted"])


if __name__ == "__main__":
    unittest.main()
