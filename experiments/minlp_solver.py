"""MINLP Solver (Joint Optimization Oracle)
============================================
Ported from minlp/run.py + minlp/model.py.

Solves the exact MILP joint deployment + frequency-allocation formulation with
HiGHS via Pyomo, instead of the GA heuristic. Intended as a near-optimal
reference / upper-bound baseline for comparison against the GA/EMS/DL3/LSR
solver modes already wired into trigger_solver().

Porting notes:
- minlp/model.py's build_model(data) is copied verbatim — it only depends on
  the generic `data` dict, not on anything specific to the original CLI tool.
- `data["combos"][node][combo_id]` is rebuilt from GAEnv.all_caps[node_idx],
  using the legal mask (int) as the combo_id so the optimal z can be mapped
  straight back to a deployment mask (gene) without an extra lookup table.
- Unlike minlp/run.py, there is no warm-start: pyomo's HiGHS (appsi) backend
  does not support `solve(..., warmstart=True)`, so set_simple_warm_start()
  in the original script never actually reached the solver — porting it here
  would be dead code.
"""

import logging
from typing import Dict, List, Tuple

from pyomo.environ import (
    ConcreteModel, Set, Param, Var, Constraint, Objective,
    Binary, NonNegativeReals, maximize, Expression,
    SolverFactory, value,
)
from pyomo.opt import TerminationCondition, SolverStatus

logger = logging.getLogger("MINLP")

_FEASIBLE_TERMS = {
    TerminationCondition.optimal,
    TerminationCondition.maxTimeLimit,
    TerminationCondition.maxIterations,
}
if hasattr(TerminationCondition, "feasible"):
    _FEASIBLE_TERMS.add(TerminationCondition.feasible)
if hasattr(TerminationCondition, "mipGap"):
    _FEASIBLE_TERMS.add(TerminationCondition.mipGap)


