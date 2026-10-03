import os
import tempfile
import unittest

from app.contracts import Request
from app.service import SoeMetricsService


def req(service, actor, action, payload, request_id):
    return service.handle(Request(actor, action, payload, request_id))


class World:
    """构造标准场景：集团 G 下有甲 A（含下级 A1）、乙 B。"""

    def __init__(self, service=None):
        self.s = service or SoeMetricsService()
        r = req(self.s, "hq", "add_user", {"user_id": "hq", "role": "hq_admin"}, "b-hq")
        assert r.accepted, r.message
        for rid, args in [
            ("b-g", {"org_id": "G", "name": "集团"}),
            ("b-a", {"org_id": "A", "name": "甲单位", "parent_id": "G"}),
            ("b-b", {"org_id": "B", "name": "乙单位", "parent_id": "G"}),
            ("b-a1", {"org_id": "A1", "name": "甲一下级", "parent_id": "A"}),
        ]:
            assert req(self.s, "hq", "add_org", args, rid).accepted
        for rid, args in [
            ("u-rev", {"user_id": "rev", "role": "reviewer"}),
            ("u-view", {"user_id": "view", "role": "viewer"}),
            ("u-ua", {"user_id": "ua", "role": "unit_admin", "org_id": "A"}),
            ("u-ua1", {"user_id": "ua1", "role": "unit_admin", "org_id": "A1"}),
            ("u-ub", {"user_id": "ub", "role": "unit_admin", "org_id": "B"}),
        ]:
            assert req(self.s, "hq", "add_user", args, rid).accepted
        assert req(self.s, "hq", "define_metric",
                   {"metric_code": "sei", "name": "战新产业产值", "unit": "万元",
                    "inputs": ["base", "rate"]}, "m-sei").accepted

    def caliber(self, formula, reason, version, rid):
        r = req(self.s, "hq", "create_caliber",
                {"metric_code": "sei", "formula": formula, "reason": reason}, rid)
        assert r.accepted and r.data["version"] == version, r.message
        return r


class BootstrapAndPermissionTest(unittest.TestCase):
    def test_bootstrap_first_admin(self):
        s = SoeMetricsService()
        r = req(s, "x", "add_user", {"user_id": "x", "role": "reviewer"}, "1")
        self.assertFalse(r.accepted)
        self.assertEqual(r.state, "rejected")
        r = req(s, "hq", "add_user", {"user_id": "hq", "role": "hq_admin"}, "2")
        self.assertTrue(r.accepted)
        # 引导完成后，非集团管理员不能再登记用户
        r = req(s, "hq", "add_user", {"user_id": "hq2", "role": "hq_admin"}, "3")
        self.assertTrue(r.accepted)
        # 未登记用户执行任何操作都被拒绝
        r = req(s, "nobody", "add_user", {"user_id": "z", "role": "viewer"}, "4")
        self.assertEqual(r.state, "forbidden")

    def test_unknown_user_rejected(self):
        w = World()
        r = req(w.s, "ghost", "query_summary",
                {"metric_code": "sei", "period": "2026-09"}, "x1")
        self.assertEqual(r.state, "forbidden")

    def test_role_boundaries(self):
        w = World()
        w.caliber("base * rate", "初版", 1, "c1")
        # 单位管理员不能维护口径
        r = req(w.s, "ua", "create_caliber",
                {"metric_code": "sei", "formula": "base", "reason": "x"}, "p1")
        self.assertEqual(r.state, "forbidden")
        # 复核人员不能上报
        r = req(w.s, "rev", "submit_data",
                {"metric_code": "sei", "period": "2026-09",
                 "inputs": {"base": 1, "rate": 1}, "value": 1}, "p2")
        self.assertEqual(r.state, "forbidden")
        # 只读用户不能锁定
        r = req(w.s, "view", "lock_snapshot",
                {"metric_code": "sei", "period": "2026-09"}, "p3")
        self.assertEqual(r.state, "forbidden")

    def test_unit_admin_cannot_cross_org(self):
        w = World()
        w.caliber("base * rate", "初版", 1, "c1")
        payload = {"metric_code": "sei", "period": "2026-09", "org_id": "B",
                   "inputs": {"base": 50, "rate": 2}, "value": 100}
        r = req(w.s, "ua", "submit_data", payload, "x1")
        self.assertFalse(r.accepted)
        self.assertEqual(r.state, "forbidden")
        self.assertIn("范围外", r.message)

    def test_unit_admin_may_report_for_descendant(self):
        w = World()
        w.caliber("base * rate", "初版", 1, "c1")
        r = req(w.s, "ua", "submit_data",
                {"metric_code": "sei", "period": "2026-09", "org_id": "A1",
                 "inputs": {"base": 10, "rate": 2}, "value": 20}, "x1")
        self.assertTrue(r.accepted, r.message)


