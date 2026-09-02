from pyomo.environ import SolverFactory, value, Var
from pyomo.opt import TerminationCondition, SolverStatus
import time
import json
import os

from model import build_model
from params import create_example_data, create_random_data, load_data_from_json
from config import DATA_CONFIG


def load_data_by_config():
    mode = DATA_CONFIG["mode"]
    if mode == "json":
        return load_data_from_json(DATA_CONFIG["json_path"])
    if mode == "random":
        return create_random_data(**DATA_CONFIG["random"])
    # 預設 example
    return create_example_data()


def configure_solver(
    solver_name="highs",
    time_limit=120,
    rel_gap=0.15,
    max_nodes=30000,
    first_feasible_only=False
):
    solver = SolverFactory(solver_name)
    if solver_name.lower() == "highs":
        solver.options['time_limit'] = time_limit
        solver.options['mip_rel_gap'] = rel_gap
        solver.options['mip_max_nodes'] = max_nodes
        solver.options['presolve'] = 'on'
        solver.options['parallel'] = 'off'
        solver.options['simplex_strategy'] = 1
        # HiGHS 1.12.0 不支援 solution_limit，會報錯，因此這邊不設
        # if first_feasible_only:
        #     solver.options['solution_limit'] = 1
    return solver


def quick_feasibility_check(data):
    """快速可行性預檢查"""
    print("\n=== 快速可行性預檢查 ===")

    feasibility_issues = []

    for q in data['Q']:
        # 計算需求
        demand_users = sum(1 for g in data['G'] if q in data['R'][g])
        min_total_demand = demand_users * data['f_l'][q]

        # 計算最大可能供給
        max_total_supply = 0
        for n in data['M']:
            max_capacity_for_q = 0
            for combo_id, combo_info in data['combos'][n].items():
                if q in combo_info['services']:
                    max_capacity_for_q = max(
                        max_capacity_for_q,
                        combo_info['W'].get(q, 0)
                    )
            max_total_supply += max_capacity_for_q

        supply_demand_ratio = max_total_supply / max(min_total_demand, 1)

        print(f"服務 {q}:")
        print(f"  需求使用者: {demand_users}")
        print(f"  最低總需求: {min_total_demand}")
        print(f"  最大總供給: {max_total_supply}")
        print(f"  供需比: {supply_demand_ratio:.2f}")

        if max_total_supply < min_total_demand:
            feasibility_issues.append(
                f"服務 {q} 供給不足: 需求 {min_total_demand} > 供給 {max_total_supply}"
            )

    if feasibility_issues:
        print(f"\n⚠️  檢測到 {len(feasibility_issues)} 個潛在可行性問題:")
        for issue in feasibility_issues:
            print(f"  - {issue}")
        return False
    else:
        print(f"\n✅ 預檢查通過，問題看起來是可行的")
        return True


def analyze_infeasibility(model, solver, results, data):
    """分析模型不可行性的原因"""
    print("\n=== INFEASIBILITY ANALYSIS ===")

    # 基本統計
    print(f"節點數: {len(data['M'])}")
    print(f"服務數: {len(data['Q'])}")
    print(f"使用者數: {len(data['G'])}")

    # 需求分析
    service_demand = {}
    for q in data['Q']:
        service_demand[q] = sum(1 for g in data['G'] if q in data['R'][g])

    print(f"\n各服務需求使用者數量:")
    for q, count in service_demand.items():
        print(f"  {q}: {count} 個使用者需要")

    # 供給分析
    service_supply = {}
    total_capacity = {}
    for q in data['Q']:
        service_supply[q] = 0
        total_capacity[q] = 0

    for n in data['M']:
        for combo_id, combo_info in data['combos'][n].items():
            for q in combo_info['services']:
                service_supply[q] += 1
                total_capacity[q] += combo_info['W'].get(q, 0)

    print(f"\n各服務供給能力:")
    for q in data['Q']:
        print(
            f"  {q}: 可在 {service_supply[q]} 個組合中部署，總容量 {total_capacity[q]}"
        )

    # 需求vs供給分析
    print(f"\n需求供給比較:")
    for q in data['Q']:
        min_total_demand = service_demand[q] * data['f_l'][q]
        print(
            f"  {q}: 最低總需求 {min_total_demand:.1f}, "
            f"最大總供給 {total_capacity[q]:.1f}, "
            f"比例 {min_total_demand / max(total_capacity[q], 1):.2f}"
        )

    # 節點組合分析
    print(f"\n各節點可部署組合數:")
    for n in data['M']:
        print(f"  {n}: {len(data['combos'][n])} 個組合")

    # 使用者需求模式分析
    req_patterns = {}
    for g in data['G']:
        pattern = tuple(sorted(data['R'][g]))
        req_patterns[pattern] = req_patterns.get(pattern, 0) + 1

    print(f"\n使用者需求模式:")
    for pattern, count in req_patterns.items():
        print(f"  {pattern}: {count} 個使用者")


