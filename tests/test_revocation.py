"""约束撤销建议的纯标准库测试（无需 FastAPI / 网络）。

用法:  python tests/test_revocation.py
"""
import itertools
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = tempfile.mkdtemp(prefix="audit-rev-test-")
os.environ["DATA_DIR"] = DATA_DIR

from app import revocation  # noqa: E402
from app.db import db, init_db  # noqa: E402
from app.feasibility import check_feasibility  # noqa: E402
from app.importer import import_payload, reset_all  # noqa: E402

init_db(load_sample=False)

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "examples",
                       "login-anomaly.json")


def load_sample():
    reset_all()
    with open(EXAMPLE, encoding="utf-8") as f:
        import_payload(json.load(f))


def constraint_id(frm, to, kind=None):
    sql = "SELECT id FROM constraints WHERE from_event=? AND to_event=?"
    args = [frm, to]
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    with db() as conn:
        row = conn.execute(sql, args).fetchone()
    return row["id"] if row else None


def all_constraints():
    with db() as conn:
        rows = conn.execute(
            "SELECT id, from_event, to_event, kind FROM constraints").fetchall()
    return [dict(r) for r in rows]


class SampleCase(unittest.TestCase):
    """内置示例案件：默认假设下单边矛盾，校正假设下可行，造假假设下暴露合规约束。"""

    def setUp(self):
        load_sample()

    def test_default_single_revocation(self):
        r = revocation.suggest_revocations()
        self.assertEqual(r["check_type"], "constraint-revocation-suggestion")
        self.assertFalse(r["base_feasible"])
        self.assertTrue(r["needed"])
        self.assertTrue(r["fixable_by_revocation"])
        self.assertTrue(r["complete"])
        self.assertEqual(r["min_removals"], 1)
        self.assertEqual(len(r["solutions"]), 1)
        sol = r["solutions"][0]
        cid = constraint_id("a1", "m1", "causal")
        self.assertEqual(sol["constraint_ids"], [cid])
        c = sol["constraints"][0]
        self.assertEqual((c["id"], c["from"], c["to"], c["kind"]),
                         (cid, "a1", "m1", "causal"))
        # 撤销后全量可行见证：8 个事件，每个最早可行时刻都落在闭区间内
        w = sol["witness"]
        self.assertEqual(w["event_count"], 8)
        by_id = {e["id"]: e for e in w["events"]}
        for e in w["events"]:
            self.assertLessEqual(e["lo"], e["earliest"] + 1e-6)
            self.assertLessEqual(e["earliest"], e["hi"] + 1e-6)
        # 见证满足除被撤销约束外的全部约束（含 source_order）
        for edge in all_constraints():
            if edge["id"] == cid:
                continue
            self.assertLessEqual(by_id[edge["from_event"]]["earliest"],
                                 by_id[edge["to_event"]]["earliest"] + 1e-6)
        # 统计：6 条可撤销（人工/导入），5 条 source_order 不可撤销
        self.assertEqual(r["stats"]["revocable_constraints"], 6)
        self.assertEqual(r["stats"]["non_revocable_constraints"], 5)
        self.assertLessEqual(r["stats"]["solve_count"], r["limits"]["max_solves"])

    def test_feasible_assumptions_no_revocation_needed(self):
        r = revocation.suggest_revocations({"app-server": {"clock_offset": 50},
                                            "mobile-app": {"clock_offset": -30}})
        self.assertTrue(r["base_feasible"])
        self.assertFalse(r["needed"])
        self.assertEqual(r["min_removals"], 0)
        self.assertEqual(r["solutions"], [])
        self.assertEqual(r["stats"]["solve_count"], 1)  # 只跑了基线

    def test_fraud_assumptions_point_to_claimed_constraint(self):
        # 校正时钟后矛盾转移到合规声明 g3→a3：撤销建议应指向该 claimed 约束
        r = revocation.suggest_revocations({"app-server": {"clock_offset": 60},
                                            "mobile-app": {"clock_offset": -30,
                                                           "uncertainty": 1}})
        self.assertFalse(r["base_feasible"])
        self.assertEqual(r["min_removals"], 1)
        self.assertEqual(len(r["solutions"]), 1)
        c = r["solutions"][0]["constraints"][0]
        self.assertEqual((c["from"], c["to"], c["kind"]), ("g3", "a3", "claimed"))

    def test_read_only_preview(self):
        with db() as conn:
            before = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                      for t in ("events", "sources", "constraints")}
            stored_before = [(r["id"], r["clock_offset"], r["uncertainty"])
                             for r in conn.execute(
                                 "SELECT id, clock_offset, uncertainty FROM sources")]
        revocation.suggest_revocations({"app-server": {"clock_offset": 60}})
        with db() as conn:
            after = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                     for t in ("events", "sources", "constraints")}
            stored_after = [(r["id"], r["clock_offset"], r["uncertainty"])
                            for r in conn.execute(
                                "SELECT id, clock_offset, uncertainty FROM sources")]
        self.assertEqual(before, after)          # 预览不写库
        self.assertEqual(stored_before, stored_after)  # overrides 未持久化