class FormulaAndValidationTest(unittest.TestCase):
    def test_unsafe_formula_rejected(self):
        w = World()
        for bad in ["__import__('os')", "base + rate.__class__", "open('x')",
                    "base ** rate", "(lambda: 1)()", "base +", "base + 'evil'",
                    "max(base, rate, *x)"]:
            r = req(w.s, "hq", "create_caliber",
                    {"metric_code": "sei", "formula": bad, "reason": "恶意/非法"}, f"f-{bad}")
            self.assertFalse(r.accepted, bad)

    def test_unknown_reference_rejected(self):
        w = World()
        r = req(w.s, "hq", "create_caliber",
                {"metric_code": "sei", "formula": "base * unknown_x", "reason": "r"}, "g1")
        self.assertFalse(r.accepted)

    def test_reason_required_for_new_caliber(self):
        w = World()
        r = req(w.s, "hq", "create_caliber",
                {"metric_code": "sei", "formula": "base * rate", "reason": "  "}, "g2")
        self.assertFalse(r.accepted)

    def test_submission_value_must_match_formula(self):
        w = World()
        w.caliber("base * rate", "初版", 1, "c1")
        r = req(w.s, "ua", "submit_data",
                {"metric_code": "sei", "period": "2026-09",
                 "inputs": {"base": 100, "rate": 1.2}, "value": 999}, "v1")
        self.assertFalse(r.accepted)
        self.assertIn("校验", r.message)

    def test_inputs_must_match_definition(self):
        w = World()
        w.caliber("base * rate", "初版", 1, "c1")
        r = req(w.s, "ua", "submit_data",
                {"metric_code": "sei", "period": "2026-09",
                 "inputs": {"base": 100}, "value": 100}, "v2")
        self.assertFalse(r.accepted)


class SubmissionDedupTest(unittest.TestCase):
    def test_same_request_id_is_idempotent(self):
        w = World()
        w.caliber("base * rate", "初版", 1, "c1")
        payload = {"metric_code": "sei", "period": "2026-09",
                   "inputs": {"base": 100, "rate": 1.2}, "value": 120}
        first = req(w.s, "ua", "submit_data", payload, "same-id")
        second = req(w.s, "ua", "submit_data", payload, "same-id")
        self.assertEqual(first, second)
        summary = req(w.s, "rev", "query_summary",
                      {"metric_code": "sei", "period": "2026-09"}, "q1")
        self.assertEqual(summary.data["participating_orgs"], ["A"])

    def test_duplicate_import_does_not_create_second_copy(self):
        w = World()
        w.caliber("base * rate", "初版", 1, "c1")
        payload = {"metric_code": "sei", "period": "2026-07", "import_ref": "BATCH-1",
                   "inputs": {"base": 10, "rate": 2}, "value": 20}
        first = req(w.s, "ua", "import_data", payload, "i1")
        second = req(w.s, "ua", "import_data",
                     {**payload, "value": 20, "inputs": {"base": 999, "rate": 2}}, "i2")
        self.assertEqual(first.state, "submitted")
        self.assertEqual(second.state, "duplicate_ignored")
        summary = req(w.s, "rev", "query_summary",
                      {"metric_code": "sei", "period": "2026-07"}, "q")
        self.assertEqual(summary.data["contributions"]["A"], 20)

    def test_failed_import_does_not_consume_import_ref(self):
        w = World()
        w.caliber("base * rate", "初版", 1, "c1")
        bad = {"metric_code": "sei", "period": "2026-07", "import_ref": "BATCH-X",
               "inputs": {"base": 10, "rate": 2}, "value": 999}
        first = req(w.s, "ua", "import_data", bad, "i1")
        self.assertFalse(first.accepted)  # 校验失败
        # 相同 import_ref 修正后重新导入，不应被判为重复
        good = {**bad, "value": 20}
        second = req(w.s, "ua", "import_data", good, "i2")
        self.assertEqual(second.state, "submitted")
        third = req(w.s, "ua", "import_data", good, "i3")
        self.assertEqual(third.state, "duplicate_ignored")

    def test_resubmit_before_lock_keeps_single_copy(self):
        w = World()
        w.caliber("base * rate", "初版", 1, "c1")
        req(w.s, "ua", "submit_data",
            {"metric_code": "sei", "period": "2026-09",
             "inputs": {"base": 100, "rate": 1.2}, "value": 120}, "s1")
        r = req(w.s, "ua", "submit_data",
                {"metric_code": "sei", "period": "2026-09",
                 "inputs": {"base": 110, "rate": 1.2}, "value": 132}, "s2")
        self.assertEqual(r.state, "resubmitted")
        summary = req(w.s, "rev", "query_summary",
                      {"metric_code": "sei", "period": "2026-09"}, "q")
        self.assertEqual(summary.data["contributions"], {"A": 132})