def count_model_components(model):
    """計算模型組件數量"""
    component_counts = {
        "variables": 0,
        "constraints": 0,
        "objectives": 0,
        "sets": 0,
        "parameters": 0,
        "expressions": 0
    }

    from pyomo.environ import Var, Constraint, Objective, Set, Param, Expression

    for component in model.component_objects():
        if isinstance(component, Var):
            if hasattr(component, '__len__'):
                try:
                    component_counts["variables"] += len(component)
                except Exception:
                    component_counts["variables"] += 1
            else:
                component_counts["variables"] += 1
        elif isinstance(component, Constraint):
            if hasattr(component, '__len__'):
                try:
                    component_counts["constraints"] += len(component)
                except Exception:
                    component_counts["constraints"] += 1
            else:
                component_counts["constraints"] += 1
        elif isinstance(component, Objective):
            component_counts["objectives"] += 1
        elif isinstance(component, Set):
            component_counts["sets"] += 1
        elif isinstance(component, Param):
            component_counts["parameters"] += 1
        elif isinstance(component, Expression):
            component_counts["expressions"] += 1

    return component_counts


def save_model_info(model, data, results, filename="model_analysis.json"):
    """儲存模型資訊和結果"""

    # 計算模型組件統計
    component_stats = count_model_components(model)

    info = {
        "problem_size": {
            "nodes": len(data['M']),
            "services": len(data['Q']),
            "agents": len(data['G']),
            "total_combos": sum(len(combos) for combos in data['combos'].values())
        },
        "model_statistics": component_stats,
        "solver_results": {
            "termination_condition": str(results.solver.termination_condition),
            "status": str(results.solver.status),
            "time": getattr(results.solver, "time", None)
        }
    }

    # 如果有最優解，添加解的資訊
    if results.solver.termination_condition == TerminationCondition.optimal:
        try:
            info["solution"] = {
                "objective_value": value(model.Obj),
                "satisfied_agents": sum(value(model.Y[i]) for i in model.G),
            }
        except Exception:
            info["solution"] = {"note": "無法提取解的詳細資訊"}

    # 需求供給分析
    service_analysis = analyze_service_balance(data)
    info["service_analysis"] = service_analysis

    # 儲存原始資料（簡化版，避免檔案過大）
    info["input_data_summary"] = {
        "nodes": data["M"],
        "services": data["Q"],
        "agents_count": len(data["G"]),
        "f_low": data["f_l"],
        "f_high": data["f_h"],
        "combos_per_node": {n: len(combos) for n, combos in data["combos"].items()}
    }

    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(info, f, ensure_ascii=False, indent=2, default=str)

    return filename


def analyze_service_balance(data):
    """分析服務供需平衡"""
    analysis = {}

    # 需求分析
    service_demand = {}
    for q in data['Q']:
        service_demand[q] = sum(1 for g in data['G'] if q in data['R'][g])

    # 供給分析
    service_supply = {}
    total_capacity = {}
    for q in data['Q']:
        service_supply[q] = 0
        total_capacity[q] = 0

    for n in data['M']:
        for combo_id, combo_info in data['combos'][n].items():
            for q in combo_info['services']:
                service_supply[q] += 1
                total_capacity[q] += combo_info['W'].get(q, 0)

    # 計算供需比
    for q in data['Q']:
        min_total_demand = service_demand[q] * data['f_l'][q]
        max_total_supply = total_capacity[q]

        analysis[q] = {
            "demand_users": service_demand[q],
            "min_total_demand": min_total_demand,
            "max_total_supply": max_total_supply,
            "supply_demand_ratio": max_total_supply / max(min_total_demand, 1),
            "deployment_options": service_supply[q],
            "is_potentially_feasible": max_total_supply >= min_total_demand
        }

    return analysis


