"""4-3-5節：整體演算法複雜度分析——GA（本研究方法）vs MINLP精確求解器

比較對象：
- 本研究方法：GA（bench_ga_v2_CUR.py run_ga），世代數 in {5,10,20,50}
- 精確求解器：MINLP/HiGHS（/home/hiro/git_repo/minlp/run.py），作為A_sat之最優解上界(ground truth)

資料來源：
- 環境（節點/服務/W(i,c,j)容量表）：ha/Controller_v2/information/aws_prod_serviceSpec_mul.json
- 使用者訂閱組合：ha/Controller_v2/user_profiles.json

注意：MINLP求解需要pyomo/highs，僅在 /home/hiro/git_repo/minlp/venv 裡有安裝，
必須用該venv的python執行本腳本：
    /home/hiro/git_repo/minlp/venv/bin/python3 complexity_experiment_4_3_5.py
"""

import json
import sys
import time
import statistics
from pathlib import Path

CONTROLLER_DIR = Path(__file__).resolve().parent.parent / "controller"
MINLP_DIR = Path(__file__).resolve().parent / "minlp"

sys.path.insert(0, str(CONTROLLER_DIR))
sys.path.insert(0, str(MINLP_DIR))

from bench_ga_v2_CUR import run_ga  # noqa: E402
from model import build_model  # noqa: E402
from run import run_single_solve_for_experiment, run_minlp_with_checkpoints  # noqa: E402

SPEC_PATH = CONTROLLER_DIR / "information" / "aws_prod_serviceSpec_mul.json"
PROFILES_PATH = CONTROLLER_DIR / "information" / "complexity_experiment_subscription_patterns.json"
GA_MODEL_OUT_PATH = CONTROLLER_DIR / "information" / "complexity_experiment_model.json"

GENERATIONS_LIST = [5, 10, 20, 50]
GENERATIONS_LIST_4SVC = [5, 10, 20, 50, 75, 100, 150]  # 情境B（4服務）搜索空間較大，多測幾個世代數
N_SEEDS = 5            # 內層：同一次 trial 內跑幾個不同種子，取其中最佳（max）值
OUTER_TRIALS = 10      # 外層：把「N_SEEDS 個種子取最佳」重複幾次，最後對這些 trial 的最佳值取平均±標準差
GA_POP_SIZE = 20       # 對齊 controller_v2.py cfg_ga_pop_size() 預設
GA_PC = 0.9            # 對齊 controller_v2.py 主迴圈
GA_PM = 0.3            # 對齊 controller_v2.py 主迴圈 mutate_hybrid(pm=0.3)
GA_ALLOCATOR = "nsats_plus"  # 對齊 controller_v2.py 'ga' 模式之 _ga_eval

MINLP_TIME_LIMIT = 300
MINLP_REL_GAP = 1e-6   # 逼近真正最優解，而非近似
CHECKPOINT_TIMES = [1, 10, 60, 300, 600]  # 單次求解、事後回推各時間點之最佳incumbent