class SnapshotTest(unittest.TestCase):
    def _locked_world(self):
        w = World()
        w.caliber("base * rate", "初版", 1, "c1")
        req(w.s, "ua", "submit_data",
            {"metric_code": "sei", "period": "2026-09",
             "inputs": {"base": 100, "rate": 1.2}, "value": 120}, "s1")
        req(w.s, "ub", "submit_data",
            {"metric_code": "sei", "period": "2026-09",
             "inputs": {"base": 50, "rate": 2}, "value": 100}, "s2")
        r = req(w.s, "rev", "lock_snapshot",
                {"metric_code": "sei", "period": "2026-09", "reason": "九月复核"}, "lock")
        self.assertTrue(r.accepted, r.message)
        return w, r

    def test_snapshot_aggregates_and_freezes(self):
        _, r = self._locked_world()
        self.assertEqual(r.data["aggregate_value"], 220)
        self.assertEqual(r.data["org_ids"], ["A", "B"])
        self.assertEqual(r.data["caliber_version"], 1)

    def test_locked_data_cannot_be_overwritten(self):
        w, _ = self._locked_world()
        r = req(w.s, "ua", "submit_data",
                {"metric_code": "sei", "period": "2026-09",
                 "inputs": {"base": 100, "rate": 1.2}, "value": 120}, "overwrite")
        self.assertEqual(r.state, "forbidden")
        self.assertIn("锁定", r.message)

    def test_cannot_lock_twice(self):
        w, _ = self._locked_world()
        r = req(w.s, "rev", "lock_snapshot",
                {"metric_code": "sei", "period": "2026-09"}, "lock2")
        self.assertFalse(r.accepted)

    def test_scope_lock_only_covers_subtree(self):
        w = World()
        w.caliber("base * rate", "初版", 1, "c1")
        req(w.s, "ua", "submit_data",
            {"metric_code": "sei", "period": "2026-09",
             "inputs": {"base": 100, "rate": 1.2}, "value": 120}, "s1")
        r = req(w.s, "rev", "lock_snapshot",
                {"metric_code": "sei", "period": "2026-09", "scope_org": "A"}, "lockA")
        self.assertTrue(r.accepted, r.message)
        self.assertEqual(r.data["org_ids"], ["A"])
        # A 被范围锁定后同样不可覆盖
        self.assertEqual(req(w.s, "ua", "submit_data",
                             {"metric_code": "sei", "period": "2026-09",
                              "inputs": {"base": 1, "rate": 1}, "value": 1}, "x").state,
                         "forbidden")