def build_model(data: dict) -> ConcreteModel:
    """
    建立 Pyomo MILP 模型（聯合服務部署 z 與頻率分配 x/s/y/Y）。

    data = {
        "M": [...],                # 節點集合
        "Q": [...],                # 服務集合
        "G": [...],                # Agent 集合
        "R": {agent: [services]},  # 每個 Agent 所需服務集合 R(g_i)
        "combos": {node: {combo_id: {"services": [...], "W": {svc: cap}}}},
        "f_l": {svc: freq}, "f_h": {svc: freq},
    }
    """
    model = ConcreteModel()

    M_list = list(data["M"])
    Q_list = list(data["Q"])
    G_list = list(data["G"])
    combos_data = data["combos"]

    model.M = Set(initialize=M_list, doc="節點集合 M")
    model.Q = Set(initialize=Q_list, doc="服務集合 Q")
    model.G = Set(initialize=G_list, doc="Agent 集合 G")

    def R_init(model, i):
        return data["R"].get(i, [])
    model.R = Set(model.G, within=model.Q, initialize=R_init, doc="每個 Agent 所需服務集合 R(g_i)")

    def Z_INDEX_init(model):
        for n, combo_dict in combos_data.items():
            for c in combo_dict.keys():
                yield (n, c)
    model.Z_INDEX = Set(dimen=2, initialize=Z_INDEX_init, doc="(節點,部署組合) 索引集合")

    model.f_l = Param(model.Q, initialize=data["f_l"], within=NonNegativeReals, doc="最低服務頻率門檻 f_k^l")
    model.f_h = Param(model.Q, initialize=data["f_h"], within=NonNegativeReals, doc="標準服務頻率上限 f_k^h")

    W_init = {}
    HasService_init = {}
    for n, combo_dict in combos_data.items():
        for c, c_info in combo_dict.items():
            services = set(c_info["services"])
            cap_dict = c_info["W"]
            for k in Q_list:
                cap = cap_dict.get(k, 0.0)
                W_init[((n, c), k)] = cap
                HasService_init[((n, c), k)] = 1 if k in services else 0

    model.W = Param(
        model.Z_INDEX, model.Q, initialize=W_init, default=0.0, within=NonNegativeReals,
        doc="節點 n 在部署組合 c 下，對服務 k 可提供的最大吞吐量 W(n,c,k)"
    )
    model.HasService = Param(
        model.Z_INDEX, model.Q, initialize=HasService_init, default=0, within=NonNegativeReals,
        doc="指示服務 k 是否包含在部署組合 (n,c) 中的 0/1 參數"
    )

    model.z = Var(model.Z_INDEX, domain=Binary, doc="若節點 n 選擇部署組合 c 則 z_{n,c}=1")
    model.x = Var(model.G, model.M, model.Q, domain=Binary, doc="若 Agent i 連到節點 n 的服務 k 則 x_{i,n,k}=1")
    model.s = Var(model.G, model.M, model.Q, domain=NonNegativeReals, doc="Agent i 在節點 n 上獲得服務 k 的頻率")
    model.y = Var(model.G, model.Q, domain=Binary, doc="若 Agent i 在服務 k 上達到最低頻率門檻則 y_{i,k}=1")
    model.Y = Var(model.G, domain=Binary, doc="若 Agent i 所需的所有服務皆達到 QoS 則 Y_i=1")

    def d_expr_rule(model, n, k):
        return sum(
            model.z[n, c]
            for (nn, c) in model.Z_INDEX
            if nn == n and model.HasService[(n, c), k] >= 0.5
        )
    model.d = Expression(model.M, model.Q, rule=d_expr_rule)

    def f_expr_rule(model, i, k):
        return sum(model.s[i, n, k] for n in model.M)
    model.f = Expression(model.G, model.Q, rule=f_expr_rule)

    def node_choose_one_combo_rule(model, n):
        combos_n = [c for (nn, c) in model.Z_INDEX if nn == n]
        return sum(model.z[n, c] for c in combos_n) <= 1
    model.NodeChooseOneCombo = Constraint(model.M, rule=node_choose_one_combo_rule)

    def x_leq_d_rule(model, i, n, k):
        return model.x[i, n, k] <= model.d[n, k]
    model.Link_x_d = Constraint(model.G, model.M, model.Q, rule=x_leq_d_rule)

    def single_node_per_service_rule(model, i, k):
        if k not in model.R[i]:
            return Constraint.Skip
        return sum(model.x[i, n, k] for n in model.M) == model.y[i, k]
    model.SingleNodePerService = Constraint(model.G, model.Q, rule=single_node_per_service_rule)

    def capacity_rule(model, n, k):
        left = sum(model.s[i, n, k] for i in model.G)
        right = sum(model.W[(n, c), k] * model.z[n, c] for (nn, c) in model.Z_INDEX if nn == n)
        return left <= right
    model.Capacity = Constraint(model.M, model.Q, rule=capacity_rule)

    def s_leq_fx_rule(model, i, n, k):
        return model.s[i, n, k] <= model.f_h[k] * model.x[i, n, k]
    model.SxLink = Constraint(model.G, model.M, model.Q, rule=s_leq_fx_rule)

    def qos_lower_rule(model, i, k):
        if k not in model.R[i]:
            return Constraint.Skip
        return model.f[i, k] >= model.f_l[k] * model.y[i, k]

    def qos_upper_rule(model, i, k):
        if k not in model.R[i]:
            return Constraint.Skip
        return model.f[i, k] <= model.f_h[k] * model.y[i, k]

    model.QoSLower = Constraint(model.G, model.Q, rule=qos_lower_rule)
    model.QoSUpper = Constraint(model.G, model.Q, rule=qos_upper_rule)

    def user_qos_upper_rule(model, i, k):
        if k not in model.R[i]:
            return Constraint.Skip
        return model.Y[i] <= model.y[i, k]
    model.UserQoSUpper = Constraint(model.G, model.Q, rule=user_qos_upper_rule)

    def user_qos_lower_rule(model, i):
        R_i = list(model.R[i])
        if len(R_i) == 0:
            return model.Y[i] == 0
        return model.Y[i] >= sum(model.y[i, k] for k in R_i) - (len(R_i) - 1)
    model.UserQoSLower = Constraint(model.G, rule=user_qos_lower_rule)

    def objective_rule(model):
        return sum(model.Y[i] for i in model.G)
    model.Obj = Objective(rule=objective_rule, sense=maximize)

    return model


