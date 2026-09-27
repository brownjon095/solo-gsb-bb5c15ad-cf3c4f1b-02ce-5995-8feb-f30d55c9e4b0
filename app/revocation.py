"""约束撤销建议：全量联立无解时，搜索需撤销的最少人工/导入约束。

模型
----
沿用 ``feasibility`` 的差分约束联立求解。当基线检查无解时，本模块回答：
**至少撤销哪几条约束，系统才恢复全局可行？**

- 可撤销：人工声明或导入的约束（``kind != "source_order"``）。
  ``source_order`` 系统时序边与事件时间区间是观测事实，**不可撤销**。
- 搜索沿矛盾路径进行：无解见证（矛盾路径）是系统的一组"冲突证书"——
  路径上每一跳（相邻事件对）只要还剩一条边，证书就依然成立。
  因此任何可行的撤销集，必须包含该路径上某一跳的**全部**剩余约束
  （同一对端点间的平行边诱导完全相同的不等式，只撤一条等于没撤）。
  撤销候选后重新求解会得到新的矛盾路径，据此逐层扩展候选。
- 按撤销条数升序（堆）处理候选节点：第一个被复核为可行的节点，
  其撤销条数就是全局最少（更小的集合此时已全部复核为无解）。
  每个候选都调用 ``feasibility`` 的**原求解器**复核，可行时把完整的
  最早可行时刻见证附在方案里。

限制（防止交互被拖垮）
--------------------
- 最多撤销 ``MAX_REMOVALS`` 条（3）；
- 最多 ``MAX_SOLVES`` 次求解（2000，含基线与全撤检查）；
- 最多展示 ``MAX_SOLUTIONS`` 个同样最少的方案（5）。
触限时 ``complete=False`` 明确标为**未完成**：未找到方案时不得声称
"无解/最优"；已找到方案时最少条数已被证明（更小的集合已全部复核），
但方案列表可能不完整。

若撤销**全部**可撤销约束后仍无解（矛盾完全由 source_order 边与事件区间
构成），说明没有仅靠撤销约束的修复方案，只能调整时钟假设或核对事件归属。

全程只读：预览不写数据库，不改写任何事件、约束或假设。
"""
from __future__ import annotations

import heapq
from typing import Any

from . import engine, feasibility

MAX_REMOVALS = 3    # 最多撤销 3 条约束
MAX_SOLVES = 2000   # 最多 2000 次求解（含基线与全撤检查）
MAX_SOLUTIONS = 5   # 最多展示 5 个同样最少的方案

NON_REVOCABLE_KIND = "source_order"  # 系统时序边不可撤销；事件区间本就不可撤销


def _breakable_hops(solver: feasibility._Solver,
                    path_events: list[str]) -> list[frozenset]:
    """矛盾路径上可打断的"跳"：同一对端点间的全部剩余约束均可撤销。

    返回 [{约束 id, ...}, ...]——要打断一跳必须撤掉该跳上的全部约束；
    含 source_order 边的跳不可打断（系统时序边不可撤销）。
    """
    pair_edges: dict[tuple[str, str], list[dict]] = {}
    for e in solver.edges:
        key = (solver.ids[e["u"]], solver.ids[e["v"]])
        pair_edges.setdefault(key, []).append(e)
    hops: list[frozenset] = []
    seen: set[tuple[str, str]] = set()
    for a, b in zip(path_events, path_events[1:]):
        if (a, b) in seen:
            continue
        seen.add((a, b))
        edges = pair_edges.get((a, b), [])
        if edges and all(e["kind"] != NON_REVOCABLE_KIND for e in edges):
            hop = frozenset(e["id"] for e in edges if e["id"] is not None)
            if hop:
                hops.append(hop)
    return hops


def _solution(node: frozenset, res: dict, edge_by_id: dict) -> dict:
    """组装一个方案：约束 ID + 端点 + 撤销后全量可行见证。"""
    constraints = []
    for cid in sorted(node):
        r = edge_by_id.get(cid) or {}
        constraints.append({
            "id": cid,
            "from": r.get("from_event"),
            "to": r.get("to_event"),
            "kind": r.get("kind", ""),
            "note": r.get("note", ""),
        })
    events = res.get("events", [])
    return {
        "constraint_ids": sorted(node),
        "constraints": constraints,
        # 撤销后全量可行见证：每个事件的最早可行时刻（原求解器复核结果）
        "witness": {"event_count": len(events), "events": events},
    }