def create_relaxed_model(data):
    """建立放鬆版本的模型用於可行性分析"""
    from pyomo.environ import Constraint

    print("\n=== 嘗試約束放鬆分析 ===")

    # 建立放鬆模型：移除Agent相關約束，只保留部署約束
    relaxed_model = build_model(data)

    try:
        # 移除所有 Agent 相關約束，只保留部署約束
        constraints_to_remove = [
            'SingleNodePerService', 'Link_x_d', 'Capacity',
            'SxLink', 'QoSLower', 'QoSUpper',
            'UserQoSUpper', 'UserQoSLower'
        ]

        for constraint_name in constraints_to_remove:
            if hasattr(relaxed_model, constraint_name):
                relaxed_model.del_component(constraint_name)
                print(f"  移除約束: {constraint_name}")

    except Exception as e:
        print(f"  放鬆模型時發生錯誤: {e}")

    return relaxed_model


def detailed_solution_analysis(model, data):
    """詳細分析求解結果"""
    print("\n=== DETAILED SOLUTION ANALYSIS ===")

    try:
        # 基本統計
        total_agents = len(model.G)
        satisfied_agents = sum(
            1 for i in model.G if value(model.Y[i]) > 0.5
        )
        satisfaction_rate = (
            satisfied_agents / total_agents if total_agents > 0 else 0
        )

        print(f"滿足率: {satisfied_agents}/{total_agents} ({satisfaction_rate:.1%})")

        # 服務部署分析
        print("\n服務部署情況:")
        deployed_services = {}
        for n in model.M:
            deployed_services[n] = []
            for (nn, c) in model.Z_INDEX:
                if nn == n and value(model.z[n, c]) > 0.5:
                    combo_info = data['combos'][n][c]
                    deployed_services[n] = combo_info['services']
                    break

        for n, services in deployed_services.items():
            if services:
                print(f"  {n}: {services}")
            else:
                print(f"  {n}: 未部署任何服務")

        # 容量利用分析
        print("\n容量利用情況:")
        for n in model.M:
            for k in model.Q:
                total_demand = sum(
                    value(model.s[i, n, k]) for i in model.G
                )

                # 計算可用容量
                available_capacity = 0
                for (nn, c) in model.Z_INDEX:
                    if nn == n and value(model.z[n, c]) > 0.5:
                        available_capacity = data['combos'][n][c]['W'].get(k, 0)
                        break

                if available_capacity > 0:
                    utilization = total_demand / available_capacity
                    print(
                        f"  {n}-{k}: 需求 {total_demand:.1f}/"
                        f"{available_capacity:.1f} ({utilization:.1%})"
                    )

        # 未滿足的使用者分析
        unsatisfied_users = []
        for i in model.G:
            if value(model.Y[i]) < 0.5:
                unsatisfied_services = []
                for k in model.R[i]:
                    if value(model.y[i, k]) < 0.5:
                        actual_freq = value(model.f[i, k])
                        required_freq = data['f_l'][k]
                        unsatisfied_services.append(
                            f"{k}({actual_freq:.1f}<{required_freq})"
                        )

                if unsatisfied_services:
                    unsatisfied_users.append(
                        f"{i}: {', '.join(unsatisfied_services)}"
                    )

        if unsatisfied_users:
            print(f"\n未滿足QoS的使用者 ({len(unsatisfied_users)}):")
            for user_info in unsatisfied_users[:10]:  # 只顯示前10個
                print(f"  {user_info}")
            if len(unsatisfied_users) > 10:
                print(f"  ... 還有 {len(unsatisfied_users) - 10} 個")

    except Exception as e:
        print(f"詳細分析時發生錯誤: {e}")


