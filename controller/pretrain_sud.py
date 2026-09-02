"""
SUD Pre-training Script
=======================
離線模擬「舊部署 + 新需求湧入」情境，讓 Q-table 在正式實驗前收斂。

核心邏輯：
  每個 episode 模擬一次「增量訂閱」事件：
    1. 隨機在 K 個節點（K < N）上建稀疏初始部署（模擬前一時刻的系統狀態）
    2. 以 PSS 實際公式算出最小需求量，讓 n_new 保證觸發 PSS
    3. PSS 偵測到舊部署容量不足 → SUD 決定 ADD 哪個服務到哪個節點
    4. Q-table 累積更新

  初始部署刻意稀疏（不呼叫 USC-TS），因為 USC-TS 在 8 節點環境下
  對少量需求仍會部署至全節點，導致 PSS 永遠無法觸發。

執行方式：
    python pretrain_sud.py                          # 8-node spec, 1000 episodes
    python pretrain_sud.py --episodes 3000 --max-agents 80
    python pretrain_sud.py --spec information/serviceSpec_mul.json  # 2-node spec
"""

import argparse
import json
import math
import logging
import os
import random
import sys
import time

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
sys.path.insert(0, os.path.dirname(__file__))

from svc_updating_decision import SUDAgent, _save_q_table
from usc_ts_solver import _get_node_combo_capacity

_DEFAULT_SPEC = os.path.join(
    os.path.dirname(__file__), "information", "serviceSpec_mul_8node.json"
)


def _make_subs(n: int, svcs: list) -> list:
    return [
        {"agentIP": f"10.0.{i//256}.{i%256}", "agentPort": 9000 + i,
         "subscriptions": [{"serviceType": s} for s in svcs]}
        for i in range(n)
    ]


def _sparse_deployment(nodes: list, svc_combo: list, k: int) -> dict:
    """Deploy svc_combo on exactly k randomly selected nodes, rest empty."""
    subset = set(random.sample(nodes, k))
    return {n: list(svc_combo) if n in subset else [] for n in nodes}


def _pss_threshold(deployment: dict, svc_combo: list,
                   nodes: list, specs: dict) -> int:
    """
    Returns the minimum n_new that will cause PSS to trigger for at least one
    service in svc_combo, using the same formula as _pss():
        d * f_limit > total_combo_cap_fps
    → minimum d = floor(total_fps / f_limit) + 1
    """
    combo_cap = _get_node_combo_capacity(specs, deployment)
    f_l = {s: specs["services"][s]["frequencyLimit"][1] for s in svc_combo}

    threshold = 1
    for svc in svc_combo:
        total_fps = sum(
            combo_cap.get((n, svc), 0.0)
            for n in nodes
            if svc in deployment.get(n, [])
        )
        fl = f_l[svc]
        if fl > 0:
            min_d = math.floor(total_fps / fl) + 1
            threshold = max(threshold, min_d)
    return threshold


def run_pretrain(n_episodes: int = 1000,
                 max_agents: int = 80,
                 seed: int = 42,
                 spec_file: str = _DEFAULT_SPEC) -> None:
    with open(spec_file) as fh:
        specs = json.load(fh)

    # 與 controller_v2.py 相同：把 workAbility 的 raw IP key 換成 alias，
    # 讓預訓練產出的 Q-table action key 與執行期 generic_nodes 一致。
    aliases = specs.get("nodeAliases", {})
    if aliases:
        specs = dict(specs)
        specs["workAbility"] = {aliases.get(k, k): v for k, v in specs["workAbility"].items()}

    services = list(specs["services"].keys())
    nodes    = list(specs["workAbility"].keys())   # alias 名稱（node-1, node-2, …）
    n_nodes  = len(nodes)

    random.seed(seed)
    SUDAgent.reset()
    agent = SUDAgent.get_instance()
    agent.initialized = True
    agent.epsilon = 0.8

    print(f"Pre-training SUD: {n_episodes} episodes")
    print(f"  spec      : {spec_file}")
    print(f"  nodes ({n_nodes}): {nodes}")
    print(f"  services  : {services}")
    print(f"  max_agents: {max_agents}")

    t0 = time.time()
    pss_triggered = 0
    pss_skipped   = 0

    for ep in range(1, n_episodes + 1):
        # ── 選擇服務組合 ──────────────────────────────────────────────────
        k_svc = random.choices([1, 2, 3], weights=[4, 3, 1])[0]
        svc_combo = random.sample(services, k_svc)

        # ── 稀疏初始部署：用 k_nodes 個節點，保留至少 1 個節點空著 ────────
        k_nodes = random.randint(1, n_nodes - 1)
        start_deployment = _sparse_deployment(nodes, svc_combo, k_nodes)

        # ── n_new：使用 PSS 公式算出門檻，確保觸發 ────────────────────────
        n_new_min = _pss_threshold(start_deployment, svc_combo, nodes, specs)
        n_new_max = max(n_new_min + 1, max_agents)
        n_new = random.randint(n_new_min, n_new_max)
        new_subs = _make_subs(n_new, svc_combo)

        # ── SUD episode ───────────────────────────────────────────────────
        result = agent.decide(
            current_deployment=start_deployment,
            subscriptions=new_subs,
            nodes=nodes,
            services=services,
            specs=specs,
            dry_run=False,
        )
        # PSS fired if deployment was modified (SUD added at least one service)
        if result != start_deployment:
            pss_triggered += 1
        else:
            pss_skipped += 1

        # pretrain 內部額外 ε 衰減
        agent.epsilon = max(0.05, agent.epsilon * 0.997)

        if ep % 100 == 0:
            elapsed = time.time() - t0
            print(f"  ep {ep:>5}/{n_episodes}"
                  f"  Q-table: {len(agent.q_table):>4} entries"
                  f"  PSS hit: {pss_triggered:>4}"
                  f"  PSS miss: {pss_skipped:>4}"
                  f"  ε: {agent.epsilon:.4f}"
                  f"  {elapsed:.1f}s")

    _save_q_table(agent.q_table)
    print(f"\nDone.")
    print(f"  Q-table entries : {len(agent.q_table)}")
    print(f"  PSS triggered   : {pss_triggered}/{n_episodes} episodes")
    print(f"  PSS skipped     : {pss_skipped}/{n_episodes} episodes")
    print(f"  Final ε         : {agent.epsilon:.4f}")
    print(f"  Saved to        : information/q_table.json")
    print(f"  Total time      : {time.time() - t0:.1f}s")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--episodes",   type=int,  default=1000)
    parser.add_argument("--max-agents", type=int,  default=80)
    parser.add_argument("--seed",       type=int,  default=42)
    parser.add_argument("--spec",       type=str,  default=_DEFAULT_SPEC,
                        help="path to serviceSpec JSON (default: serviceSpec_mul_8node.json)")
    args = parser.parse_args()
    run_pretrain(n_episodes=args.episodes,
                 max_agents=args.max_agents,
                 seed=args.seed,
                 spec_file=args.spec)