class RecalculationTest(unittest.TestCase):
    def _setup(self):
        w = World()
        w.caliber("base * rate", "初版", 1, "c1")
        req(w.s, "ua", "submit_data",
            {"metric_code": "sei", "period": "2026-09",
             "inputs": {"base": 100, "rate": 1.2}, "value": 120}, "s1")
        req(w.s, "ub", "submit_data",
            {"metric_code": "sei", "period": "2026-09",
             "inputs": {"base": 50, "rate": 2}, "value": 100}, "s2")
        req(w.s, "rev", "lock_snapshot",
            {"metric_code": "sei", "period": "2026-09", "reason": "锁定v1"}, "lock")
        return w

    def test_recalc_preserves_old_snapshot_and_impact(self):
        w = self._setup()
        r = req(w.s, "hq", "create_caliber",
                {"metric_code": "sei", "formula": "base * rate + 10",
                 "reason": "2030规划口径调整"}, "c2")
        self.assertTrue(r.accepted)
        self.assertEqual(r.data["version"], 2)
        r = req(w.s, "rev", "recalculate",
                {"metric_code": "sei", "period": "2026-09", "reason": "规划口径更新"}, "rc")
        self.assertTrue(r.accepted, r.message)
        self.assertEqual(r.data["old_value"], 220)
        self.assertEqual(r.data["new_value"], 240)
        self.assertEqual(r.data["affected_org_ids"], ["A", "B"])
        # 原快照保留，值不变
        self.assertEqual(r.data["old_snapshot"]["aggregate_value"], 220)
        self.assertEqual(r.data["old_snapshot"]["caliber_version"], 1)
        self.assertEqual(r.data["new_snapshot"]["caliber_version"], 2)
        self.assertIn("#v1", r.data["old_snapshot_ref"])

        q = req(w.s, "rev", "query_summary",
                {"metric_code": "sei", "period": "2026-09"}, "q")
        self.assertEqual(q.data["value"], 240)
        self.assertEqual(q.data["caliber_version"], 2)
        # 查询返回参与单位、口径版本、重算影响，且快照链上两个版本都在
        self.assertEqual(q.data["participating_orgs"], ["A", "B"])
        self.assertEqual([v["caliber_version"] for v in q.data["snapshot_versions"]], [1, 2])
        impact = q.data["recalculation_impact"]
        self.assertEqual(len(impact), 1)
        self.assertEqual(impact[0]["reason"], "规划口径更新")
        self.assertEqual(impact[0]["affected_org_ids"], ["A", "B"])

    def test_recalc_requires_reviewer_and_reason(self):
        w = self._setup()
        req(w.s, "hq", "create_caliber",
            {"metric_code": "sei", "formula": "base * rate + 1", "reason": "调整"}, "c2")
        self.assertEqual(req(w.s, "ua", "recalculate",
                             {"metric_code": "sei", "period": "2026-09",
                              "reason": "x"}, "r1").state, "forbidden")
        r = req(w.s, "rev", "recalculate",
                {"metric_code": "sei", "period": "2026-09"}, "r2")
        self.assertFalse(r.accepted)  # 缺少原因

    def test_recalc_same_version_rejected(self):
        w = self._setup()
        r = req(w.s, "rev", "recalculate",
                {"metric_code": "sei", "period": "2026-09", "reason": "再算一次"}, "rc")
        self.assertFalse(r.accepted)
        self.assertIn("无需重算", r.message)

    def test_recalc_new_input_marks_unprovided_units(self):
        w = self._setup()
        # 新口径引入调整系数 extra；已锁定快照没有该输入，相关单位受影响且待补报
        r = req(w.s, "hq", "extend_metric_inputs",
                {"metric_code": "sei", "new_inputs": ["extra"]}, "ext")
        self.assertTrue(r.accepted, r.message)
        self.assertEqual(r.data["added"], ["extra"])
        r = req(w.s, "hq", "create_caliber",
                {"metric_code": "sei", "formula": "base * rate + extra", "reason": "新增调整系数"}, "c3")
        self.assertTrue(r.accepted, r.message)
        r = req(w.s, "rev", "recalculate",
                {"metric_code": "sei", "period": "2026-09", "reason": "规划新增调整项"}, "rc")
        self.assertTrue(r.accepted, r.message)
        # 两单位都缺 extra：全部受影响、全部暂不计入新快照
        self.assertEqual(r.data["affected_org_ids"], ["A", "B"])
        self.assertEqual(r.data["excluded_org_ids"], ["A", "B"])
        self.assertEqual(r.data["new_snapshot"]["org_ids"], [])
        self.assertEqual(r.data["new_value"], 0)
        # 原快照仍然完整保留
        self.assertEqual(r.data["old_snapshot"]["aggregate_value"], 220)


