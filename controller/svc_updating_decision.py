"""
SUD – Service Updating Decision-making (Q-learning)
=====================================================
對應論文 Algorithm 3 (PSS) + Algorithm 4 (SUD)。

設計原則（方案 A）：
  subscribe / node_recovery → SUD episode（ADD replica）
  unsubscribe / node_failure → 由 trigger_solver() 呼叫 USC-TS 重解，不經此模組

State  z_τ  : frozenset，本 episode 中「已被指派新 replica 的服務集合」
               從 {} 逐步增長，terminal 時為 USmk 的子集。
               State space = 2^k（k=3 服務 → 最多 8 種）

Action a_τ  : (service, node) — 把服務的新 replica 加到指定節點
               只來自 PSS 篩選出的候選服務 × 合法節點

Reward r_τ  : nsats(新部署) − nsats(舊部署)

Q-table 更新：
  Q[z][a] ← Q[z][a] + μ × (r + σ × max_a' Q[z∪{svc}][a'] − Q[z][a])
"""

import json
import logging
import os
import random
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ── 超參數 ─────────────────────────────────────────────────────────────────
ALPHA      = 0.1    # μ 學習率
GAMMA      = 0.9    # σ 折扣因子
EPS_START  = 0.5    # 初始探索率
EPS_MIN    = 0.05   # 最低探索率
EPS_DECAY  = 0.995  # 每次 episode 後的衰減係數

_Q_TABLE_FILE = os.path.join(os.path.dirname(__file__), "information", "q_table.json")


# ── Q-table 持久化 ──────────────────────────────────────────────────────────

def _load_q_table() -> dict:
    if os.path.exists(_Q_TABLE_FILE):
        try:
            with open(_Q_TABLE_FILE) as f:
                return json.load(f)
        except Exception as e:
            logger.warning("Failed to load Q-table: %s", e)
    return {}


def _save_q_table(q_table: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_Q_TABLE_FILE), exist_ok=True)
        with open(_Q_TABLE_FILE, "w") as f:
            json.dump(q_table, f)
    except Exception as e:
        logger.warning("Failed to save Q-table: %s", e)


# ── SUDAgent ────────────────────────────────────────────────────────────────

