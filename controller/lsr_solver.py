"""LSR Solver (Baseline 2)
=========================
Two-stage heuristic porting of:
  Tambe & Franklin (IEEE 2026) — "Balancing the Edge: Multi-Service Request
  Allocation in Urban 5G-MEC Environment"

Stage 1: Greedy deployment + assignment (coverage maximization, hardest-first)
Stage 2: Iterative load rebalancing (variance minimization, routing only)

Porting notes:
- Server e       → Node i  (env.nodes)
- Service s      → Service j  (env.services)
- User request u → (agent k, service j) pair
- A_e (deploy capacity) → C_i (legal combo masks per node)
- C_e (compute capacity) → W(i,c,j) via env.all_caps[ni][mask][si]
- LD_u (fixed per-request demand) → f_l: an (agent, service) request is either
  granted its full f_l or not served at all (no partial/residual amount),
  mirroring the original paper's binary y_{e,u} over a fixed-size demand —
  Stage 1 and Stage 2 both enforce this consistently. f_l (not f_h) is used
  because it is the threshold this thesis's own Problem P treats as "served"
  (A_sat's first-order objective is defined at >=f_l; f_h/Q is a secondary
  refinement everywhere in this thesis, including EMS's floor semantics) —
  mapping LD_u to f_h would make LSR's own bar for "served" stricter than
  what "served" means for every other method being compared.
- No QoS-threshold awareness in allocation (preserves LSR design spirit)
"""

import logging
from typing import Dict, List, Tuple

logger = logging.getLogger("LSR")


def _variance(vals: List[float]) -> float:
    if len(vals) < 2:
        return 0.0
    mean = sum(vals) / len(vals)
    return sum((v - mean) ** 2 for v in vals) / len(vals)