def build_shared_data(group_size: int = 12):
    """從真實環境+使用者資料，建出GA與MINLP兩邊等價的問題實例。

    group_size：e/a/b三組（各自訂閱[gesture,object]/[pose,gesture]/[pose,object]）
    每組的agent數量，預設12（對齊user_profiles.json原始36人情境）。純粹依user_profiles.json
    既有的三組配對比例往上等比例延伸，不動用任何合成/推測資料——3個服務、W(i,c,j)
    容量表皆為aws_prod_serviceSpec_mul.json的真實壓測值，只有agent數量規模變動。
    """
    specs = json.load(open(SPEC_PATH, encoding="utf-8"))
    profiles = json.load(open(PROFILES_PATH, encoding="utf-8"))

    nodes = list(specs["nodeAliases"].keys())
    services = list(specs["services"].keys())
    f_l = {s: specs["services"][s]["frequencyLimit"][1] for s in services}
    f_h = {s: specs["services"][s]["frequencyLimit"][0] for s in services}

    # 從user_profiles.json取出三組的訂閱組合定義（e/a/b各自要哪兩個服務），
    # 而不是直接沿用檔案裡固定的36個agent——這樣才能忠實延伸同一份「配對比例」
    # 到任意group_size，且不需要編輯user_profiles.json本身。
    group_defs = []
    seen_patterns = []
    for _, reqs in profiles["users"].items():
        pattern = tuple(sorted(reqs))
        if pattern not in seen_patterns:
            seen_patterns.append(pattern)
            group_defs.append(list(reqs))

    prefixes = ["e", "a", "b"]
    agents, requirements = [], {}
    for prefix, reqs in zip(prefixes, group_defs):
        for i in range(1, group_size + 1):
            ag = f"{prefix}{i}"
            agents.append(ag)
            requirements[ag] = reqs

    # GA 用：legal_masks[node][mask_int_str] = {service: cap, ...}
    legal_masks = {node: {} for node in nodes}
    # MINLP 用：combos[node][combo_id] = {"services":[...], "W": {...}}
    combos = {node: {} for node in nodes}

    for node in nodes:
        node_ability = specs["workAbility"][node]
        for combo_str, cap_dict in node_ability.items():
            svc_list = combo_str.split(",")
            mask = 0
            for s in svc_list:
                mask |= (1 << services.index(s))
            legal_masks[node][str(mask)] = cap_dict
            combos[node][combo_str] = {"services": svc_list, "W": cap_dict}

    ga_model = {
        "services": services, "nodes": nodes, "agents": agents,
        "requirements": requirements, "legal_masks": legal_masks,
        "f_l": f_l, "f_h": f_h,
    }

    minlp_data = {
        "M": nodes, "Q": services, "G": agents, "R": requirements,
        "combos": combos, "f_l": f_l, "f_h": f_h,
    }

    return ga_model, minlp_data


SVC4_NAME = "svc4"
SVC4_BASE_CAP = 95.0   # 合成值，落在真實3服務82~113的量級區間內，無真實壓測依據
DECAY_PER_EXTRA_SERVICE = 0.85  # 取自真實資料實測平均衰退率（2服務共置約85%、3服務約74%）