class SyntheticCases(unittest.TestCase):
    """合成数据：环、平行边、独立矛盾对、不可撤销边。"""

    def test_cycle_two_of_three_edges_fix(self):
        # 三环无公共交集：撤 x→y 或 z→x 可行，撤 y→z 不可行
        reset_all()
        import_payload({
            "sources": [{"id": "s1", "uncertainty": 2},
                        {"id": "s2", "uncertainty": 0.5},
                        {"id": "s3", "uncertainty": 1}],
            "events": [
                {"id": "x", "source": "s1", "time": 102},    # [100, 104]
                {"id": "y", "source": "s2", "time": 101.5},  # [101, 102]
                {"id": "z", "source": "s3", "time": 104},    # [103, 105]
            ],
            "constraints": [{"from": "x", "to": "y"},
                            {"from": "y", "to": "z"},
                            {"from": "z", "to": "x"}],
        })
        r = revocation.suggest_revocations()
        self.assertTrue(r["complete"])
        self.assertEqual(r["min_removals"], 1)
        removed = {tuple(s["constraint_ids"]) for s in r["solutions"]}
        self.assertEqual(removed, {(constraint_id("x", "y"),),
                                   (constraint_id("z", "x"),)})
        self.assertNotIn((constraint_id("y", "z"),), removed)
        for s in r["solutions"]:
            self.assertEqual(s["witness"]["event_count"], 3)

    def test_parallel_edges_need_both_removed(self):
        # 同一对端点上的两条人工约束诱导同一不等式：只撤一条等于没撤
        reset_all()
        import_payload({
            "sources": [{"id": "s1", "uncertainty": 1},
                        {"id": "s2", "uncertainty": 1}],
            "events": [
                {"id": "a", "source": "s1", "time": 105},   # [104, 106]
                {"id": "b", "source": "s2", "time": 100},   # [99, 101]
            ],
            "constraints": [{"from": "a", "to": "b", "kind": "causal"},
                            {"from": "a", "to": "b", "kind": "claimed"}],
        })
        r = revocation.suggest_revocations()
        self.assertTrue(r["complete"])
        self.assertEqual(r["min_removals"], 2)  # 必须两条一起撤
        self.assertEqual(len(r["solutions"]), 1)
        self.assertEqual(sorted(r["solutions"][0]["constraint_ids"]),
                         sorted([constraint_id("a", "b", "causal"),
                                 constraint_id("a", "b", "claimed")]))

    def test_two_independent_contradictions_min_two(self):
        reset_all()
        payload = {"sources": [{"id": f"s{i}", "uncertainty": 1}
                               for i in range(4)],
                   "events": [], "constraints": []}
        for i in (0, 1):
            payload["events"] += [
                {"id": f"a{i}", "source": f"s{2*i}", "time": 100},   # [99,101]
                {"id": f"b{i}", "source": f"s{2*i+1}", "time": 105},  # [104,106]
            ]
            payload["constraints"].append({"from": f"b{i}", "to": f"a{i}"})
        import_payload(payload)
        r = revocation.suggest_revocations()
        self.assertTrue(r["complete"])
        self.assertEqual(r["min_removals"], 2)
        self.assertEqual(len(r["solutions"]), 1)
        self.assertEqual(sorted(r["solutions"][0]["constraint_ids"]),
                         sorted([constraint_id("b0", "a0"),
                                 constraint_id("b1", "a1")]))

    def test_four_independent_contradictions_exceed_limit(self):
        # 需要撤销 4 条：超过 MAX_REMOVALS=3，明确报告而非误称最优
        reset_all()
        payload = {"sources": [{"id": f"s{i}", "uncertainty": 1}
                               for i in range(8)],
                   "events": [], "constraints": []}
        for i in range(4):
            payload["events"] += [
                {"id": f"a{i}", "source": f"s{2*i}", "time": 100},
                {"id": f"b{i}", "source": f"s{2*i+1}", "time": 105},
            ]
            payload["constraints"].append({"from": f"b{i}", "to": f"a{i}"})
        import_payload(payload)
        r = revocation.suggest_revocations()
        self.assertTrue(r["complete"])               # 1~3 条已穷尽，结论确定
        self.assertTrue(r["fixable_by_revocation"])  # 全撤可行
        self.assertEqual(r["solutions"], [])
        self.assertIsNone(r["min_removals"])
        self.assertIn("4", r["summary"])
        self.assertIn("超出搜索上限", r["summary"])

    def test_source_order_not_revocable(self):
        # seq 强制同来源逆序：矛盾完全由 source_order 边构成，无可撤销约束
        reset_all()
        import_payload({
            "sources": [{"id": "s1", "uncertainty": 0}],
            "events": [
                {"id": "A", "source": "s1", "time": 100, "seq": 1},
                {"id": "B", "source": "s1", "time": 99, "seq": 2},
            ],
        })
        r = revocation.suggest_revocations()
        self.assertFalse(r["base_feasible"])
        self.assertTrue(r["needed"])
        self.assertFalse(r["fixable_by_revocation"])
        self.assertEqual(r["solutions"], [])
        self.assertEqual(r["stats"]["revocable_constraints"], 0)
        self.assertIn("没有仅靠撤销约束的修复方案", r["summary"])
        self.assertIn("不可撤销", r["summary"])

    def test_max_solutions_cap(self):
        # 六环：5 条边撤任一都可恢复（第 6 条不行），默认上限恰好展示 5 个
        reset_all()
        events = [{"id": "e0", "source": "s0", "time": 2500}]
        events += [{"id": f"e{i}", "source": f"s{i}", "time": 2499}
                   for i in range(1, 5)]
        events.append({"id": "e5", "source": "s5", "time": 2498})
        import_payload({
            "sources": [{"id": f"s{i}", "uncertainty": 0.6} for i in range(6)],
            "events": events,
            "constraints": ([{"from": f"e{i}", "to": f"e{i+1}"} for i in range(5)]
                            + [{"from": "e5", "to": "e0"}]),
        })
        r = revocation.suggest_revocations()
        self.assertTrue(r["complete"])
        self.assertEqual(r["min_removals"], 1)
        self.assertEqual(len(r["solutions"]), 5)  # 至多 5 个同样最少的方案
        removed_pairs = {(s["constraints"][0]["from"], s["constraints"][0]["to"])
                         for s in r["solutions"]}
        self.assertNotIn(("e5", "e0"), removed_pairs)  # 撤它仍然无解
        for s in r["solutions"]:
            self.assertEqual(s["witness"]["event_count"], 6)

    def test_minimality_cross_check_brute_force(self):
        # 混合场景：与暴力枚举交叉验证"最少"与每个方案的有效性
        reset_all()
        import_payload({
            "sources": [{"id": "s1", "uncertainty": 1},
                        {"id": "s2", "uncertainty": 0.5},
                        {"id": "s3", "uncertainty": 2}],
            "events": [
                {"id": "a", "source": "s1", "time": 100},    # [99, 101]
                {"id": "b", "source": "s2", "time": 104.5},  # [104, 105]
                {"id": "c", "source": "s3", "time": 103},    # [101, 105]
                {"id": "d", "source": "s3", "time": 106},    # [104, 108]
            ],
            "constraints": [{"from": "b", "to": "a"},              # 单独不可能
                            {"from": "a", "to": "c"},
                            {"from": "c", "to": "d"},
                            {"from": "d", "to": "a", "kind": "claimed"}],
        })
        r = revocation.suggest_revocations()
        self.assertTrue(r["complete"])
        with db() as conn:
            rev_ids = [row["id"] for row in conn.execute(
                "SELECT id FROM constraints WHERE kind != 'source_order'")]
        # 暴力：最小可行撤销条数
        brute_min = None
        for k in range(0, 4):
            if any(check_feasibility(exclude=set(combo))["feasible"]
                   for combo in itertools.combinations(rev_ids, k)):
                brute_min = k
                break
        self.assertEqual(r["min_removals"], brute_min)
        # 每个方案独立复核：撤销后必须真的可行
        for s in r["solutions"]:
            self.assertEqual(len(s["constraint_ids"]), brute_min)
            self.assertTrue(
                check_feasibility(exclude=set(s["constraint_ids"]))["feasible"])
        # 未截断时方案集合与暴力枚举完全一致
        if not r["solutions_truncated"]:
            want = {tuple(sorted(combo))
                    for combo in itertools.combinations(rev_ids, brute_min)
                    if check_feasibility(exclude=set(combo))["feasible"]}
            got = {tuple(s["constraint_ids"]) for s in r["solutions"]}
            self.assertEqual(got, want)