class SUDAgent:
    """
    Online Q-learning agent for Service Updating Decision-making.

    使用方式（整合至 trigger_solver() EMS 路徑）：
        agent = SUDAgent.get_instance()
        new_deployment = agent.decide(current_deployment, subscriptions,
                                      nodes, services, specs)
    """

    _instance: Optional["SUDAgent"] = None

    def __init__(self) -> None:
        self.q_table: dict = _load_q_table()
        self.epsilon: float = EPS_START
        self.event_count: int = 0
        # True after first cold-start USC-TS run; prevents repeated cold-start loops
        self.initialized: bool = bool(self.q_table)
        logger.info("SUDAgent initialised. Q-table entries: %d, ε=%.3f, initialized=%s",
                    len(self.q_table), self.epsilon, self.initialized)

    @classmethod
    def get_instance(cls) -> "SUDAgent":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """清除 singleton（測試用）。"""
        cls._instance = None

    # ── 公開 API ─────────────────────────────────────────────────────────────

    def decide(self,
               current_deployment: Dict[str, List[str]],
               subscriptions: list,
               nodes: List[str],
               services: List[str],
               specs: dict,
               dry_run: bool = False) -> Dict[str, List[str]]:
        """
        執行一個完整的 SUD episode（對應論文 Algorithm 4）。

        dry_run=True 時（准入控制模擬）跳過 Q-table 更新，直接以 greedy 推論。
        """
        # ── Step 1：PSS（Algorithm 3）────────────────────────────────────
        us, f = self._pss(current_deployment, subscriptions, nodes, services, specs)

        if not us:
            logger.debug("SUD: PSS found no candidate services, keep current deployment")
            return current_deployment

        logger.info("SUD: PSS candidates=%s", us)

        # ── Step 2：SUD episode（Algorithm 4）────────────────────────────
        working = {n: set(svcs) for n, svcs in current_deployment.items()}
        z: FrozenSet[str] = frozenset()
        old_nsats = self._simulate_nsats(working, subscriptions, specs)

        while True:
            remaining = us - z
            if not remaining:
                break

            valid_actions = [
                (svc, node)
                for svc in remaining
                for node in f.get(svc, [])
                if svc not in working.get(node, set())
            ]
            if not valid_actions:
                break

            state_key = self._state_key(z)

            # ε-greedy（論文 lines 12–15）
            if random.random() < self.epsilon or state_key not in self.q_table:
                action = random.choice(valid_actions)
            else:
                q_vals = self.q_table[state_key]
                action = max(valid_actions, key=lambda a: q_vals.get(str(a), 0.0))

            svc, node = action
            working[node].add(svc)

            new_nsats = self._simulate_nsats(working, subscriptions, specs)
            reward = new_nsats - old_nsats

            z_next = z | frozenset([svc])

            if not dry_run:
                self._update_q(state_key, str(action),
                               reward, self._state_key(z_next))

            z = z_next
            old_nsats = new_nsats
            logger.debug("SUD step: action=%s reward=%d nsats=%d",
                         action, reward, new_nsats)

        # ── Step 3：episode 結束，更新 ε ─────────────────────────────────
        if not dry_run:
            self.epsilon = max(EPS_MIN, self.epsilon * EPS_DECAY)
            self.event_count += 1
            if self.event_count % 50 == 0:
                _save_q_table(self.q_table)
                logger.info("SUD Q-table saved (%d entries, ε=%.3f)",
                            len(self.q_table), self.epsilon)

        result = {n: sorted(svcs) for n, svcs in working.items()}
        logger.info("SUD episode done: assigned=%s final_deployment=%s",
                    sorted(z), result)
        return result

    # ── Algorithm 3：PSS ─────────────────────────────────────────────────────

    def _pss(self,
             deployment: Dict[str, List[str]],
             subscriptions: list,
             nodes: List[str],
             services: List[str],
             specs: dict) -> Tuple[Set[str], Dict[str, List[str]]]:
        """
        Primary Services Selection。

        找出「需求 × f_limit > 目前總容量」的服務（對應論文 Δ_sj_mk > 0），
        並為每個候選服務找出合法的可部署節點（對應 F(USmk)）。

        Returns
        -------
        us : 需要擴展的服務集合（USmk）
        f  : {service: [valid_nodes]}（F(USmk)）
        """
        from usc_ts_solver import _get_node_combo_capacity, _svc_combo_key

        demand = self._compute_demand(subscriptions)
        combo_cap = _get_node_combo_capacity(specs, deployment)
        f_l = {s: specs["services"][s]["frequencyLimit"][1] for s in services}
        work_ability = specs.get("workAbility", {})

        us: Set[str] = set()
        f: Dict[str, List[str]] = {}

        for svc in services:
            d = demand.get(svc, 0)
            if d == 0:
                continue

            total_cap = sum(
                combo_cap.get((n, svc), 0.0)
                for n in nodes
                if svc in deployment.get(n, [])
            )

            if d * f_l[svc] > total_cap:
                valid_nodes = []
                for n in nodes:
                    if svc in deployment.get(n, []):
                        continue
                    new_combo = _svc_combo_key(set(deployment.get(n, [])) | {svc}, specs)
                    if new_combo in work_ability.get(n, {}):
                        valid_nodes.append(n)
                if valid_nodes:
                    us.add(svc)
                    f[svc] = valid_nodes

        return us, f

    # ── Q-table 操作 ──────────────────────────────────────────────────────────

    def _update_q(self, state_key: str, action_key: str,
                  reward: float, next_state_key: str) -> None:
        if state_key not in self.q_table:
            self.q_table[state_key] = {}
        old_q = self.q_table[state_key].get(action_key, 0.0)
        next_max = max(self.q_table.get(next_state_key, {}).values(), default=0.0)
        self.q_table[state_key][action_key] = (
            old_q + ALPHA * (reward + GAMMA * next_max - old_q)
        )

    # ── nsats 模擬 ────────────────────────────────────────────────────────────

    def _simulate_nsats(self,
                        deployment: Dict[str, set],
                        subscriptions: list,
                        specs: dict) -> int:
        """
        不啟動 Pod，利用 workAbility combo capacity 模擬 nsats。
        使用 nsats_first greedy：逐一嘗試讓每個 agent 的所有服務都達到 f_limit。
        """
        from usc_ts_solver import _get_node_combo_capacity

        dep_list = {n: sorted(svcs) for n, svcs in deployment.items()}
        combo_cap = _get_node_combo_capacity(specs, dep_list)
        f_l = {s: specs["services"][s]["frequencyLimit"][1]
               for s in specs.get("services", {})}

        remaining = dict(combo_cap)
        nsats = 0
        for sub in subscriptions:
            req_svcs = [e["serviceType"] for e in sub.get("subscriptions", [])]
            tentative: List[Tuple[str, str, float]] = []
            ok = True
            for s in req_svcs:
                fl = f_l.get(s, 0.0)
                best_node, best_cap = None, -1.0
                for (node, svc), c in remaining.items():
                    if svc == s and c >= fl and c > best_cap:
                        best_node, best_cap = node, c
                if best_node is None:
                    ok = False
                    break
                tentative.append((best_node, s, fl))
            if ok:
                nsats += 1
                for node, s, fl in tentative:
                    remaining[(node, s)] -= fl

        return nsats

    # ── 工具 ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _state_key(z: FrozenSet[str]) -> str:
        """frozenset → 穩定字串 key，對應論文的 z_τ。"""
        return ",".join(sorted(z)) if z else "(empty)"

    @staticmethod
    def _compute_demand(subscriptions: list) -> Dict[str, int]:
        demand: Dict[str, int] = {}
        for sub in subscriptions:
            for e in sub.get("subscriptions", []):
                s = e["serviceType"]
                demand[s] = demand.get(s, 0) + 1
        return demand