def set_simple_warm_start(model, data):
    """簡單暖啟：選每個節點中容量總和最大的組合，為需求服務指派一個節點"""
    # 選 z
    for n in model.M:
        best_c = None
        best_score = -1
        for (nn, c) in model.Z_INDEX:
            if nn != n:
                continue
            combo = data['combos'][n][c]
            score = sum(combo['W'].get(k, 0) for k in combo['services'])
            if score > best_score:
                best_score = score
                best_c = c
        if best_c is not None:
            model.z[n, best_c].value = 1

    # 指派每個 agent 的每個需求服務到第一個有該服務的節點
    for i in model.G:
        for k in model.R[i]:
            assigned = False
            for n in model.M:
                has_service = any(
                    k in data['combos'][n][c]['services']
                    and (model.z[n, c].value == 1)
                    for (nn, c) in model.Z_INDEX if nn == n
                )
                if has_service:
                    model.x[i, n, k].value = 1
                    # 預設頻率設最低門檻
                    model.s[i, n, k].value = data['f_l'][k]
                    model.y[i, k].value = 1
                    assigned = True
                    break
            if not assigned:
                # 沒找到服務，保持 0
                model.y[i, k].value = 0

    # Y_i
    for i in model.G:
        all_ok = all(
            (model.y[i, k].value or 0) >= 0.5 for k in model.R[i]
        )
        model.Y[i].value = 1 if all_ok and len(model.R[i]) > 0 else 0


def run_single_solve_for_experiment(
    data,
    time_limit,
    rel_gap,
    max_nodes=10000,
    show_solution=False,
):
    """在給定 time_limit / rel_gap 下跑一次 MIP，回傳結果摘要（給實驗用）"""
    from pyomo.opt import TerminationCondition, SolverStatus

    model = build_model(data)
    set_simple_warm_start(model, data)

    solver = configure_solver(
        "highs",
        time_limit=time_limit,
        rel_gap=rel_gap,
        max_nodes=max_nodes,
        first_feasible_only=False,
    )

    # 計時並求解
    t0 = time.time()
    results = solver.solve(model, tee=False)
    solve_time = time.time() - t0
    # 可在回傳中提供 solve_time 方便實驗記錄

    term = results.solver.termination_condition
    status = results.solver.status

    # 判斷是否有可行解
    feasible_terms = {
        TerminationCondition.optimal,
        TerminationCondition.maxTimeLimit,
        TerminationCondition.maxIterations,
    }
    if hasattr(TerminationCondition, "feasible"):
        feasible_terms.add(TerminationCondition.feasible)
    if hasattr(TerminationCondition, "mipGap"):
        feasible_terms.add(TerminationCondition.mipGap)
    has_feasible = (status == SolverStatus.ok and term in feasible_terms)

    # 目標值（若有可行解）
    obj_value = None
    try:
        if has_feasible:
            obj_value = value(model.Obj)
    except Exception:
        pass

    best_obj = getattr(results.solver, "objective", None)     # primal bound
    best_bound = getattr(results.solver, "best_bound", None)  # dual bound
    mip_gap_attr = getattr(results.solver, "mip_gap", None)

    theoretical_gap = None
    if best_obj is not None and best_bound is not None:
        try:
            theoretical_gap = (best_bound - best_obj) / max(1.0, abs(best_obj))
        except ZeroDivisionError:
            theoretical_gap = None

    if show_solution and has_feasible:
        print("\n--- Solution (time_limit="
              f"{time_limit}, rel_gap={rel_gap}) ---")
        detailed_solution_analysis(model, data)
        print_solution_basic(model, data)
    elif show_solution and not has_feasible:
        print("\n⚠️  無可行解，無法列印變數。")

    return {
        "time_limit": time_limit,
        "rel_gap": rel_gap,
        "status": status,
        "termination": term,
        "objective": obj_value,
        "solve_time": solve_time,
        "primal_bound": best_obj,
        "dual_bound": best_bound,
        "theoretical_gap": theoretical_gap,
        "solver_mip_gap": mip_gap_attr,
        "model": model,
        "data": data,
        "has_feasible": has_feasible,
    }