def suggest_revocations(overrides: dict | None = None) -> dict[str, Any]:
    """搜索最少约束撤销方案（预览，不写数据库）。

    overrides 为页面当前（含未保存）的时钟假设，与可行性检查完全一致。
    """
    model = engine.load_model()
    edge_by_id = {r.get("id"): r for r in model[2] if r.get("id") is not None}
    limits = {"max_removals": MAX_REMOVALS,
              "max_solves": MAX_SOLVES,
              "max_solutions": MAX_SOLUTIONS}

    solve_count = 0

    def solve(exclude: set | None):
        """用原求解器复核一个候选（exclude 为视为已撤销的约束 id）。"""
        nonlocal solve_count
        solver = feasibility._Solver(overrides, exclude=exclude, model=model)
        res = solver.result()
        solve_count += 1
        return solver, res

    def payload(**kw) -> dict[str, Any]:
        out = {
            "check_type": "constraint-revocation-suggestion",
            "limits": limits,
            "solutions": [],
            "solutions_truncated": False,
            "min_removals": None,
            "complete": True,
        }
        out.update(kw)
        return out

    # ------------------------------------------------------------ 基线
    base_solver, base = solve(None)
    revocable = sorted(e["id"] for e in base_solver.edges
                       if e["kind"] != NON_REVOCABLE_KIND and e["id"] is not None)
    non_revocable = sum(1 for e in base_solver.edges
                        if e["kind"] == NON_REVOCABLE_KIND)
    stats = {
        "solve_count": solve_count,
        "revocable_constraints": len(revocable),
        "non_revocable_constraints": non_revocable,
        "skipped_constraints": base_solver.skipped_edges,
        "candidates_checked": 0,
    }

    if base["feasible"]:
        return payload(
            base_feasible=True, needed=False, fixable_by_revocation=None,
            min_removals=0, stats=stats,
            summary="当前时钟假设下全局时序可行，无需撤销任何约束。",
        )

    # ---------------------------------------- 全撤检查：是否存在撤销型修复
    _, all_removed = solve(set(revocable))
    stats["solve_count"] = solve_count
    fixable = bool(all_removed["feasible"])
    if not fixable:
        reason = ("没有可撤销的人工/导入约束（全部有效约束均为 source_order "
                  "系统时序边）" if not revocable else
                  f"撤销全部 {len(revocable)} 条可撤销的人工/导入约束后仍无解")
        return payload(
            base_feasible=False, needed=True, fixable_by_revocation=False,
            stats=stats,
            summary=(reason + "：source_order 系统时序边与事件时间区间不可撤销，"
                     "没有仅靠撤销约束的修复方案；请调整相关来源的时钟偏差 / "
                     "不确定度假设，或核对事件归属。"),
        )

    # ------------------------------------------------------------ 搜索
    solutions: list[dict] = []
    visited: set[frozenset] = {frozenset()}
    heap: list[tuple[int, int, frozenset]] = []  # (撤销条数, 生成序号, 约束集)
    seq = 0

    def push(node: frozenset) -> None:
        nonlocal seq
        if node in visited or not 1 <= len(node) <= MAX_REMOVALS:
            return
        visited.add(node)
        heapq.heappush(heap, (len(node), seq, node))
        seq += 1

    for hop in _breakable_hops(base_solver, base["witness"]["events"]):
        push(hop)

    complete = True
    min_size: int | None = None
    while heap:
        if len(solutions) >= MAX_SOLUTIONS:
            break
        size, _, node = heapq.heappop(heap)
        if min_size is not None and size > min_size:
            break  # 已有更少方案：更大的集合无需再复核
        if solve_count >= MAX_SOLVES:
            complete = False  # 触限：明确标为未完成，不声称已穷尽
            heapq.heappush(heap, (size, _, node))
            break
        solver, res = solve(set(node))
        stats["candidates_checked"] += 1
        if res["feasible"]:
            if min_size is None:
                min_size = size  # 按条数升序处理，首个可行即全局最少
            solutions.append(_solution(node, res, edge_by_id))
            continue
        if size < MAX_REMOVALS:
            for hop in _breakable_hops(solver, res["witness"]["events"]):
                push(node | hop)

    stats["solve_count"] = solve_count
    stats["candidate_queue_remaining"] = len(heap)
    truncated = bool(solutions) and (
        not complete
        or len(solutions) >= MAX_SOLUTIONS
        and any(size == min_size for size, _, _ in heap))

    # ------------------------------------------------------------ 结论
    if solutions:
        k = min_size
        summary = (f"最少撤销 {k} 条约束即可恢复全局可行"
                   + (f"，以下展示 {len(solutions)} 个同样最少的方案"
                      if len(solutions) > 1 or truncated else "，找到 1 个方案"))
        if not complete:
            summary += (f"；注意：搜索触限（{MAX_SOLVES} 次求解）提前停止，"
                        "方案列表不完整（最少条数已证明：更少的组合已全部复核无解）")
        elif truncated:
            summary += f"；可能还有更多同样最少的方案，仅展示前 {MAX_SOLUTIONS} 个"
        summary += "。每个方案均已用原求解器复核：撤销后全量联立可行，附完整可行见证。"
    elif complete:
        summary = (f"仅靠撤销约束可以修复（撤销全部 {len(revocable)} 条可撤销约束后可行），"
                   f"但撤销 1~{MAX_REMOVALS} 条的组合已穷尽且全部无解："
                   f"修复方案至少需要撤销 {MAX_REMOVALS + 1} 条，超出搜索上限；"
                   "请调整时钟假设，或在页面上人工删除更多约束。")
    else:
        summary = (f"搜索触限（{MAX_SOLVES} 次求解）未完成：尚未找到撤销 "
                   f"1~{MAX_REMOVALS} 条的可行方案，无法确认其是否存在（不保证最优）；"
                   "请缩小数据规模或调整时钟假设后重试。")

    return payload(
        base_feasible=False, needed=True, fixable_by_revocation=True,
        min_removals=min_size, solutions=solutions,
        solutions_truncated=truncated, complete=complete, stats=stats,
        summary=summary,
    )