class QueryScopeTest(unittest.TestCase):
    def test_unit_admin_query_restricted_to_subtree(self):
        w = World()
        w.caliber("base * rate", "初版", 1, "c1")
        req(w.s, "ua", "submit_data",
            {"metric_code": "sei", "period": "2026-09",
             "inputs": {"base": 100, "rate": 1.2}, "value": 120}, "s1")
        req(w.s, "ub", "submit_data",
            {"metric_code": "sei", "period": "2026-09",
             "inputs": {"base": 50, "rate": 2}, "value": 100}, "s2")
        req(w.s, "rev", "lock_snapshot",
            {"metric_code": "sei", "period": "2026-09"}, "lock")
        # A 单位管理员查全集团：默认范围即本单位；显式查 B 被拒
        r = req(w.s, "ua", "query_summary",
                {"metric_code": "sei", "period": "2026-09", "scope_org": "B"}, "q1")
        self.assertEqual(r.state, "forbidden")
        r = req(w.s, "ua", "query_summary",
                {"metric_code": "sei", "period": "2026-09"}, "q2")
        self.assertEqual(r.data["participating_orgs"], ["A"])
        self.assertEqual(r.data["value"], 120)

    def test_live_query_before_lock(self):
        w = World()
        w.caliber("base * rate", "初版", 1, "c1")
        req(w.s, "ua", "submit_data",
            {"metric_code": "sei", "period": "2026-08",
             "inputs": {"base": 3, "rate": 4}, "value": 12}, "s1")
        r = req(w.s, "view", "query_summary",
                {"metric_code": "sei", "period": "2026-08"}, "q")
        self.assertEqual(r.data["source"], "live_submissions")
        self.assertEqual(r.data["value"], 12)
        self.assertEqual(r.data["caliber_version"], 1)


class PersistenceTest(unittest.TestCase):
    def test_sqlite_replay_restores_state_and_idempotency(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "metrics.db")
            s1 = SoeMetricsService(db)
            w = World(s1)
            w.caliber("base * rate", "初版", 1, "c1")
            req(s1, "ua", "submit_data",
                {"metric_code": "sei", "period": "2026-09",
                 "inputs": {"base": 100, "rate": 1.2}, "value": 120}, "s1")
            req(s1, "rev", "lock_snapshot",
                {"metric_code": "sei", "period": "2026-09", "reason": "锁定"}, "lock")
            req(s1, "hq", "create_caliber",
                {"metric_code": "sei", "formula": "base * rate + 5", "reason": "调整"}, "c2")
            req(s1, "rev", "recalculate",
                {"metric_code": "sei", "period": "2026-09", "reason": "重启前重算"}, "rc")
            # 重复请求（同一 request_id）在重启前返回缓存
            before = req(s1, "ua", "submit_data",
                         {"metric_code": "sei", "period": "2026-09",
                          "inputs": {"base": 100, "rate": 1.2}, "value": 120}, "s1")
            s1.close()

            s2 = SoeMetricsService(db)
            q = req(s2, "rev", "query_summary",
                    {"metric_code": "sei", "period": "2026-09"}, "q-after")
            self.assertTrue(q.accepted)
            self.assertEqual(q.data["value"], 125)
            self.assertEqual(q.data["caliber_version"], 2)
            self.assertEqual([v["caliber_version"] for v in q.data["snapshot_versions"]], [1, 2])
            # 同一 request_id 重启后仍返回首次结果，不产生第二份数据
            after = req(s2, "ua", "submit_data",
                        {"metric_code": "sei", "period": "2026-09",
                         "inputs": {"base": 100, "rate": 1.2}, "value": 120}, "s1")
            self.assertEqual(before.state, after.state)
            self.assertEqual(after.state, "submitted")
            # 换一个新 request_id：锁定状态经重放恢复，覆盖仍被拒绝
            blocked = req(s2, "ua", "submit_data",
                          {"metric_code": "sei", "period": "2026-09",
                           "inputs": {"base": 100, "rate": 1.2}, "value": 120}, "new-after-restart")
            self.assertEqual(blocked.state, "forbidden")
            s2.close()


if __name__ == "__main__":
    unittest.main()