def build_shared_data_4services(group_size: int = 12, total_agents: int = None):
    """在真實3服務環境上，疊加第4個合成服務（無真實壓測資料），測試服務數量增加
    對複雜度的影響。

    agent總數之縮放規則（total_agents優先於group_size）：依服務數量比例放大，
    例如3服務60人時，4服務對應為60*4/3=80人——放大比例跟著「服務種類數」走，
    而非跟著「配對組合數C(Q,2)」走（後者3→4服務會變成3→6配對，放大2倍，會
    over-放大、混入「配對變多本身」的效應；比例應對齊服務數量本身3→4=4/3倍，
    才能跟只變動agent數、不變動服務數的對照組（例如60人/3服務）做乾淨比較）。
    80人分配到C(4,2)=6種配對時無法整除，以盡量平均的方式分配（部分配對14人、
    部分13人）。

    - gesture/pose/object三者之間的全部7種組合，維持aws_prod_serviceSpec_mul.json
      之真實壓測數值，完全不更動。
    - 任何牽涉到第4服務(svc4)的組合（共2^4-1-(2^3-1)=8種新組合／節點），因無真實
      壓測資料，改用W(k|組合大小n) = base_cap[k] * 0.85^(n-1) 之合成衰退模型推算，
      此為複雜度分析所需之必要外推，非量測值，需在論文中明確區分。
    - 未指定total_agents時，沿用group_size=12之預設（C(4,2)*12=72人）。
    """
    import itertools

    specs = json.load(open(SPEC_PATH, encoding="utf-8"))
    nodes = list(specs["nodeAliases"].keys())
    real_services = list(specs["services"].keys())  # [gesture, pose, object]
    services = real_services + [SVC4_NAME]

    f_l = {s: specs["services"][s]["frequencyLimit"][1] for s in real_services}
    f_h = {s: specs["services"][s]["frequencyLimit"][0] for s in real_services}
    f_l[SVC4_NAME] = f_l[real_services[0]]  # 沿用真實服務之QoS門檻（皆相同）
    f_h[SVC4_NAME] = f_h[real_services[0]]

    base_caps = {s: specs["workAbility"][nodes[0]][s][s] for s in real_services}
    base_caps[SVC4_NAME] = SVC4_BASE_CAP

    legal_masks = {node: {} for node in nodes}
    combos = {node: {} for node in nodes}

    for node in nodes:
        real_ability = specs["workAbility"][node]
        # 1) 真實3服務的7種組合：原封不動照抄真實壓測值
        for combo_str, cap_dict in real_ability.items():
            svc_list = combo_str.split(",")
            mask = sum(1 << services.index(s) for s in svc_list)
            legal_masks[node][str(mask)] = cap_dict
            combos[node][combo_str] = {"services": svc_list, "W": cap_dict}

        # 2) 任何牽涉svc4的組合：合成衰退模型推算（8種新組合，size從0開始才會
        #    包含「svc4單獨部署」這個組合——combinations(...,0)恰好產生一個空
        #    tuple，對應svc_list=[svc4]單獨存在）
        for size in range(0, len(real_services) + 1):
            for subset in itertools.combinations(real_services, size):
                svc_list = list(subset) + [SVC4_NAME]
                combo_str = ",".join(svc_list)
                n = len(svc_list)
                decay = DECAY_PER_EXTRA_SERVICE ** (n - 1)
                cap_dict = {k: round(base_caps[k] * decay, 1) for k in svc_list}
                mask = sum(1 << services.index(s) for s in svc_list)
                legal_masks[node][str(mask)] = cap_dict
                combos[node][combo_str] = {"services": svc_list, "W": cap_dict}

    # agent：4服務兩兩配對，C(4,2)=6種配對；total_agents無法整除配對數時，
    # 以divmod盡量平均分配（前n_extra組多分1人）
    pairs = list(itertools.combinations(services, 2))
    prefixes = [f"grp{i}" for i in range(1, len(pairs) + 1)]
    n_pairs = len(pairs)
    if total_agents is not None:
        base_n, n_extra = divmod(total_agents, n_pairs)
        sizes = [base_n + 1 if idx < n_extra else base_n for idx in range(n_pairs)]
    else:
        sizes = [group_size] * n_pairs

    agents, requirements = [], {}
    for prefix, pair, size in zip(prefixes, pairs, sizes):
        for i in range(1, size + 1):
            ag = f"{prefix}_{i}"
            agents.append(ag)
            requirements[ag] = list(pair)

    ga_model = {
        "services": services, "nodes": nodes, "agents": agents,
        "requirements": requirements, "legal_masks": legal_masks,
        "f_l": f_l, "f_h": f_h,
    }
    minlp_data = {
        "M": nodes, "Q": services, "G": agents, "R": requirements,
        "combos": combos, "f_l": f_l, "f_h": f_h,
    }
    return ga_model, minlp_data


def run_minlp_ground_truth(minlp_data):
    """跑MINLP精確求解，並回傳求解時間、目標值。

    注意：當求解在time_limit內未能證明最優（status=aborted，例如
    termination=maxTimeLimit/maxIterations）時，run_single_solve_for_experiment
    自身的has_feasible判斷過嚴，會把incumbent(目前最佳解)的目標值回報成None，
    即使pyomo/HiGHS實際上已將該incumbent解載入model變數中。這裡改為直接從
    res["model"]讀取Y變數加總，無論status為何都能拿到目前找到的最佳解，
    並额外標記is_proven_optimal，讓「求解時間到了但沒證明最優」跟「真的解出
    最優」在報告裡能區分清楚。
    """
    from pyomo.environ import value

    print(f"\n=== MINLP 精確求解（time_limit={MINLP_TIME_LIMIT}s, rel_gap={MINLP_REL_GAP}）===")
    res = run_single_solve_for_experiment(
        minlp_data,
        time_limit=MINLP_TIME_LIMIT,
        rel_gap=MINLP_REL_GAP,
        show_solution=False,
    )

    incumbent_obj = res["objective"]
    if incumbent_obj is None:
        try:
            incumbent_obj = sum(value(res["model"].Y[i]) for i in res["model"].G)
        except Exception as e:
            print(f"  (無法從model直接讀取incumbent: {e})")

    from pyomo.opt import TerminationCondition
    is_proven_optimal = res["termination"] == TerminationCondition.optimal

    print(f"termination={res['termination']}  status={res['status']}  "
          f"is_proven_optimal={is_proven_optimal}")
    print(f"incumbent objective(A_sat)={incumbent_obj}  solve_time={res['solve_time']:.4f}s "
          f"(time_limit={MINLP_TIME_LIMIT}s)")
    print(f"primal_bound={res['primal_bound']}  dual_bound={res['dual_bound']}  "
          f"theoretical_gap={res['theoretical_gap']}")

    res["incumbent_objective"] = incumbent_obj
    res["is_proven_optimal"] = is_proven_optimal
    return res