class LimitBehavior(unittest.TestCase):
    """触限：明确标为未完成，不声称最优。"""

    def setUp(self):
        reset_all()
        # 四环：撤 e0→e1 / e1→e2 / e2→e3 任一可恢复，撤 e3→e0 不可
        import_payload({
            "sources": [{"id": f"s{i}", "uncertainty": 0.6} for i in range(4)],
            "events": [
                {"id": "e0", "source": "s0", "time": 2500},
                {"id": "e1", "source": "s1", "time": 2499},
                {"id": "e2", "source": "s2", "time": 2499},
                {"id": "e3", "source": "s3", "time": 2498},
            ],
            "constraints": [{"from": "e0", "to": "e1"},
                            {"from": "e1", "to": "e2"},
                            {"from": "e2", "to": "e3"},
                            {"from": "e3", "to": "e0"}],
        })
        self._max_solves = revocation.MAX_SOLVES
        self._max_solutions = revocation.MAX_SOLUTIONS

    def tearDown(self):
        revocation.MAX_SOLVES = self._max_solves
        revocation.MAX_SOLUTIONS = self._max_solutions

    def test_solve_budget_incomplete_with_solution(self):
        revocation.MAX_SOLVES = 3  # 基线 + 全撤 + 1 次候选复核
        r = revocation.suggest_revocations()
        self.assertFalse(r["complete"])          # 触限：明确标为未完成
        self.assertEqual(r["min_removals"], 1)   # 最少条数已证明（0 条即基线无解）
        self.assertEqual(len(r["solutions"]), 1)
        self.assertTrue(r["solutions_truncated"])
        self.assertIn("触限", r["summary"])
        self.assertIn("不完整", r["summary"])

    def test_solve_budget_incomplete_without_solution(self):
        revocation.MAX_SOLVES = 2  # 只够基线 + 全撤，候选一个都复核不了
        r = revocation.suggest_revocations()
        self.assertFalse(r["complete"])
        self.assertEqual(r["solutions"], [])
        self.assertIsNone(r["min_removals"])
        self.assertIn("未完成", r["summary"])
        self.assertIn("不保证最优", r["summary"])
        self.assertNotIn("最少撤销", r["summary"])  # 触限时不得声称最优

    def test_max_solutions_truncation(self):
        revocation.MAX_SOLUTIONS = 2
        r = revocation.suggest_revocations()
        self.assertTrue(r["complete"])           # 展示上限不是搜索触限
        self.assertEqual(r["min_removals"], 1)
        self.assertEqual(len(r["solutions"]), 2)
        self.assertTrue(r["solutions_truncated"])  # 可能还有更多同样最少的方案


if __name__ == "__main__":
    unittest.main(verbosity=2)