def run_minlp_with_checkpoints(
    data,
    checkpoint_times,
    rel_gap,
    max_nodes=10000,
):
    """單次求解過程中，透過 HiGHS 原生 callback（kCallbackMipImprovingSolution）
    即時記錄每次找到更好 incumbent 的經過時間與目標值，求解時間設為
    checkpoint_times 中的最大值即可，事後直接從記錄的軌跡回推任一 checkpoint
    時間點當下最佳的 incumbent，不需要為每個時間點分別重新求解一次。

    比起讀取 HiGHS 文字求解 log 再解析（tee=True + regex），直接用
    highspy 原生 callback 拿到結構化的 (running_time, mip_primal_bound,
    mip_dual_bound)，不受 log 格式版本差異影響，更穩定可靠。

    注意：此函式繞過 configure_solver()／SolverFactory 走的 Legacy pyomo
    介面，改用 pyomo.contrib.solver.solvers.highs.Highs 這個新式介面直接
    存取其 self._solver_model（原生 highspy.Highs 實例）以便在求解前註冊
    callback；求解參數（presolve/parallel/simplex_strategy）與
    configure_solver() 保持一致，確保跟單次求解版本的結果具可比性。
    """
    import highspy
    from pyomo.contrib.solver.solvers.highs import Highs as PyomoHighs

    model = build_model(data)
    set_simple_warm_start(model, data)

    max_time = max(checkpoint_times)

    solver = PyomoHighs()
    solver.set_instance(model)
    hs = solver._solver_model

    # 事先建好 (i,k)->s[i,n,k]之solver欄位索引 的對照表，供callback捕捉到的
    # mip_solution（HiGHS原生欄位順序之解向量）事後回推每個checkpoint時間點
    # 當下之次要目標Q（同score_from_x()之定義：對所有(agent,service)配對取
    # freq/f_h的正規化平均），不需要在每個checkpoint時間點各自重新求解一次。
    s_cols = {}
    for (i, k) in model.GK_R:
        s_cols[(i, k)] = [
            solver._pyomo_var_to_solver_var_map[id(model.s[i, n, k])]
            for n in model.M
        ]
    f_h_vals = {k: value(model.f_h[k]) for k in model.Q}
    total_pairs = len(list(model.GK_R))

    def _compute_q(sol_vec):
        total = 0.0
        for (i, k), cols in s_cols.items():
            f_ik = sum(sol_vec[c] for c in cols)
            total += f_ik / f_h_vals[k]
        return total / total_pairs if total_pairs > 0 else 0.0

    trajectory = []  # [(running_time, mip_primal_bound, mip_dual_bound, mip_solution), ...]

    def _record_incumbent(callback_type, message, output, cb_input, user_data):
        trajectory.append(
            (
                output.running_time,
                output.mip_primal_bound,
                output.mip_dual_bound,
                list(output.mip_solution),
            )
        )

    hs.setCallback(_record_incumbent, None)
    hs.startCallbackInt(highspy.cb.kCallbackMipImprovingSolution)

    hs.setOptionValue('time_limit', max_time)
    hs.setOptionValue('mip_rel_gap', rel_gap)
    hs.setOptionValue('mip_max_nodes', max_nodes)
    hs.setOptionValue('presolve', 'on')
    hs.setOptionValue('parallel', 'off')
    hs.setOptionValue('simplex_strategy', 1)
    hs.setOptionValue('log_to_console', False)

    t0 = time.time()
    results = solver.solve(model, raise_exception_on_nonoptimal_result=False)
    wall_time = time.time() - t0

    # 注意：這裡走的是新式 pyomo.contrib.solver 介面，回傳的 Results 物件與
    # run_single_solve_for_experiment 用的 Legacy SolverFactory 介面（
    # results.solver.termination_condition，對應 pyomo.opt.TerminationCondition）
    # 是兩套不同的列舉，不能混用；新式介面要用
    # pyomo.contrib.solver.common.results.TerminationCondition，直接以屬性存取
    # （results.termination_condition），不是 results.solver.termination_condition。
    from pyomo.contrib.solver.common.results import (
        TerminationCondition as NewTerminationCondition,
    )
    term = results.termination_condition
    is_proven_optimal = (term == NewTerminationCondition.convergenceCriteriaSatisfied)

    # 跟 run_minlp_ground_truth 同樣理由：time_limit 到但未證明最優時直接從
    # model 讀 Y 變數加總，比信任 pyomo 回報的 objective 更保險。
    final_obj = None
    try:
        final_obj = value(model.Obj)
    except Exception:
        pass

    checkpoints = {}
    checkpoints_q = {}
    for T in sorted(checkpoint_times):
        best_so_far = None
        best_sol = None
        for (t, pb, _db, sol) in trajectory:
            if t <= T and pb is not None and pb > float("-inf"):
                if best_so_far is None or pb > best_so_far:
                    best_so_far = pb
                    best_sol = sol
        checkpoints[T] = best_so_far
        checkpoints_q[T] = _compute_q(best_sol) if best_sol is not None else None
    # trajectory 是否即時捕捉到「求解到 time_limit 那一刻」的最終 incumbent，
    # 取決於 HiGHS 是否在收尾時仍觸發 improving-solution callback，不保證一定
    # 有；最大時間點的 checkpoint 改以求解完成後直接讀 model 的最終值為準，
    # 避免跟 final_objective 對不上。Q 亦比照辦理，直接從最終 model 的 s 變數值
    # 計算，而非沿用trajectory最後一筆快照。
    if final_obj is not None:
        checkpoints[max_time] = final_obj
        try:
            checkpoints_q[max_time] = sum(
                value(model.f[i, k]) / f_h_vals[k] for (i, k) in model.GK_R
            ) / total_pairs if total_pairs > 0 else 0.0
        except Exception:
            pass

    return {
        "checkpoint_times": sorted(checkpoint_times),
        "checkpoints_q": checkpoints_q,
        "checkpoints": checkpoints,  # {time: 該時間點當下最佳 incumbent}
        "trajectory": trajectory,
        "final_objective": final_obj,
        "final_termination": term,
        "solution_status": results.solution_status,
        "is_proven_optimal": is_proven_optimal,
        "wall_time": wall_time,
        "model": model,
        "data": data,
    }