def run_ga_sweep(ga_model_path, generations_list=None):
    """對每個世代數，跑 OUTER_TRIALS 次獨立 trial；每次 trial 內跑 N_SEEDS 個不同
    種子的 GA，取其中 nsat 最高者（打平時取 Q 較高者）作為該次 trial 的代表值，
    而非對 N_SEEDS 個種子取平均——最終彙總統計是對 OUTER_TRIALS 次「trial 內最佳
    值」取平均±標準差，用以驗證此最佳值之收斂結果具穩定性、並非單次僥倖跑到，
    而非模擬允許重跑取最佳解之操作情境。"""
    generations_list = generations_list if generations_list is not None else GENERATIONS_LIST
    print(f"\n=== GA 世代數掃描 {generations_list}，每個世代數跑 {OUTER_TRIALS} 次 trial，"
          f"每次 trial 取 {N_SEEDS} 個種子中之最佳值 ===")
    raw_rows = []    # 每一次單一種子之原始結果，供除錯／透明度用
    trial_rows = []  # 每一次 trial（N_SEEDS 個種子中之最佳）
    seed_counter = 0
    for gens in generations_list:
        for trial in range(OUTER_TRIALS):
            trial_candidates = []
            for _ in range(N_SEEDS):
                seed = seed_counter
                seed_counter += 1
                best_gene, final_fit, total_time, done_gens = run_ga(
                    model_path=str(ga_model_path),
                    pop_size=GA_POP_SIZE,
                    generations=gens,
                    pc=GA_PC,
                    pm=GA_PM,
                    seed=seed,
                    log_csv=None,
                    allocator=GA_ALLOCATOR,
                )
                nsat, q = final_fit
                raw_rows.append({
                    "generations": gens, "trial": trial, "seed": seed,
                    "nsat": nsat, "Q": q, "time_s": total_time,
                })
                trial_candidates.append({"nsat": nsat, "Q": q, "time_s": total_time})
            best = max(trial_candidates, key=lambda r: (r["nsat"], r["Q"]))
            trial_rows.append({
                "generations": gens, "trial": trial,
                "nsat": best["nsat"], "Q": best["Q"], "time_s": best["time_s"],
            })
            print(f"  gens={gens:3d} trial={trial}  best_of_{N_SEEDS}_seeds: "
                  f"nsat={best['nsat']}  Q={best['Q']:.3f}  time={best['time_s']:.4f}s")
    return raw_rows, trial_rows


def summarize(trial_rows, minlp_obj, generations_list=None):
    generations_list = generations_list if generations_list is not None else GENERATIONS_LIST
    print(f"\n=== 彙總（各世代數：{OUTER_TRIALS} 次 trial 之平均±標準差，"
          f"每次 trial 為 {N_SEEDS} 個種子中之最佳值）===")
    header = f"{'generations':>12} {'nsat_mean':>10} {'nsat_std':>9} {'Q_mean':>8} {'Q_std':>8} {'time_mean_s':>12} {'time_std_s':>11} {'gap_to_opt':>11}"
    print(header)
    print("-" * len(header))
    summary_rows = []
    for gens in generations_list:
        subset = [r for r in trial_rows if r["generations"] == gens]
        nsats = [r["nsat"] for r in subset]
        qs = [r["Q"] for r in subset]
        times = [r["time_s"] for r in subset]
        nsat_mean = statistics.mean(nsats)
        nsat_std = statistics.stdev(nsats) if len(nsats) > 1 else 0.0
        q_mean = statistics.mean(qs)
        q_std = statistics.stdev(qs) if len(qs) > 1 else 0.0
        time_mean = statistics.mean(times)
        time_std = statistics.stdev(times) if len(times) > 1 else 0.0
        gap = (minlp_obj - nsat_mean) if minlp_obj is not None else None
        print(f"{gens:>12} {nsat_mean:>10.2f} {nsat_std:>9.2f} {q_mean:>8.2f} {q_std:>8.2f} "
              f"{time_mean:>12.4f} {time_std:>11.4f} {str(gap):>11}")
        summary_rows.append({
            "generations": gens, "nsat_mean": nsat_mean, "nsat_std": nsat_std,
            "Q_mean": q_mean, "Q_std": q_std, "time_mean_s": time_mean, "time_std_s": time_std,
            "gap_to_opt": gap,
        })
    return summary_rows