def lsr_solve(env) -> Tuple[List[int], Dict[Tuple[str, str, str], float]]:
    """
    LSR two-stage heuristic.

    Returns:
        gene  — List[int], gene[ni] = deployment mask for node ni
        x     — Dict[(agent_id, node_name, service_type), freq]
    """
    n_nodes = len(env.nodes)
    n_services = len(env.services)

    # ── Initial deployment: empty (no services pre-deployed on any node) ──────
    # 注意：不可用 min(all_caps[ni].keys()) 當起始 mask——那個值純粹取決於
    # services 在 spec JSON 裡的宣告順序（bit 0 的服務會被「免費」預先部署到
    # 每個節點），跟 LSR 演算法設計無關，會讓某個服務天生佔便宜。
    current_masks: List[int] = [0] * n_nodes

    # remaining[ni][si] = remaining FPS capacity for service si on node ni
    remaining: List[List[float]] = [[0.0] * n_services for _ in range(n_nodes)]

    x: Dict[Tuple[str, str, str], float] = {}

    def _used(ni: int, si: int) -> float:
        """目前已分配給該節點該服務的總量（不受容量上限影響，純加總 x）。"""
        node_name = env.nodes[ni]
        svc = env.services[si]
        return sum(x.get((ag, node_name, svc), 0.0) for ag in env.agents)

    def _refresh_remaining(ni: int) -> None:
        """Recompute remaining[ni] from current mask and existing x entries."""
        mask = current_masks[ni]
        caps = env.all_caps[ni].get(mask, [0.0] * n_services)
        for si in range(n_services):
            remaining[ni][si] = max(0.0, caps[si] - _used(ni, si))

    # Initial remaining (no assignments yet)
    for ni in range(n_nodes):
        caps = env.all_caps[ni].get(current_masks[ni], [0.0] * n_services)
        for si in range(n_services):
            remaining[ni][si] = caps[si]

    # ── Build request list ─────────────────────────────────────────────────────
    # node_can_deploy[si] = set of node indices that have at least one mask with svc si
    node_can_deploy: Dict[int, set] = {}
    for si in range(n_services):
        deployable = set()
        for ni in range(n_nodes):
            for mask in env.all_caps[ni]:
                if mask & (1 << si):
                    deployable.add(ni)
                    break
        node_can_deploy[si] = deployable

    requests: List[Tuple[str, int, str]] = []
    for ag in env.agents:
        for svc in env.req[ag]:
            si = env.svc2idx[svc]
            requests.append((ag, si, svc))

    # 難度依據：node_can_deploy 在同質叢集下所有服務都平手（到處都能部署），
    # 完全沒有區分力，會讓排序退化成純到達順序。改以「單獨部署時的吞吐量」
    # 當難度指標——吞吐量低的服務視為較難服務，優先處理，讓它有機會搶到
    # 全新節點（fresh node）的最佳容量，而非被晚處理、被迫升級組合到已被
    # 佔用、容量較低的節點。
    def _standalone_throughput(si: int) -> float:
        vals = [
            env.all_caps[ni][1 << si][si]
            for ni in range(n_nodes)
            if (1 << si) in env.all_caps[ni]
        ]
        return sum(vals) / len(vals) if vals else 0.0

    throughput_metric = {si: _standalone_throughput(si) for si in range(n_services)}
    requests.sort(key=lambda r: throughput_metric[r[1]])

    # ── Stage 1: Greedy assignment ─────────────────────────────────────────────
    for ag, si, svc in requests:
        # LD_u固定量對應f_l，不是f_h：本研究方法Problem P的第一層目標A_sat
        # 本身即以>=f_l為「達標」判準（f_h/Q僅為次要精煉項），EMS的保底承諾
        # 也是f_l——若LSR的固定量設為f_h，等於讓LSR「算不算服務到」的門檻
        # 比另外兩個方法共同的「達標」定義還嚴格，並非單純的忠誠度選擇。
        ld = env.fl_list[si]

        # Candidates: nodes where svc is already deployed AND has room for a
        # FULL LD_u（嚴格二元下，殘餘容量不夠一整份LD_u的節點不算候選，否則
        # 會卡住候選判斷、錯過其他還能給滿LD_u的全新節點）
        candidates = [
            ni for ni in range(n_nodes)
            if (current_masks[ni] & (1 << si)) and remaining[ni][si] >= ld
        ]

        if not candidates:
            # Upgrade a node's deployment combo to include svc.
            # Prefer smallest superset of current mask; fall back to any mask with svc.
            best_ni, best_new_mask, best_cap = None, None, -1.0
            best_load = float("inf")

            for ni in range(n_nodes):
                cur_mask = current_masks[ni]
                # Superset masks (keep all existing services + add svc)
                candidates_masks = [
                    m for m in env.all_caps[ni]
                    if (m & (1 << si)) and (m & cur_mask) == cur_mask
                ]
                if not candidates_masks:
                    # No superset — accept any mask that includes svc
                    candidates_masks = [m for m in env.all_caps[ni] if m & (1 << si)]

                # 過濾掉會讓既有已分配服務超額的候選 mask：升級後，該節點原本
                # 已部署的每個服務，新容量都必須仍能涵蓋目前已分配出去的總量
                # （env.all_caps[ni][m][sj] >= _used(ni, sj)），否則既有使用者
                # 的分配會變成帳面上超出真實容量的虛值——這是先前發現的
                # over-commit bug，修正後寧可放棄這個升級選項，也不讓既有分配
                # 失真。
                candidates_masks = [
                    m for m in candidates_masks
                    if all(env.all_caps[ni][m][sj] + 1e-9 >= _used(ni, sj)
                           for sj in range(n_services) if cur_mask & (1 << sj))
                ]

                # 已部署服務數（負載指標）：同質叢集下容量平手是常態，若只比較
                # capacity，tie-break 永遠選遍歷順序中第一個節點，導致某個服務
                # 被鎖死在單一節點上。平手時改偏好負載較輕（已部署服務數較少）
                # 的節點，讓不同服務有機會分散到不同節點。
                load = bin(cur_mask).count("1")
                for m in candidates_masks:
                    cap = env.all_caps[ni][m][si]
                    if cap > best_cap or (cap == best_cap and load < best_load):
                        best_cap, best_ni, best_new_mask, best_load = cap, ni, m, load

            if best_ni is None or best_cap <= 0:
                logger.debug("[LSR][S1] Cannot serve (%s, %s): skipped", ag, svc)
                continue

            current_masks[best_ni] = best_new_mask
            _refresh_remaining(best_ni)
            candidates = [best_ni] if remaining[best_ni][si] >= ld else []

        if not candidates:
            continue

        # Pick node with most remaining capacity（candidates已保證remaining>=LD_u，
        # 嚴格二元：對應原論文LD_u為固定需求量、y_{e,u}為二元變數的精神——
        # 裝得下完整的LD_u才分配，不會留下低於LD_u的殘值）
        best_ni = max(candidates, key=lambda ni: remaining[ni][si])
        freq = ld

        node_name = env.nodes[best_ni]
        x[(ag, node_name, svc)] = freq
        remaining[best_ni][si] -= freq

    logger.info("[LSR][S1] Done. assignments=%d, gene=%s", len(x), current_masks)

    # ── Stage 2: Iterative load variance minimization (routing only) ──────────
    MAX_ITERS = 50
    EPS = 0.01

    def _compute_util() -> List[float]:
        util = []
        for ni in range(n_nodes):
            # mask=0（節點從未被用到）不是 workAbility 裡宣告的合法 combo，
            # all_caps[ni] 裡沒有這個 key，要用 .get 給 0 capacity 的預設值
            total_cap = sum(env.all_caps[ni].get(current_masks[ni], [0.0] * n_services))
            node_name = env.nodes[ni]
            total_used = sum(
                x.get((ag, node_name, svc), 0.0)
                for ag in env.agents
                for svc in env.req[ag]
            )
            util.append(total_used / total_cap if total_cap > 0 else 0.0)
        return util

    for iteration in range(MAX_ITERS):
        util = _compute_util()
        if max(util) - min(util) < EPS:
            logger.debug("[LSR][S2] Converged at iter %d", iteration)
            break

        max_ni = max(range(n_nodes), key=lambda i: util[i])
        min_ni = min(range(n_nodes), key=lambda i: util[i])
        var_before = _variance(util)
        node_max = env.nodes[max_ni]
        node_min = env.nodes[min_ni]

        moved = False
        for ag in env.agents:
            if moved:
                break
            for svc in env.req[ag]:
                si = env.svc2idx[svc]

                if (ag, node_max, svc) not in x:
                    continue
                # Stage 2 only re-routes; no deployment change
                if not (current_masks[min_ni] & (1 << si)):
                    continue

                ld = env.fl_list[si]  # 與Stage 1一致：LD_u固定量對應f_l
                avail = remaining[min_ni][si]
                # 同Stage 1的嚴格二元原則：目標節點要有完整LD_u的空間才搬，
                # 不允許搬過去後拿到比原本更少的量（LD_u為固定需求，Stage 2
                # 只換伺服器、不調整數量）。
                if avail < ld:
                    continue

                # Trial move
                old_freq = x[(ag, node_max, svc)]
                new_freq = ld

                del x[(ag, node_max, svc)]
                remaining[max_ni][si] += old_freq
                x[(ag, node_min, svc)] = new_freq
                remaining[min_ni][si] -= new_freq

                var_after = _variance(_compute_util())
                if var_after < var_before:
                    logger.debug(
                        "[LSR][S2] iter=%d moved (%s,%s) %s→%s var %.4f→%.4f",
                        iteration, ag, svc, node_max, node_min, var_before, var_after,
                    )
                    moved = True
                    break
                else:
                    # Revert
                    del x[(ag, node_min, svc)]
                    remaining[min_ni][si] += new_freq
                    x[(ag, node_max, svc)] = old_freq
                    remaining[max_ni][si] -= old_freq

        if not moved:
            logger.debug("[LSR][S2] No improving move at iter %d, stopping", iteration)
            break

    logger.info("[LSR] Solver done. gene=%s, assignments=%d", current_masks, len(x))
    return current_masks, x