def experiment_time_limits(
    time_limits,
    baseline_time_limit=300,
    baseline_rel_gap=0.01,
    rel_gap_for_runs=0.25,
    show_last_solution=False,
):
    """對同一組 data 用不同 time_limit 做實驗，列出解的品質變化，可選擇印最後一次的解"""
    print("\n=== Time limit experiment ===")

    data = load_data_by_config()

    print(
        f"\n[Baseline] time_limit={baseline_time_limit}s, "
        f"mip_rel_gap={baseline_rel_gap}"
    )
    baseline = run_single_solve_for_experiment(
        data,
        time_limit=baseline_time_limit,
        rel_gap=baseline_rel_gap,
        show_solution=False,
    )
    z_best = baseline["objective"]
    print(f"Baseline objective (近似最優解): {z_best}")

    # 若 time_limits 為空，僅執行 baseline 並依需求印出 baseline 解
    if not time_limits:
        print("\n⚠️  未提供 time_limits（清單為空），僅執行 baseline。")
        if show_last_solution:
            if baseline.get("has_feasible"):
                print("\n=== Baseline solution details ===")
                detailed_solution_analysis(baseline["model"], data)
                print_solution_basic(baseline["model"], data)
            else:
                print("\n⚠️  基準執行未取得可行解，無法列印。")
        return

    print(
        "\n[Experiment] 使用相同 mip_rel_gap="
        f"{rel_gap_for_runs}，測試不同 time_limit："
    )

    results_list = []
    for T in time_limits:
        print(f"\n  -> Running with time_limit={T}s ...")
        res = run_single_solve_for_experiment(
            data,
            time_limit=T,
            rel_gap=rel_gap_for_runs,
            show_solution=False,  # 不在迴圈中印，避免冗長
        )
        results_list.append(res)

    print("\n=== Time limit vs solution quality ===")
    header = (
        "time_limit  obj(T)   emp_gap(%)  status/term               "
        "primal_bound  dual_bound  theo_gap(%)"
    )
    print(header)
    print("-" * len(header))

    for res in results_list:
        T = res["time_limit"]
        objT = res["objective"]
        status = res["status"]
        term = res["termination"]
        pb = res["primal_bound"]
        db = res["dual_bound"]

        if z_best is not None and objT is not None:
            emp_gap_pct = (z_best - objT) / max(1.0, abs(z_best)) * 100
        else:
            emp_gap_pct = None

        if res["theoretical_gap"] is not None:
            theo_gap_pct = res["theoretical_gap"] * 100
        elif res["solver_mip_gap"] is not None:
            theo_gap_pct = res["solver_mip_gap"] * 100
        else:
            theo_gap_pct = None

        def fmt(x):
            return f"{x:8.2f}" if isinstance(x, (int, float)) and x is not None else f"{str(x):>8}"

        print(
            f"{T:9d}  "
            f"{fmt(objT)}  "
            f"{fmt(emp_gap_pct)}  "
            f"{str(status)[:6]}/{str(term)[:10]:<10}  "
            f"{fmt(pb)}  {fmt(db)}  {fmt(theo_gap_pct)}"
        )

    if show_last_solution and results_list:
        last = results_list[-1]
        if last["has_feasible"]:
            print("\n=== Last run solution details ===")
            detailed_solution_analysis(last["model"], data)
            print_solution_basic(last["model"], data)
        else:
            print("\n⚠️  最後一次執行未取得可行解，無法列印。")