def main():
    args = sys.argv[1:]
    use_4services = "--4services" in args
    args = [a for a in args if a != "--4services"]
    group_size = int(args[0]) if args else 12

    if use_4services:
        n_pairs = 6  # C(4,2)
        total_agents = group_size * n_pairs
        print(f"=== 4服務模式：group_size={group_size} (總agent數={total_agents}) ===")
        ga_model, minlp_data = build_shared_data_4services(group_size=group_size)
    else:
        total_agents = group_size * 3
        print(f"=== 3服務模式：group_size={group_size} (總agent數={total_agents}) ===")
        ga_model, minlp_data = build_shared_data(group_size=group_size)

    GA_MODEL_OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(GA_MODEL_OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(ga_model, f, ensure_ascii=False, indent=2)
    print(f"GA model 已輸出: {GA_MODEL_OUT_PATH}")
    print(f"問題規模: nodes={len(ga_model['nodes'])}, services={len(ga_model['services'])}, "
          f"agents={len(ga_model['agents'])}")

    print(f"\n=== MINLP 精確求解（單次求解、checkpoint_times={CHECKPOINT_TIMES}s, "
          f"rel_gap={MINLP_REL_GAP}，不設節點數上限，僅受 time_limit 約束）===")
    import highspy
    minlp_res = run_minlp_with_checkpoints(
        minlp_data,
        checkpoint_times=CHECKPOINT_TIMES,
        rel_gap=MINLP_REL_GAP,
        max_nodes=highspy.kHighsIInf,  # 不設節點數上限，只靠 time_limit 決定何時停止
    )
    print(f"checkpoints={minlp_res['checkpoints']}")
    print(f"final_termination={minlp_res['final_termination']}  "
          f"is_proven_optimal={minlp_res['is_proven_optimal']}  "
          f"wall_time={minlp_res['wall_time']:.2f}s")

    gens_list = GENERATIONS_LIST_4SVC if use_4services else GENERATIONS_LIST
    ga_raw_rows, ga_trial_rows = run_ga_sweep(GA_MODEL_OUT_PATH, generations_list=gens_list)

    final_minlp_obj = minlp_res["checkpoints"][max(CHECKPOINT_TIMES)]
    summary = summarize(ga_trial_rows, final_minlp_obj, generations_list=gens_list)

    out = {
        "minlp": {
            "checkpoints": minlp_res["checkpoints"],
            "checkpoints_q": minlp_res["checkpoints_q"],
            "checkpoint_times": minlp_res["checkpoint_times"],
            "final_objective": minlp_res["final_objective"],
            "is_proven_optimal": minlp_res["is_proven_optimal"],
            "wall_time_s": minlp_res["wall_time"],
            "final_termination": str(minlp_res["final_termination"]),
            "solution_status": str(minlp_res["solution_status"]),
            "rel_gap": MINLP_REL_GAP,
        },
        "ga_raw_runs": ga_raw_rows,
        "ga_trial_best": ga_trial_rows,
        "ga_summary": summary,
        "config": {
            "generations_list": gens_list,
            "n_seeds_per_trial": N_SEEDS, "outer_trials": OUTER_TRIALS,
            "ga_pop_size": GA_POP_SIZE, "ga_pc": GA_PC, "ga_pm": GA_PM,
            "ga_allocator": GA_ALLOCATOR,
            "group_size": group_size, "total_agents": total_agents,
            "num_services": len(ga_model["services"]), "use_4services": use_4services,
        },
    }
    tag = "_4svc" if use_4services else ""
    out_path = CONTROLLER_DIR / "information" / f"complexity_experiment_4_3_5_result_n{total_agents}{tag}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n完整結果已存到: {out_path}")


if __name__ == "__main__":
    main()