def configure_solver(time_limit: float = 30, rel_gap: float = 0.1, max_nodes: int = 10000):
    solver = SolverFactory("highs")
    solver.options['time_limit'] = time_limit
    solver.options['mip_rel_gap'] = rel_gap
    solver.options['mip_max_nodes'] = max_nodes
    solver.options['presolve'] = 'on'
    solver.options['parallel'] = 'on'
    solver.options['simplex_strategy'] = 1
    return solver


def _env_to_data(env) -> dict:
    """將 GAEnv 轉換成 build_model() 所需的 data 結構，combo_id 直接使用 mask 字串。"""
    services = env.services
    data = {
        "M": list(env.nodes),
        "Q": list(services),
        "G": list(env.agents),
        "R": {a: list(env.req[a]) for a in env.agents},
        "f_l": {s: env.fl_list[si] for si, s in enumerate(services)},
        "f_h": {s: env.fh_list[si] for si, s in enumerate(services)},
    }

    combos: Dict[str, Dict[str, dict]] = {}
    for ni, node in enumerate(env.nodes):
        combos[node] = {}
        for mask, caps in env.all_caps[ni].items():
            svcs_in_mask = [s for si, s in enumerate(services) if mask & (1 << si)]
            W = {s: caps[si] for si, s in enumerate(services) if mask & (1 << si)}
            combos[node][str(mask)] = {"services": svcs_in_mask, "W": W}
    data["combos"] = combos
    return data


def minlp_solve(
    env,
    time_limit: float = 30,
    rel_gap: float = 0.1,
    max_nodes: int = 10000,
) -> Tuple[List[int], Dict[Tuple[str, str, str], float]]:
    """
    求解 MILP，回傳 (gene, optimal_x)，格式與 lsr_solve / GA allocator 一致：
      gene      — List[int]，gene[ni] 為節點 ni 選擇的部署遮罩（與 env.all_caps[ni] 的 key 一致）
      optimal_x — Dict[(agent_id, node_name, service_type), freq]
    無可行解時，gene 退回每個節點的最小合法遮罩，optimal_x 為空字典。
    """
    data = _env_to_data(env)
    model = build_model(data)
    solver = configure_solver(time_limit=time_limit, rel_gap=rel_gap, max_nodes=max_nodes)
    results = solver.solve(model, tee=False)

    term = results.solver.termination_condition
    status = results.solver.status
    has_feasible = status == SolverStatus.ok and term in _FEASIBLE_TERMS

    if not has_feasible:
        logger.warning(f"[MINLP] No feasible solution (status={status}, termination={term}), "
                        f"falling back to minimal deployment with no routing.")
        gene = [min(env.all_caps[ni].keys()) for ni in range(len(env.nodes))]
        return gene, {}

    gene: List[int] = []
    for ni, node in enumerate(env.nodes):
        chosen_mask = None
        for (nn, c) in model.Z_INDEX:
            if nn == node and value(model.z[node, c]) > 0.5:
                chosen_mask = int(c)
                break
        if chosen_mask is None or chosen_mask not in env.all_caps[ni]:
            chosen_mask = min(env.all_caps[ni].keys())
        gene.append(chosen_mask)

    optimal_x: Dict[Tuple[str, str, str], float] = {}
    for i in model.G:
        for n in model.M:
            for k in model.Q:
                if value(model.x[i, n, k]) > 0.5:
                    freq = value(model.s[i, n, k])
                    if freq and freq > 0:
                        optimal_x[(i, n, k)] = freq

    logger.info(f"[MINLP] termination={term}, objective={value(model.Obj)}, "
                f"gene={gene}, assignments={len(optimal_x)}")
    return gene, optimal_x