def print_solution_basic(model, data):
    """列印核心決策變數與目標值（限制輸出量）"""
    MAX_PRINT = 20
    try:
        from pyomo.environ import value
        print("\n=== BASIC SOLUTION (summary) ===")
        try:
            print(f"Objective: {value(model.Obj):.4f}")
        except Exception:
            print("Objective: (不可讀取)")

        # 組合部署 z（摘要）
        deployed = []
        for (n, c) in model.Z_INDEX:
            try:
                if value(model.z[n, c]) > 0.5:
                    combo_info = data['combos'][n][c]
                    deployed.append((n, c, combo_info['services']))
            except Exception:
                pass
        print(f"\n部署組合總數: {len(deployed)}")
        for item in deployed[:MAX_PRINT]:
            n, c, services = item
            print(f"  {n} 使用組合 {c}: 服務 {services}")
        if len(deployed) > MAX_PRINT:
            print(f"  ... 還有 {len(deployed)-MAX_PRINT} 個被截斷")

        # 使用者滿足 Y[i]（摘要）
        satisfied = []
        for i in model.G:
            try:
                if value(model.Y[i]) > 0.5:
                    satisfied.append(i)
            except Exception:
                pass
        print(f"\n滿足使用者總數: {len(satisfied)}")
        for i in satisfied[:MAX_PRINT]:
            print(f"  {i}")
        if len(satisfied) > MAX_PRINT:
            print(f"  ... 還有 {len(satisfied)-MAX_PRINT} 個被截斷")

        # 服務層滿足 y[i,k]（摘要）
        y_list = []
        for i in model.G:
            for k in model.R[i]:
                try:
                    if value(model.y[i, k]) > 0.5:
                        y_list.append((i, k))
                except Exception:
                    pass
        print(f"\n服務滿足總數 (y=1): {len(y_list)} (僅列前{MAX_PRINT})")
        for i,k in y_list[:MAX_PRINT]:
            print(f"  i={i}, k={k}")
        if len(y_list) > MAX_PRINT:
            print(f"  ... 還有 {len(y_list)-MAX_PRINT} 筆被截斷")

        # 指派 x 與頻率 s（摘要）
        assign_list = []
        for i in model.G:
            for n in model.M:
                for k in model.Q:
                    try:
                        xv = value(model.x[i, n, k])
                        sv = value(model.s[i, n, k])
                        if (xv is not None and xv > 0.5) or (sv is not None and sv > 1e-8):
                            assign_list.append((i, n, k, xv, sv))
                    except Exception:
                        pass
        print(f"\n指派總數 (非零 x/s): {len(assign_list)} (僅列前{MAX_PRINT})")
        for rec in assign_list[:MAX_PRINT]:
            i,n,k,xv,sv = rec
            print(f"  i={i}, n={n}, k={k}, x={xv:.0f}, s={sv:.2f}")
        if len(assign_list) > MAX_PRINT:
            print(f"  ... 還有 {len(assign_list)-MAX_PRINT} 筆被截斷")

        # 使用者頻率 f（若有）
        if hasattr(model, "f"):
            f_list = []
            for i in model.G:
                for k in model.Q:
                    try:
                        fv = value(model.f[i, k])
                        if fv is not None and abs(fv) > 1e-8:
                            f_list.append((i, k, fv))
                    except Exception:
                        pass
            print(f"\n使用者頻率 f 非零筆數: {len(f_list)} (僅列前{MAX_PRINT})")
            for rec in f_list[:MAX_PRINT]:
                i,k,fv = rec
                print(f"  i={i}, k={k}, f={fv:.2f}")
            if len(f_list) > MAX_PRINT:
                print(f"  ... 還有 {len(f_list)-MAX_PRINT} 筆被截斷")

    except Exception as e:
        print(f"列印求解結果時發生錯誤: {e}")


def main():
    print("=== Optimization Run ===")
    data = load_data_by_config()

    is_likely_feasible = quick_feasibility_check(data)
    if not is_likely_feasible:
        print("⚠️  預檢查顯示問題可能不可行，但仍會嘗試求解...")

    model = build_model(data)
    set_simple_warm_start(model, data)

    solver = configure_solver(
        "highs",
        time_limit=60,
        rel_gap=0.25,
        max_nodes=10000,
        first_feasible_only=True
    )
    print(
        "\n求解器設定: time_limit=60s, mip_rel_gap=0.25, "
        "mip_max_nodes=10000"
    )

    results = solver.solve(model, tee=True)

    term = results.solver.termination_condition
    status = results.solver.status

    print("\n=== Solve summary ===")
    print(f"status={status}, termination={term}")

    best_obj = getattr(results.solver, "objective", None)
    best_bound = getattr(results.solver, "best_bound", None)
    mip_gap_attr = getattr(results.solver, "mip_gap", None)

    if best_obj is not None:
        print(f"reported best objective = {best_obj}")
    if best_bound is not None:
        print(f"reported best bound     = {best_bound}")
    if mip_gap_attr is not None:
        print(f"reported mip gap        = {mip_gap_attr}")

    feasible_terms = {
        TerminationCondition.optimal,
        TerminationCondition.maxTimeLimit,
        TerminationCondition.maxIterations,
    }
    if hasattr(TerminationCondition, "feasible"):
        feasible_terms.add(TerminationCondition.feasible)
    if hasattr(TerminationCondition, "mipGap"):
        feasible_terms.add(TerminationCondition.mipGap)

    has_feasible_solution = (
        status == SolverStatus.ok and
        term in feasible_terms
    )

    if has_feasible_solution:
        print("\n=== Incumbent objective (best known feasible solution) ===")
        try:
            print(value(model.Obj))
        except Exception:
            print("無法讀取目標值，但求解器回報存在可行解。")
        detailed_solution_analysis(model, data)
        print_solution_basic(model, data)  # 新增：印出核心結果
        try:
            save_model_info(model, data, results, filename="model_analysis.json")
            print("\n已將結果摘要存到 model_analysis.json")
        except Exception as e:
            print(f"儲存摘要失敗: {e}")
    else:
        print(
            "\n未取得可行解。"
            f" (solver status={status}, termination={term})"
        )
        # 若你想在無解時做更多 debug，可以打開這行：
        # analyze_infeasibility(model, solver, results, data)


if __name__ == "__main__":
    try:
        # 選一種：
        # 1) 直接跑 main() 印出求解結果
        # main()

        # 2) 做 time limit 實驗並印最後一次的解
        experiment_time_limits(
            time_limits=[],
            baseline_time_limit=600,
            baseline_rel_gap=0.1,
            rel_gap_for_runs=0.01,
            show_last_solution=True,
        )
    except Exception as e:
        import traceback
        print(f"[ERROR] 程式異常: {e}")
        traceback.print_exc()
