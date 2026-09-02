# GA_HAMMING.py
# (基於你的 ga4combo_hamming.py)
#
# 新增功能：
# - 新的 SLOT 分配器：allocate_x_slots()
# - 時間分解：分配 vs 評分 vs GA 外層開銷
# - 可插拔分配器，透過 run_ga(allocator="nsats_first"|"slots"|...)

import json
import random
import csv
import time
import logging
from itertools import product
from pathlib import Path
from typing import Dict, List, Tuple, Optional

# ---------- Logger 設定 ----------

logger = logging.getLogger("GA_HAMMING")
logger.setLevel(logging.DEBUG)

# 控制台 handler（INFO 以上）
_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.INFO)
_console_formatter = logging.Formatter(
    "[%(asctime)s %(levelname)s] %(message)s", datefmt="%H:%M:%S"
)
_console_handler.setFormatter(_console_formatter)
logger.addHandler(_console_handler)

# 檔案 handler（DEBUG 以上，詳細記錄）
_file_handler = logging.FileHandler("ga_hamming.log", mode="w", encoding="utf-8")
_file_handler.setLevel(logging.DEBUG)
_file_formatter = logging.Formatter(
    "[%(asctime)s %(levelname)s %(funcName)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)
_file_handler.setFormatter(_file_formatter)
logger.addHandler(_file_handler)


# ---------- 計時統計 ----------

class TimingStats:
    """累積一次 GA 執行的時間分解。"""
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.eval_calls: int = 0
        self.alloc_time_s: float = 0.0
        self.score_time_s: float = 0.0

    @property
    def eval_time_s(self) -> float:
        return self.alloc_time_s + self.score_time_s


TIMING = TimingStats()


# ---------- 工具函式 ----------

def load_model(path: str = "system_model.json") -> dict:
    """從檔案讀取系統模型 JSON。"""
    logger.info("載入系統模型: %s", path)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    logger.info("模型載入完成 — 服務=%d, 節點=%d, Agent=%d",
                len(data.get("services", [])),
                len(data.get("nodes", [])),
                len(data.get("agents", [])))
    return data


def decode_mask(mask: int, services: List[str]) -> List[str]:
    """將位元遮罩轉回服務名稱列表。"""
    res = []
    for i, s in enumerate(services):
        if mask & (1 << i):
            res.append(s)
    return res


def gene_to_tuple(gene):
    """將染色體 (List[int]) 轉為可雜湊的 tuple，用於 set/dict。"""
    return tuple(gene)


def count_unique_genes(pop):
    """計數族群中的獨特基因數量。pop: List[Chromosome]"""
    seen = set()
    for g in pop:
        seen.add(gene_to_tuple(g))
    return len(seen)


# ---------- GA 環境初始化，包含資料結構 ----------

class GAEnv:
    """GA 執行環境，預先計算各種優化資料。"""
    def __init__(self, model: dict):
        logger.debug("初始化 GAEnv ...")
        self.model = model
        self.services: List[str] = model["services"]
        self.nodes: List[str] = model["nodes"]
        self.agents: List[str] = model["agents"]
        self.req: Dict[str, List[str]] = model["requirements"]
        self.legal_masks: Dict[str, Dict[str, Dict[str, int]]] = model["legal_masks"]
        self.f_l: Dict[str, float] = model["f_l"]  # 下限 (最低 QoS)
        self.f_h: Dict[str, float] = model["f_h"]  # 上限 (最高 QoS)

        # 為服務建立索引對應表，方便位元操作
        self.svc2idx = {s: i for i, s in enumerate(self.services)}

        # 預先將每個節點的合法遮罩列表轉為 int 版本
        self.legal_masks_int: Dict[str, List[int]] = {}
        for m in self.nodes:
            self.legal_masks_int[m] = [int(k) for k in self.legal_masks[m].keys()]

        # ---------- 優化預計算 ----------
        # 1. 按難度排序 agent（需求服務數量和總成本）
        def agent_key(g):
            req_svcs = self.req[g]
            return (len(req_svcs), sum(self.f_l[s] for s in req_svcs))
        self.agents_sorted = sorted(self.agents, key=agent_key)

        # 2. 每個 agent 的 Q 貢獻（基於 f_l 下限的基礎 Q）
        self.agent_qs = {}
        for a in self.agents:
            val = 0.0
            for s in self.req[a]:
                if self.f_h[s] > 0:
                    val += self.f_l[s] / self.f_h[s]
            self.agent_qs[a] = val

        # 3. 請求索引和值，用於快速迭代
        #    req_idxs[agent] = list of (service_index, f_l_value)
        self.req_idxs = {}
        for a in self.agents:
            self.req_idxs[a] = [(self.svc2idx[s], self.f_l[s]) for s in self.req[a]]

        # 4. 服務 f_l 和 f_h 列表，用於索引存取
        self.fl_list = [self.f_l[s] for s in self.services]
        self.fh_list = [self.f_h[s] for s in self.services]
        
        # 5. 預計算所有 (node, mask) 組合的容量
        #    all_caps[node_idx][mask_int] = [cap_s0, cap_s1, ...]
        self.all_caps = []
        for n in self.nodes:
            node_map = {}
            for mask_int in self.legal_masks_int[n]:
                mask_str = str(mask_int)
                legal_entry = self.legal_masks[n].get(mask_str, {})
                caps_list = []
                for s_idx, s in enumerate(self.services):
                    if mask_int & (1 << s_idx):
                        c = float(legal_entry.get(s, 0.0))
                    else:
                        c = 0.0
                    caps_list.append(c)
                node_map[mask_int] = caps_list
            self.all_caps.append(node_map)

        # 6. 每個節點的遮罩評分（容量總和），用於快速排名
        self.mask_scores: List[Dict[int, float]] = []
        for node_idx, node in enumerate(self.nodes):
            scores: Dict[int, float] = {}
            caps_map = self.all_caps[node_idx]
            for mask_int, caps in caps_map.items():
                scores[mask_int] = sum(caps)
            self.mask_scores.append(scores)

        logger.debug("GAEnv 初始化完成: services=%d, nodes=%d, agents=%d",
                     len(self.services), len(self.nodes), len(self.agents))

    def is_legal_mask(self, node: str, mask: int) -> bool:
        """檢查遮罩在該節點是否合法。"""
        return str(mask) in self.legal_masks[node]

    def get_capacity(self, node: str, mask: int, service: str) -> float:
        """取得 W(i, mask, j)，沒有則回傳 0"""
        return float(self.legal_masks[node].get(str(mask), {}).get(service, 0.0))


# ---------- 染色體表示與 GA 運算子 ----------

Chromosome = List[int]  # gene[i] = 第 i 個節點的遮罩 (int)


def random_chromosome(env: GAEnv) -> Chromosome:
    """隨機生成一條合法染色體。"""
    gene = []
    for node in env.nodes:
        masks = env.legal_masks_int[node]
        gene.append(random.choice(masks))
    return gene


def init_population(env: GAEnv, pop_size: int, initial_gene: Optional[Chromosome] = None) -> List[Chromosome]:
    """初始化族群。若是傳入了舊有的部署配置 (Warm Start)，便會將其直接加入族群，並填補其他隨機解。"""
    pop = []
    if initial_gene is not None:
        pop.append(initial_gene[:])
        
    while len(pop) < pop_size:
        pop.append(random_chromosome(env))
        
    return pop


def build_capacity(env: GAEnv, gene: Chromosome) -> Dict[Tuple[str, str], float]:
    """
    根據染色體 gene，建立 cap[(node, service)] 容量字典。
    """
    cap: Dict[Tuple[str, str], float] = {}
    for node_idx, node in enumerate(env.nodes):
        mask = gene[node_idx]
        for s in env.services:
            if mask & (1 << env.svc2idx[s]):
                cap[(node, s)] = env.get_capacity(node, mask, s)
            else:
                cap[(node, s)] = 0.0
    return cap


# ---------- 分配策略（內層） ----------

def allocate_x_nsats_first(env: GAEnv, gene: Chromosome) -> Dict[Tuple[str, str, str], float]:
    """
    基準分配器（你的原始版本）：
    1) 優先最大化 N_sat：逐一嘗試讓每個 agent 的所有需求服務都至少達到 f_l
       - 若某 agent 任何一個服務無法達 f_l，則回滾該 agent 的配置（不浪費容量）
    2) 本版先不進行 f_h 加值
    回傳：x[(agent,node,service)] = freq
    """
    cap = build_capacity(env, gene)  # 剩餘容量 cap[(node, service)]
    x: Dict[Tuple[str, str, str], float] = {}

    def agent_key(g: str):
        req_svcs = env.req[g]
        return (len(req_svcs), sum(env.f_l[s] for s in req_svcs))

    agents_order = sorted(env.agents, key=agent_key)

    for agent in agents_order:
        req_svcs = env.req[agent]
        tentative: List[Tuple[str, str, float]] = []  # (node, service, freq)

        ok = True
        for s in req_svcs:
            need = float(env.f_l[s])
            # 找容量足夠的節點，選最大剩餘容量的
            candidates = [(n, cap[(n, s)]) for n in env.nodes if cap[(n, s)] >= need]
            if not candidates:
                ok = False
                break
            best_node, _ = max(candidates, key=lambda t: t[1])
            tentative.append((best_node, s, need))

        if not ok:
            continue

        # 確認可行後再提交所有指派
        for node, s, freq in tentative:
            x[(agent, node, s)] = x.get((agent, node, s), 0.0) + freq
            cap[(node, s)] -= freq

    return x


def allocate_x_slots(env: GAEnv, gene: Chromosome) -> Dict[Tuple[str, str, str], float]:
    """
    新的 SLOT 分配器（綜合效能基線）。

    概念（第 1 階段，最大化 N_sat）：
    - 將每個 (node,service) 容量轉為整數「插槽」：
        slots(node, s) = floor(cap(node,s) / f_l[s])
      每個插槽可以完全滿足一個 agent 在該服務上的最低需求。
    - 按順序迭代 agent（簡單優先），為每個需求服務選擇具有：
        slots(node,s) > 0 且剩餘容量 >= f_l[s] 的節點
      （平手時按剩餘容量排名）。
    - 只有當 agent 的所有服務都可滿足時才提交；否則回滾。
    """
    cap = build_capacity(env, gene)  # 剩餘容量
    cap_rem = dict(cap)
    x: Dict[Tuple[str, str, str], float] = {}

    # 計算最低 QoS 的插槽數
    slots: Dict[Tuple[str, str], int] = {}
    for n in env.nodes:
        for s in env.services:
            fl = float(env.f_l[s])
            if fl <= 0:
                slots[(n, s)] = 0
            else:
                slots[(n, s)] = int(cap_rem[(n, s)] // fl)

    # 優化：預先列出有插槽的節點（初始插槽 > 0）
    nodes_for_s: Dict[str, List[str]] = {
        s: [n for n in env.nodes if slots[(n, s)] > 0] for s in env.services
    }

    def agent_key(g: str):
        req_svcs = env.req[g]
        return (len(req_svcs), sum(env.f_l[s] for s in req_svcs))

    agents_order = sorted(env.agents, key=agent_key)

    for agent in agents_order:
        chosen: Dict[str, str] = {}
        ok = True

        # 為每個需求服務選擇一個節點
        for s in env.req[agent]:
            need = float(env.f_l[s])
            best_node: Optional[str] = None
            best_cap: float = -1.0

            for n in nodes_for_s.get(s, []):
                if slots[(n, s)] <= 0:
                    continue
                if cap_rem[(n, s)] < need:
                    continue
                if cap_rem[(n, s)] > best_cap:
                    best_cap = cap_rem[(n, s)]
                    best_node = n

            if best_node is None:
                ok = False
                break
            chosen[s] = best_node

        if not ok:
            continue

        # 提交指派
        for s, n in chosen.items():
            need = float(env.f_l[s])
            x[(agent, n, s)] = need
            cap_rem[(n, s)] -= need
            # 消耗一個插槽
            slots[(n, s)] -= 1

    return x


def allocate_x_slots_full_opt(env: GAEnv, gene: Chromosome) -> Tuple[int, float]:
    """
    完整 SLOT 分配器（第 1 階段 + 第 2 階段充值），已優化。
    回傳 (N_sat, Q_full)。

    第 1 階段：同 allocate_x_slots_opt（分配恰好 f_l，最大化 N_sat）。
    第 2 階段：使用剩餘容量充值至 f_h，計算額外 Q。
             僅使用 (node,service) 指派計數。
    """
    n_nodes = len(env.nodes)
    n_services = len(env.services)

    # 1) 建立容量
    current_caps = []
    for i in range(n_nodes):
        current_caps.append(list(env.all_caps[i][gene[i]]))

    # 2) 建立插槽表和 nodes_for_s
    slots = []
    nodes_for_s = [[] for _ in range(n_services)]
    fl_list = env.fl_list

    for n_idx in range(n_nodes):
        node_slots = []
        caps = current_caps[n_idx]
        for s_idx in range(n_services):
            fl = fl_list[s_idx]
            if fl > 0:
                s_val = int(caps[s_idx] // fl)
            else:
                s_val = 0
            node_slots.append(s_val)
            if s_val > 0:
                nodes_for_s[s_idx].append(n_idx)
        slots.append(node_slots)

    # 3) 第 1 階段：分配 f_l（同 slots_opt 策略）
    assigned_cnt = [[0]*n_services for _ in range(n_nodes)]  # (node,service) 的滿足邊數
    N_sat = 0
    Q = 0.0

    for agent in env.agents_sorted:
        reqs = env.req_idxs[agent]  # list of (s_idx, fl)
        assignments = []            # (n_idx, s_idx, fl)
        possible = True

        for s_idx, fl in reqs:
            best_n = -1
            best_cap = -1.0

            for n_idx in nodes_for_s[s_idx]:
                if slots[n_idx][s_idx] <= 0:
                    continue
                c = current_caps[n_idx][s_idx]
                if c < fl:
                    continue
                if c > best_cap:
                    best_cap = c
                    best_n = n_idx

            if best_n == -1:
                possible = False
                break

            assignments.append((best_n, s_idx, fl))

        if possible:
            N_sat += 1
            Q += env.agent_qs[agent]  # 基礎 Q（來自 f_l）
            for n_idx, s_idx, fl in assignments:
                current_caps[n_idx][s_idx] -= fl
                slots[n_idx][s_idx] -= 1
                assigned_cnt[n_idx][s_idx] += 1

    # 4) 第 2 階段：充值至 f_h（快速聚合公式）
    fh_list = env.fh_list
    for s_idx in range(n_services):
        fh = fh_list[s_idx]
        fl = fl_list[s_idx]
        if fh <= 0:
            continue
        delta = fh - fl
        if delta <= 0:
            continue

        for n_idx in range(n_nodes):
            k = assigned_cnt[n_idx][s_idx]
            if k <= 0:
                continue
            rem = current_caps[n_idx][s_idx]
            if rem <= 0:
                continue
            # 該 (node,service) 可使用的總額外容量
            extra_used = rem if rem < k * delta else (k * delta)
            Q += extra_used / fh
            current_caps[n_idx][s_idx] -= extra_used

    return N_sat, Q


def allocate_x_nsats_first_opt(env: GAEnv, gene: Chromosome) -> Tuple[int, float]:
    """
    allocate_x_nsats_first 的優化版本。
    直接回傳 (N_sat, Q)，不建立 x 字典。
    """
    # 快速存取變數
    n_nodes = len(env.nodes)
    
    # 1. 建立當前容量（可變副本）
    #    current_caps[node_idx][svc_idx] = float
    current_caps = []
    for i in range(n_nodes):
        current_caps.append(list(env.all_caps[i][gene[i]]))

    N_sat = 0
    Q = 0.0

    # 2. 迭代 agent（已pre-sorted）
    for agent in env.agents_sorted:
        reqs = env.req_idxs[agent]  # list of (s_idx, fl)
        
        # 為所有服務找到指派
        # 暫時儲存到列表：(node_idx, s_idx, fl)
        assignments = []
        possible = True
        
        for s_idx, fl in reqs:
            best_n = -1
            best_cap = -1.0
            
            # 找最適節點（容量最多）
            for n_idx in range(n_nodes):
                c = current_caps[n_idx][s_idx]
                if c >= fl:
                    if c > best_cap:
                        best_cap = c
                        best_n = n_idx
            
            if best_n == -1:
                possible = False
                break
            
            assignments.append((best_n, s_idx, fl))
            
        if possible:
            N_sat += 1
            Q += env.agent_qs[agent]
            # 提交
            for n_idx, s_idx, fl in assignments:
                current_caps[n_idx][s_idx] -= fl

    return N_sat, Q


def allocate_x_slots_opt(env: GAEnv, gene: Chromosome) -> Tuple[int, float]:
    """
    allocate_x_slots 的優化版本。
    直接回傳 (N_sat, Q)。
    """
    n_nodes = len(env.nodes)
    n_services = len(env.services)
    
    # 1. 建立容量
    current_caps = []
    for i in range(n_nodes):
        current_caps.append(list(env.all_caps[i][gene[i]]))

    # 2. 建立插槽表 [node_idx][svc_idx]
    #    邏輯：int(cap // fl) if fl > 0 else 0
    #    同時預計算有插槽的節點以提速
    
    slots = [] 
    # 預計算每個服務有插槽的節點，避免之後掃描所有節點
    nodes_for_s = [[] for _ in range(n_services)]
    
    fl_list = env.fl_list
    
    for n_idx in range(n_nodes):
        node_slots = []
        caps = current_caps[n_idx]
        for s_idx in range(n_services):
            fl = fl_list[s_idx]
            if fl > 0:
                s_val = int(caps[s_idx] // fl)
            else:
                s_val = 0
            node_slots.append(s_val)
            if s_val > 0:
                nodes_for_s[s_idx].append(n_idx)
        slots.append(node_slots)

    N_sat = 0
    Q = 0.0
    
    for agent in env.agents_sorted:
        reqs = env.req_idxs[agent]
        
        assignments = [] # (n_idx, s_idx, fl)
        possible = True
        
        for s_idx, fl in reqs:
            best_n = -1
            best_cap = -1.0
            
            # 只檢查初始有插槽的節點
            candidate_nodes = nodes_for_s[s_idx]
            
            for n_idx in candidate_nodes:
                if slots[n_idx][s_idx] <= 0:
                    continue
                c = current_caps[n_idx][s_idx]
                if c < fl:
                    continue
                    
                if c > best_cap:
                    best_cap = c
                    best_n = n_idx
            
            if best_n == -1:
                possible = False
                break
                
            assignments.append((best_n, s_idx, fl))
            
        if possible:
            N_sat += 1
            Q += env.agent_qs[agent]
            # 提交
            for n_idx, s_idx, fl in assignments:
                current_caps[n_idx][s_idx] -= fl
                slots[n_idx][s_idx] -= 1
    
    return N_sat, Q


def allocate_x_nsats_plus(env: GAEnv, gene: Chromosome) -> Tuple[int, float]:
    """
    混合分配器：
    1. 使用 'nsats_first' 的精確扣除邏輯（最大化 N_sat）
    2. 加入 'slots_full' 的事後充值邏輯（最大化 Q）

    Q 為正規化平均（2026-07-18 修正，同 score_from_x 的理由）：除以總配對數，
    不影響 GA 內部比較的相對排序（同一個 env 下配對數是常數）。
    """
    n_nodes = len(env.nodes)
    n_services = len(env.services)

    # 1) 複製當前容量
    current_caps = []
    for i in range(n_nodes):
        current_caps.append(list(env.all_caps[i][gene[i]]))

    # 用來記錄每個 (node, service) 被指派次數，第 2 階段充值用
    assigned_counts = [[0] * n_services for _ in range(n_nodes)]

    N_sat = 0
    Q = 0.0

    # 第 1 階段：貪婪滿足 N_sat（同 nsats_first）
    for agent in env.agents_sorted:
        reqs = env.req_idxs[agent]
        assignments = []
        possible = True
        
        for s_idx, fl in reqs:
            best_n = -1
            best_cap = -1.0
            
            # 找容量足夠且最大的節點
            for n_idx in range(n_nodes):
                c = current_caps[n_idx][s_idx]
                if c >= fl:
                    if c > best_cap:
                        best_cap = c
                        best_n = n_idx
            
            if best_n == -1:
                possible = False
                break
            
            assignments.append((best_n, s_idx, fl))
            
        if possible:
            N_sat += 1
            Q += env.agent_qs[agent]  # 加入基礎 Q（f_l 部分）
            # 實際扣除容量
            for n_idx, s_idx, fl in assignments:
                current_caps[n_idx][s_idx] -= fl
                assigned_counts[n_idx][s_idx] += 1

    # 第 2 階段：充值（同 slots_full 的優化邏輯）
    fl_list = env.fl_list
    fh_list = env.fh_list
    
    for s_idx in range(n_services):
        fh = fh_list[s_idx]
        fl = fl_list[s_idx]
        if fh <= 0: 
            continue
        
        delta = fh - fl
        if delta <= 0: 
            continue  # f_h == f_l，無額外收益
        
        for n_idx in range(n_nodes):
            k = assigned_counts[n_idx][s_idx]
            if k <= 0: 
                continue
            
            rem = current_caps[n_idx][s_idx]
            if rem <= 0: 
                continue
            
            # 該節點剩餘容量能提供的額外 Q
            # 最多補到 k * (f_h - f_l)
            extra_used = rem if rem < k * delta else (k * delta)
            Q += extra_used / fh
            # current_caps[n_idx][s_idx] -= extra_used # 若後續還有階段才需要扣

    total_pairs = sum(len(env.req_idxs[agent]) for agent in env.agents)
    Q = Q / total_pairs if total_pairs > 0 else 0.0

    return N_sat, Q


def allocate_x_nsats_dict(env: GAEnv, gene: Chromosome) -> Dict[Tuple[str, str, str], float]:
    """
    Best-Fit dict allocator: Phase 1 mirrors allocate_x_nsats_plus (select max-capacity node >= f_l),
    Phase 2 fair-shares total capacity among assigned agents. Used by ga_bf mode.
    """
    n_nodes = len(env.nodes)
    n_services = len(env.services)

    current_caps = [list(env.all_caps[i][gene[i]]) for i in range(n_nodes)]

    x: Dict[Tuple[str, str, str], float] = {}
    assigned_agents = [[[] for _ in range(n_services)] for _ in range(n_nodes)]

    for agent in env.agents_sorted:
        reqs = env.req_idxs[agent]
        assignments = []
        possible = True

        for s_idx, fl in reqs:
            best_n = -1
            best_cap = -1.0
            for n_idx in range(n_nodes):
                c = current_caps[n_idx][s_idx]
                if c >= fl and c > best_cap:
                    best_cap = c
                    best_n = n_idx

            if best_n == -1:
                possible = False
                break

            assignments.append((best_n, s_idx, fl))

        if possible:
            for n_idx, s_idx, fl in assignments:
                current_caps[n_idx][s_idx] -= fl
                assigned_agents[n_idx][s_idx].append(agent)

    fh_list = env.fh_list
    for s_idx in range(n_services):
        fh = fh_list[s_idx]
        if fh <= 0:
            continue
        for n_idx in range(n_nodes):
            agents_here = assigned_agents[n_idx][s_idx]
            k = len(agents_here)
            if k <= 0:
                continue
            node_name = env.nodes[n_idx]
            s_name = env.services[s_idx]
            total = env.get_capacity(node_name, gene[n_idx], s_name)

            avg_freq = min(fh, total // k)
            for a in agents_here:
                x[(a, node_name, s_name)] = float(avg_freq)

    return x


def allocate_x_optimize_dict(env: GAEnv, gene: Chromosome,
                              current_routing: Optional[Dict[Tuple[str, int], str]] = None
                              ) -> Dict[Tuple[str, str, str], float]:
    """
    Mirrors optimizer.py optimize(): Phase 1 Best-Fit at f_h (deduct f_h per agent),
    Phase 2 overflow to max-predFreq node (only nodes whose post-add fair-share would
    stay >= f_l are considered, so overflow agents never drag a bucket below f_l).
    Final freq = fair-share total // k capped at f_h.
    Used by ga_bf mode as the final routing allocator.

    current_routing: 選填，{(agent, service_idx): node_name}，來自上一輪已生效的路由
    （不涉及本次 GA 演化探索時對候選 gene 的 evaluate() 呼叫，只用於最終那次真正落地的分配）。
    Phase 1 幫每個 agent 找節點前，先試這個「原節點」在本次容量表下是否仍裝得下 f_h，
    裝得下就直接沿用、不進入 best-fit 搜尋——避免一個不相干 agent 的加入/離開，
    透過 best-fit 的容量狀態依賴，連帶把其他 agent 的路由洗牌掉。
    """
    n_nodes = len(env.nodes)
    n_services = len(env.services)

    current_caps = [list(env.all_caps[i][gene[i]]) for i in range(n_nodes)]
    fh_list = env.fh_list
    current_routing = current_routing or {}
    node_idx = {name: i for i, name in enumerate(env.nodes)}

    x: Dict[Tuple[str, str, str], float] = {}
    assigned_agents = [[[] for _ in range(n_services)] for _ in range(n_nodes)]

    # Phase 1: Best-Fit at f_h — pick max-remaining node with cap >= f_h, deduct f_h
    unrouted = []
    for agent in env.agents_sorted:
        reqs = env.req_idxs[agent]
        assignments = []
        possible = True

        for s_idx, _ in reqs:
            fh = fh_list[s_idx]

            # 路由穩定性：原節點容量仍夠就沿用，不重新 best-fit 搜尋
            incumbent_n = node_idx.get(current_routing.get((agent, s_idx)))
            if incumbent_n is not None and current_caps[incumbent_n][s_idx] >= fh:
                assignments.append((incumbent_n, s_idx, fh))
                continue

            best_n, best_cap = -1, -1.0
            for n_idx in range(n_nodes):
                c = current_caps[n_idx][s_idx]
                if c >= fh and c > best_cap:
                    best_cap = c
                    best_n = n_idx

            if best_n == -1:
                possible = False
                break
            assignments.append((best_n, s_idx, fh))

        if possible:
            for n_idx, s_idx, fh in assignments:
                current_caps[n_idx][s_idx] -= fh
                assigned_agents[n_idx][s_idx].append(agent)
        else:
            unrouted.append(agent)

    # Phase 2: overflow — assign to node with max predFreq = total/(k+1)，
    # 但只考慮加入後 fair-share 仍 >= f_l 的節點，避免把整桶（含 Phase 1 已放置的 agent）
    # 一起拖到 f_l 以下；找不到就視為無法服務（possible=False），不勉強塞入
    for agent in unrouted:
        reqs = env.req_idxs[agent]
        assignments = []
        possible = True

        for s_idx, fl in reqs:
            best_n, best_pred = -1, -1.0
            for n_idx in range(n_nodes):
                node_name = env.nodes[n_idx]
                s_name = env.services[s_idx]
                total = env.get_capacity(node_name, gene[n_idx], s_name)
                if total <= 0:
                    continue
                k = len(assigned_agents[n_idx][s_idx])
                pred = total / (k + 1)
                if pred < fl:
                    continue
                if pred > best_pred:
                    best_pred = pred
                    best_n = n_idx

            if best_n == -1:
                possible = False
                break
            assignments.append((best_n, s_idx))

        if possible:
            for n_idx, s_idx in assignments:
                assigned_agents[n_idx][s_idx].append(agent)

    # Final freq: fair-share total // k, capped at f_h
    for s_idx in range(n_services):
        fh = fh_list[s_idx]
        if fh <= 0:
            continue
        for n_idx in range(n_nodes):
            agents_here = assigned_agents[n_idx][s_idx]
            k = len(agents_here)
            if k == 0:
                continue
            node_name = env.nodes[n_idx]
            s_name = env.services[s_idx]
            total = env.get_capacity(node_name, gene[n_idx], s_name)
            avg_freq = min(fh, total // k)
            for a in agents_here:
                x[(a, node_name, s_name)] = float(avg_freq)

    return x


def allocate_x_packed_dict(env: GAEnv, gene: Chromosome) -> Dict[Tuple[str, str, str], float]:
    """
    回傳完整指派矩陣 `x` 的 Packing 分配器。
    包含 Phase 1 (N_sat, First-Fit) 與 Phase 2 (充值)，用於最終部屬與剪枝。
    """
    n_nodes = len(env.nodes)
    n_services = len(env.services)

    current_caps = []
    for i in range(n_nodes):
        current_caps.append(list(env.all_caps[i][gene[i]]))

    x: Dict[Tuple[str, str, str], float] = {}
    assigned_agents = [[[] for _ in range(n_services)] for _ in range(n_nodes)]

    # Phase 1: First-Fit 貪婪滿足 N_sat (以 f_h 作為單個 Agent 的標準資源消耗)
    # 策略：先嘗試按「標準頻率 f_h」塞滿服務實例，這會觸發部署剪枝（因為連線會很集中）
    for agent in env.agents_sorted:
        reqs = env.req_idxs[agent]
        assignments = []
        possible = True
        
        for s_idx, fl in reqs:
            fh = env.fh_list[s_idx] # 標準頻率
            best_n = -1
            # First-Fit: 找到第一個能塞下 fh 的節點
            for n_idx in range(n_nodes):
                if current_caps[n_idx][s_idx] >= fh:
                    best_n = n_idx
                    break
            
            # 若所有節點都塞不下 fh，則退而求其次，尋找能塞下 fl 的節點
            # 這保證了連線數量優先 (N_sat priority)
            if best_n == -1:
                for n_idx in range(n_nodes):
                    if current_caps[n_idx][s_idx] >= fl:
                        best_n = n_idx
                        break
            
            if best_n == -1:
                possible = False
                break
            
            # 注意：這裡我們先扣除真實可能分配到的量，稍後再 Phase 2 精確修正
            # 這裡暫時以 fl 作為保底扣除
            assignments.append((best_n, s_idx, fl))
            
        if possible:
            for n_idx, s_idx, fl in assignments:
                node_name = env.nodes[n_idx]
                s_name = env.services[s_idx]
                
                x[(agent, node_name, s_name)] = fl
                current_caps[n_idx][s_idx] -= fl
                assigned_agents[n_idx][s_idx].append(agent)

    # Phase 2: 頻率精確分配 (Fair Share with Residual Distribution)
    # 策略：
    # 1. 將該 (Node, Service) 的總容量平分給所有指派的 Agent。
    # 2. 產生的頻率若超過 f_h，則取 f_h。
    # 3. 產生的頻率若有餘數 (無法被 k 整除的部分)，將餘數以 1 為單位分配給前幾個 Agent，直到分完。
    fl_list = env.fl_list
    fh_list = env.fh_list
    
    for s_idx in range(n_services):
        fh = fh_list[s_idx]
        fl = fl_list[s_idx]
        if fh <= 0: continue

        for n_idx in range(n_nodes):
            agents_here = assigned_agents[n_idx][s_idx]
            k = len(agents_here)
            if k <= 0: continue
            
            # 這裡我們拿回該服務實例對外提供的「總容量」
            # 注意：在 Phase 1 我們已經扣除了容量，所以這裡要加回來計算原始總量
            node_name = env.nodes[n_idx]
            s_name = env.services[s_idx]
            mask = gene[n_idx]
            total_capacity = env.get_capacity(node_name, mask, s_name)
            
            # 每個 Agent 基礎分配 (取整數，且不高於 fh)
            # 因為第一階段保證了 total_capacity >= k * fl，所以 avg 至少為 fl
            avg_freq = min(fh, total_capacity // k)
            
            # 更新所有 Agent 的基礎頻率
            for a in agents_here:
                x[(a, node_name, s_name)] = float(avg_freq)
            
            # 計算剩餘可供分配的餘數 (Residual)
            # 只有當平均頻率尚未達到 fh 時，才分配餘數
            if avg_freq < fh:
                residual = int(total_capacity % k)
                # 將餘數 1 點 1 點分給前面的人，分完為止
                for i in range(residual):
                    a = agents_here[i]
                    if x[(a, node_name, s_name)] < fh:
                        x[(a, node_name, s_name)] += 1.0

    return x


def allocate_x_packed_opt(env: GAEnv, gene: Chromosome) -> Tuple[int, float]:
    """
    allocate_x_packed 的優化版本。
    直接回傳 (N_sat, Q)。
    """
    n_nodes = len(env.nodes)
    n_services = len(env.services)

    current_caps = []
    for i in range(n_nodes):
        current_caps.append(list(env.all_caps[i][gene[i]]))

    assigned_counts = [[0] * n_services for _ in range(n_nodes)]
    N_sat = 0
    Q = 0.0

    for agent in env.agents_sorted:
        reqs = env.req_idxs[agent]
        assignments = []
        possible = True
        
        for s_idx, fl in reqs:
            best_n = -1
            # First-Fit
            for n_idx in range(n_nodes):
                if current_caps[n_idx][s_idx] >= fl:
                    best_n = n_idx
                    break
            
            if best_n == -1:
                possible = False
                break
            
            assignments.append((best_n, s_idx, fl))
            
        if possible:
            N_sat += 1
            Q += env.agent_qs[agent]
            for n_idx, s_idx, fl in assignments:
                current_caps[n_idx][s_idx] -= fl
                assigned_counts[n_idx][s_idx] += 1

    # Phase 2
    fl_list = env.fl_list
    fh_list = env.fh_list
    
    for s_idx in range(n_services):
        fh = fh_list[s_idx]
        fl = fl_list[s_idx]
        if fh <= 0: continue
        delta = fh - fl
        if delta <= 0: continue
        
        for n_idx in range(n_nodes):
            k = assigned_counts[n_idx][s_idx]
            if k <= 0: continue
            rem = current_caps[n_idx][s_idx]
            if rem <= 0: continue
            
            extra_used = rem if rem < k * delta else (k * delta)
            Q += extra_used / fh

    return N_sat, Q


def allocate_x_ff_bf_dict(env: GAEnv, gene: Chromosome) -> Dict[Tuple[str, str, str], float]:
    """
    First-Fit at f_h + Best-Fit fallback 分配器（dict 版本，用於最終部署與剪枝）。

    Phase 1a: predFreq = total_cap/(k+1) >= f_h 時，First-Fit 選第一個符合的節點，
              使 agent 集中於前幾個節點，讓後面的節點保持空著以利剪枝。
    Phase 1b: 若所有節點 predFreq < f_h，改選 predFreq 最高且 >= f_l 的節點
              （Best-Fit fallback，同 optimizer.py 邏輯），最大化可得頻率。
    Phase 2:  各 (node, service) 將總容量 fair-share 分給所有已連線 agent，
              上限 f_h，餘數逐一補給前幾個 agent。
    """
    n_nodes = len(env.nodes)
    n_services = len(env.services)

    _zero_caps = [0.0] * n_services
    total_caps = [list(env.all_caps[i].get(gene[i], _zero_caps)) for i in range(n_nodes)]
    connections = [[0] * n_services for _ in range(n_nodes)]

    x: Dict[Tuple[str, str, str], float] = {}
    assigned_agents = [[[] for _ in range(n_services)] for _ in range(n_nodes)]

    fh_list = env.fh_list
    fl_list = env.fl_list

    for agent in env.agents_sorted:
        reqs = env.req_idxs[agent]
        assignments = []
        possible = True

        for s_idx, fl in reqs:
            fh = fh_list[s_idx]
            best_n = -1

            # Phase 1a: First-Fit at f_h
            for n_idx in range(n_nodes):
                k = connections[n_idx][s_idx]
                total = total_caps[n_idx][s_idx]
                if total > 0 and total / (k + 1) >= fh:
                    best_n = n_idx
                    break

            # Phase 1b: Best-Fit fallback（max predFreq >= f_l）
            if best_n == -1:
                best_pred = -1.0
                for n_idx in range(n_nodes):
                    k = connections[n_idx][s_idx]
                    total = total_caps[n_idx][s_idx]
                    if total <= 0:
                        continue
                    pred = total / (k + 1)
                    if pred >= fl and pred > best_pred:
                        best_pred = pred
                        best_n = n_idx

            if best_n == -1:
                possible = False
                break

            assignments.append((best_n, s_idx))

        if possible:
            for n_idx, s_idx in assignments:
                connections[n_idx][s_idx] += 1
                assigned_agents[n_idx][s_idx].append(agent)

    # Phase 2: Fair-share + 餘數分配
    for s_idx in range(n_services):
        fh = fh_list[s_idx]
        if fh <= 0:
            continue
        for n_idx in range(n_nodes):
            agents_here = assigned_agents[n_idx][s_idx]
            k = len(agents_here)
            if k <= 0:
                continue
            node_name = env.nodes[n_idx]
            s_name = env.services[s_idx]
            total = env.get_capacity(node_name, gene[n_idx], s_name)

            avg_freq = min(fh, total // k)
            for a in agents_here:
                x[(a, node_name, s_name)] = float(avg_freq)

            if avg_freq < fh:
                residual = int(total % k)
                for i in range(residual):
                    a = agents_here[i]
                    if x[(a, node_name, s_name)] < fh:
                        x[(a, node_name, s_name)] += 1.0

    return x


def allocate_x_ff_bf_opt(env: GAEnv, gene: Chromosome) -> Tuple[int, float]:
    """
    allocate_x_ff_bf_dict 的優化版本，直接回傳 (N_sat, Q)，用於 GA 適應度評估。
    Phase 1a/1b 與 dict 版本完全相同，Phase 2 以公式計算 Q 增益。
    Q 為正規化平均（2026-07-18 修正，同 score_from_x 的理由）。
    """
    n_nodes = len(env.nodes)
    n_services = len(env.services)

    _zero_caps = [0.0] * n_services
    total_caps = [list(env.all_caps[i].get(gene[i], _zero_caps)) for i in range(n_nodes)]
    connections = [[0] * n_services for _ in range(n_nodes)]

    N_sat = 0
    Q = 0.0

    fh_list = env.fh_list
    fl_list = env.fl_list

    for agent in env.agents_sorted:
        reqs = env.req_idxs[agent]
        assignments = []
        possible = True

        for s_idx, fl in reqs:
            fh = fh_list[s_idx]
            best_n = -1

            # Phase 1a: First-Fit at f_h
            for n_idx in range(n_nodes):
                k = connections[n_idx][s_idx]
                total = total_caps[n_idx][s_idx]
                if total > 0 and total / (k + 1) >= fh:
                    best_n = n_idx
                    break

            # Phase 1b: Best-Fit fallback（max predFreq >= f_l）
            if best_n == -1:
                best_pred = -1.0
                for n_idx in range(n_nodes):
                    k = connections[n_idx][s_idx]
                    total = total_caps[n_idx][s_idx]
                    if total <= 0:
                        continue
                    pred = total / (k + 1)
                    if pred >= fl and pred > best_pred:
                        best_pred = pred
                        best_n = n_idx

            if best_n == -1:
                possible = False
                break

            assignments.append((best_n, s_idx))

        if possible:
            N_sat += 1
            Q += env.agent_qs[agent]
            for n_idx, s_idx in assignments:
                connections[n_idx][s_idx] += 1

    # Phase 2: 計算 fair-share Q 增益（total - k*fl 剩餘容量分配至 f_h）
    for s_idx in range(n_services):
        fh = fh_list[s_idx]
        fl = fl_list[s_idx]
        if fh <= 0:
            continue
        delta = fh - fl
        if delta <= 0:
            continue
        for n_idx in range(n_nodes):
            k = connections[n_idx][s_idx]
            if k <= 0:
                continue
            total = total_caps[n_idx][s_idx]
            rem = total - k * fl
            if rem <= 0:
                continue
            extra_used = rem if rem < k * delta else k * delta
            Q += extra_used / fh

    total_pairs = sum(len(env.req_idxs[agent]) for agent in env.agents)
    Q = Q / total_pairs if total_pairs > 0 else 0.0

    return N_sat, Q


# ---------- 評分 / 評鑑 ----------

def score_from_x(env: GAEnv, x: Dict[Tuple[str, str, str], float]) -> Tuple[int, float]:
    """
    同你的原始 evaluate()：
    - N_sat：agent 只有在所有需求服務都達到 f_l 時才計算
    - Q：對所有 (agent,service) 配對取 freq/f_h 的正規化平均，範圍 0~1。
      2026-07-18 修正：原本是純加總、隨配對數線性放大，跟
      core_reconcile_loop 寫進 q_score 欄的定義（除以總配對數）不一致，
      也跟論文 4-1-6 節的正規化定義（2026-06-28 已修正過一次）對不上，
      這裡之前沒有跟著改到。改成除以配對數不影響 GA 內部任何比較的相對
      排序（tournament selection、deploy_switch_policy 的 current vs best
      比較，都是同一個 env 下的多次 evaluate()，配對數是同一個常數，整批
      除以它不改變大小關係），純粹讓 Q 的絕對數值變得有意義、可跨場景比較，
      也讓後續要加的切換門檻閥值有穩定尺度可用。
    """
    # 累積每個 (agent, service) 的頻率
    freq: Dict[Tuple[str, str], float] = {}
    for agent in env.agents:
        for s in env.req[agent]:
            freq[(agent, s)] = 0.0

    for (agent, node, s), val in x.items():
        freq[(agent, s)] += val

    # 計算 N_sat
    N_sat = 0
    for agent in env.agents:
        ok = True
        for s in env.req[agent]:
            if freq[(agent, s)] < env.f_l[s]:
                ok = False
                break
        if ok:
            N_sat += 1

    # 計算 Q（正規化平均）
    Q = 0.0
    total_pairs = 0
    for agent in env.agents:
        for s in env.req[agent]:
            Q += freq[(agent, s)] / env.f_h[s]
            total_pairs += 1
    Q = Q / total_pairs if total_pairs > 0 else 0.0

    return N_sat, Q


AllocatorFn = callable


def evaluate(env: GAEnv, gene: Chromosome, allocator: Optional[object] = "nsats_first") -> Tuple[int, float]:
    """
    回傳 (N_sat, Q)，含計時分解。

    `allocator` 可以是：
      - 字串名稱：'nsats_first', 'slots', 'slots_full'
      - 可呼叫的分配器函式，簽名為 allocator(env, gene)

    可呼叫的分配器可回傳：
      - (N_sat, Q) tuple（已優化版本）
      - x dict 對應 (agent,node,service)->freq。兩者都支援。
    """

    if isinstance(allocator, str):
        name = allocator.lower().strip()
        if name == "nsats_first":
            alloc_fn = allocate_x_nsats_first_opt
        elif name in ("nsats_plus", "nsats_full"):  # 新增此行
            alloc_fn = allocate_x_nsats_plus 
        elif name in ("slots", "slot"):
            alloc_fn = allocate_x_slots_opt
        elif name in ("slots_full", "slot_full", "slots_plus"):
            alloc_fn = allocate_x_slots_full_opt
        elif name in ("packed", "pack", "ff_bf"):
            alloc_fn = allocate_x_ff_bf_opt
        elif name in ("packed_dict", "pack_dict", "ff_bf_dict"):
            alloc_fn = allocate_x_ff_bf_dict
        else:
            raise ValueError(f"不明白的分配器 '{allocator}'。使用 'nsats_first', 'slots', 'ff_bf' 或 'ff_bf_dict'。")
    elif callable(allocator):
        alloc_fn = allocator
    else:
        raise ValueError("allocator 必須是字串名稱或可呼叫函式")

    t0 = time.perf_counter()
    result = alloc_fn(env, gene)
    t1 = time.perf_counter()
    
    # 檢查結果是 (N_sat, Q) 或 x dict
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], (int, float)):
        N_sat, Q = result
        # 評分已包含或為 0
        t2 = t1
    else:
        x = result
        N_sat, Q = score_from_x(env, x)
        t2 = time.perf_counter()

    TIMING.eval_calls += 1
    TIMING.alloc_time_s += (t1 - t0)
    TIMING.score_time_s += (t2 - t1)

    return N_sat, Q


# ---------- GA 運算子 ----------

def tournament_select(env: GAEnv, pop: List[Chromosome], fits: Dict[int, Tuple[int, float]], k: int = 3) -> Chromosome:
    """簡單的競賽選擇，用索引方便重用已算的 fitness"""
    idxs = random.sample(range(len(pop)), k)
    best_idx = idxs[0]
    for i in idxs[1:]:
        if fits[i] > fits[best_idx]:  # tuple 比較 = 字典序
            best_idx = i
    return pop[best_idx][:]


def crossover(parent1: Chromosome, parent2: Chromosome, pc: float = 0.9) -> Tuple[Chromosome, Chromosome]:
    """逐節點統一交叉"""
    if random.random() > pc:
        return parent1[:], parent2[:]

    c1, c2 = [], []
    for g1, g2 in zip(parent1, parent2):
        if random.random() < 0.5:
            c1.append(g1)
            c2.append(g2)
        else:
            c1.append(g2)
            c2.append(g1)
    return c1, c2


def mutate_hybrid(env: GAEnv, gene: Chromosome, pm: float = 0.3, hamming_prob: float = 0.7) -> Chromosome:
    """
    混合突變策略：
    - hamming_prob 機率用 Hamming-1/2 鄰居（局部微調）
    - (1-hamming_prob) 機率用隨機合法遮罩（避免局部最優）
    """
    from itertools import combinations

    new_gene = gene[:]
    num_services = len(env.services)

    for node_idx, node in enumerate(env.nodes):
        if random.random() < pm:
            current_mask = new_gene[node_idx]

            if random.random() < hamming_prob:
                found = False
                for hamming_dist in range(1, 3):  # 只檢查 1, 2
                    neighbors = []
                    for bit_positions in combinations(range(num_services), hamming_dist):
                        candidate = current_mask
                        for bit in bit_positions:
                            candidate ^= (1 << bit)
                        if env.is_legal_mask(node, candidate):
                            neighbors.append(candidate)
                    if neighbors:
                        new_gene[node_idx] = random.choice(neighbors)
                        found = True
                        break

                if not found:
                    masks = env.legal_masks_int[node]
                    if len(masks) > 1:
                        choices = [m for m in masks if m != current_mask]
                        if choices:
                            new_gene[node_idx] = random.choice(choices)
            else:
                masks = env.legal_masks_int[node]
                if len(masks) > 1:
                    choices = [m for m in masks if m != current_mask]
                    if choices:
                        new_gene[node_idx] = random.choice(choices)

    return new_gene


# ---------- 主要 GA 迴圈 ----------

def run_ga(
    model_path: str = "system_model.json",
    pop_size: int = 30,
    generations: int = 100,
    pc: float = 0.9,
    pm: float = 0.1,
    seed: int = 42,
    log_csv: Optional[str] = "ga_population.csv",
    max_time: Optional[float] = None,
    allocator: str = "nsats_first",
    mutation: str = "hybrid",
    initial_gene: Optional[Chromosome] = None,
):
    """
    執行 GA 並印出計時分解。

    allocator：
      - "nsats_first"（基準）
      - "slots"（新 SLOT 分配器）

    mutation：
      - "hybrid"（預設，快速）
      
    initial_gene: 先前梯次的優質配置，作為新一輪搜尋的參考起點
    """
    logger.info("=== 啟動 GA === allocator=%s, pop_size=%d, generations=%d, pc=%.2f, pm=%.2f, seed=%d",
                allocator, pop_size, generations, pc, pm, seed)
    if max_time is not None:
        logger.info("時間限制: %.2f 秒", max_time)
    random.seed(seed)
    model = load_model(model_path)
    env = GAEnv(model)

    pop = init_population(env, pop_size, initial_gene=initial_gene)
    logger.info("族群初始化完成, 初始大小=%d, warm_start=%s", len(pop), initial_gene is not None)
    
    # 增加：若有參考解，則加入一些該解的輕微變異體（幫助直接從前次解附近找更佳）
    if initial_gene is not None:
        for _ in range(min(5, pop_size // 3)):
            pop.append(mutate_hybrid(env, initial_gene, pm=0.2, hamming_prob=1.0))
        # 裁剪掉多出來的
        pop = pop[:pop_size]
        logger.info("已注入 warm-start 變異體, 族群大小=%d", len(pop))

    best_gene: Optional[Chromosome] = None
    best_fit: Optional[Tuple[int, float]] = None

    TIMING.reset()

    start_time = time.perf_counter()

    # CSV 輸出
    csv_f = None
    writer = None
    if log_csv:
        csv_f = open(log_csv, "w", newline="", encoding="utf-8")
        writer = csv.writer(csv_f)
        header = ["gen", "idx", "is_best_gen", "N_sat", "Q", "elapsed_time"]
        header += [f"mask_{i}" for i in range(len(env.nodes))]
        header += ["unique_pop"]
        writer.writerow(header)

    gen = 0
    while True:
        elapsed_time = time.perf_counter() - start_time
        if max_time is not None and elapsed_time >= max_time:
            logger.info("⏰ 達到時間限制 %.2f 秒，停止於第 %d 代", max_time, gen)
            break
        if gen >= generations:
            logger.info("✓ 達到世代數限制 %d 代", generations)
            break

        # 計算當代族群的 fitness
        fits: Dict[int, Tuple[int, float]] = {}
        for i, g in enumerate(pop):
            fits[i] = evaluate(env, g, allocator=allocator)

        # 更新最佳
        gen_best_idx = max(fits.keys(), key=lambda i: fits[i])
        prev_best_fit = best_fit
        for i, f in fits.items():
            if best_fit is None or f > best_fit:
                best_fit = f
                best_gene = pop[i][:]

        uniq = count_unique_genes(pop)
        elapsed_time = time.perf_counter() - start_time

        # 每代 log
        gen_best_ns, gen_best_q = fits[gen_best_idx]
        logger.debug("Gen %3d | best_gen N_sat=%d Q=%.3f | global_best N_sat=%d Q=%.3f | unique=%d | elapsed=%.3fs",
                     gen, gen_best_ns, gen_best_q,
                     best_fit[0], best_fit[1], uniq, elapsed_time)
        if prev_best_fit is None or best_fit > prev_best_fit:
            logger.info("🔺 Gen %d 全域最佳更新: N_sat=%d, Q=%.3f", gen, best_fit[0], best_fit[1])

        # 紀錄
        if writer:
            for i, g in enumerate(pop):
                ns, q = fits[i]
                row = [gen, i, int(i == gen_best_idx), ns, q, elapsed_time]
                row += g[:]
                row += [uniq]
                writer.writerow(row)

        # 下一代
        new_pop: List[Chromosome] = []
        new_pop.append(best_gene[:])  # 菁英保留

        while len(new_pop) < pop_size:
            p1 = tournament_select(env, pop, fits, k=3)
            p2 = tournament_select(env, pop, fits, k=3)
            c1, c2 = crossover(p1, p2, pc=pc)
            if mutation == "hybrid":
                c1 = mutate_hybrid(env, c1, pm=pm)
                c2 = mutate_hybrid(env, c2, pm=pm)
            else:
                c1 = mutate_hybrid(env, c1, pm=pm)
                c2 = mutate_hybrid(env, c2, pm=pm)

            new_pop.append(c1)
            if len(new_pop) < pop_size:
                new_pop.append(c2)

        pop = new_pop
        gen += 1

    # 最終評鑑（不過度污染計時統計）
    if best_gene is None:
        best_gene = pop[0][:]
    final_fit = evaluate(env, best_gene, allocator=allocator)

    total_time = time.perf_counter() - start_time
    outer_time = max(0.0, total_time - TIMING.eval_time_s)

    logger.info("=== GA 結束 ===")
    logger.info("分配器: %s", allocator)
    logger.info("總執行時間: %.3f 秒", total_time)
    logger.info("完成世代數: %d", gen)
    logger.info("最佳 N_sat=%d, Q=%.3f", final_fit[0], final_fit[1])

    # 計時分解
    logger.info("=== 時間分解 ===")
    logger.info("eval_calls: %d", TIMING.eval_calls)
    if total_time > 0:
        logger.info("分配時間: %.3fs (%.1f%%)", TIMING.alloc_time_s, TIMING.alloc_time_s/total_time*100)
        logger.info("評分時間: %.3fs (%.1f%%)", TIMING.score_time_s, TIMING.score_time_s/total_time*100)
        logger.info("GA 外層: %.3fs (%.1f%%)", outer_time, outer_time/total_time*100)
        logger.info("eval 合計: %.3fs (%.1f%%)", TIMING.eval_time_s, TIMING.eval_time_s/total_time*100)

    logger.info("最佳部署方案:")
    for node_idx, node in enumerate(env.nodes):
        mask = best_gene[node_idx]
        svcs = decode_mask(mask, env.services)
        logger.info("  %s: mask=%d (%s) -> services=%s", node, mask, bin(mask), svcs)

    if csv_f:
        csv_f.close()
        logger.info("已輸出族群紀錄到: %s", log_csv)

    return best_gene, final_fit, total_time, gen


# ---------- 其他啟發式演算法（非 GA） ----------

def run_lns(
    model_path: str = "system_model.json",
    iterations: int = 500,
    allocator: str = "nsats_first",
    destroy_k: Tuple[int, int] = (2, 5),
    candidates_per_node: int = 3,
    max_combos: int = 128,
    seed: int = 42,
    max_time: Optional[float] = None,
    initial_gene: Optional[Chromosome] = None,
):
    """大鄰域搜尋，包含摧毀/修復策略。"""
    logger.info("=== 啟動 LNS === allocator=%s, iterations=%d, destroy_k=%s, seed=%d",
                allocator, iterations, destroy_k, seed)
    if max_time is not None:
        logger.info("時間限制: %.2f 秒", max_time)
    random.seed(seed)
    model = load_model(model_path)
    env = GAEnv(model)

    TIMING.reset()
    start = time.perf_counter()

    best_gene = initial_gene[:] if initial_gene is not None else random_chromosome(env)
    best_fit = evaluate(env, best_gene, allocator=allocator)
    logger.info("LNS 初始解: N_sat=%d, Q=%.3f, warm_start=%s", best_fit[0], best_fit[1], initial_gene is not None)

    for it in range(iterations):
        base_gene = best_gene[:]
        min_k, max_k = destroy_k
        if max_k < 1:
            max_k = 1
        if min_k < 1:
            min_k = 1
        k = random.randint(min_k, min(max_k, len(env.nodes)))
        node_idxs = random.sample(range(len(env.nodes)), k)

        # 為每個摧毀節點生成候選遮罩
        candidate_lists: List[List[int]] = []
        for idx in node_idxs:
            node_name = env.nodes[idx]
            masks = env.legal_masks_int[node_name]
            scores = env.mask_scores[idx]
            # 按遮罩評分排名
            ranked = sorted(masks, key=lambda m: scores.get(m, 0.0), reverse=True)
            cand = ranked[:candidates_per_node]
            cur_mask = base_gene[idx]
            if cur_mask not in cand:
                cand.append(cur_mask)
            if not cand:
                cand = [cur_mask]
            candidate_lists.append(cand)

        # 測試候選組合（有上限）
        combos_tested = 0
        for combo in product(*candidate_lists):
            if combos_tested >= max_combos:
                break
            combos_tested += 1
            trial_gene = base_gene[:]
            for offset, node_idx in enumerate(node_idxs):
                trial_gene[node_idx] = combo[offset]
            fit = evaluate(env, trial_gene, allocator=allocator)
            if fit > best_fit:
                best_fit = fit
                best_gene = trial_gene
                base_gene = trial_gene[:]  # 在新最優解附近加強搜尋
                logger.info("🔺 LNS iter %d 最佳更新: N_sat=%d, Q=%.3f", it, best_fit[0], best_fit[1])
            if max_time is not None and (time.perf_counter() - start) >= max_time:
                break
        if max_time is not None and (time.perf_counter() - start) >= max_time:
            break

    total = time.perf_counter() - start
    summarize("LNS", allocator, best_gene, best_fit, total, iterations, env)
    return best_gene, best_fit, total, iterations


def run_two_phase(
    model_path: str = "system_model.json",
    iterations: int = 3000,
    allocator: str = "nsats_first",
    phase_ratio: float = 0.7,
    pm: float = 0.2,
    seed: int = 42,
    max_time: Optional[float] = None,
    initial_gene: Optional[Chromosome] = None,
):
    """兩階段搜尋：首先最大化 N_sat，然後在固定 N_sat 下提升 Q。"""
    logger.info("=== 啟動 Two-Phase === allocator=%s, iterations=%d, phase_ratio=%.2f, pm=%.2f, seed=%d",
                allocator, iterations, phase_ratio, pm, seed)
    if max_time is not None:
        logger.info("時間限制: %.2f 秒", max_time)
    random.seed(seed)
    model = load_model(model_path)
    env = GAEnv(model)

    TIMING.reset()
    start = time.perf_counter()

    current = initial_gene[:] if initial_gene is not None else random_chromosome(env)
    current_fit = evaluate(env, current, allocator=allocator)
    best_gene = current[:]
    best_fit = current_fit
    best_ns_gene = current[:]
    best_ns_fit = current_fit
    logger.info("Two-Phase 初始解: N_sat=%d, Q=%.3f", current_fit[0], current_fit[1])

    phase1_iters = max(1, int(iterations * phase_ratio))
    phase2_iters = max(0, iterations - phase1_iters)
    logger.info("Phase 1 (最大化 N_sat): %d iters, Phase 2 (推升 Q): %d iters", phase1_iters, phase2_iters)

    # 第 1 階段：忽略 Q（除了作為平手破解者）
    for _ in range(phase1_iters):
        cand_gene = mutate_hybrid(env, current, pm=pm)
        cand_fit = evaluate(env, cand_gene, allocator=allocator)
        if cand_fit[0] > current_fit[0] or (cand_fit[0] == current_fit[0] and cand_fit[1] >= current_fit[1]):
            current = cand_gene
            current_fit = cand_fit
        if cand_fit[0] > best_ns_fit[0] or (cand_fit[0] == best_ns_fit[0] and cand_fit[1] > best_ns_fit[1]):
            best_ns_gene = cand_gene
            best_ns_fit = cand_fit
        if cand_fit > best_fit:
            best_gene = cand_gene
            best_fit = cand_fit
        if max_time is not None and (time.perf_counter() - start) >= max_time:
            break

    target_ns = best_ns_fit[0]
    current = best_ns_gene[:]
    current_fit = best_ns_fit
    logger.info("Phase 1 結束: best_ns N_sat=%d, Q=%.3f -> 進入 Phase 2 (target_ns=%d)",
                best_ns_fit[0], best_ns_fit[1], target_ns)

    # 第 2 階段：鎖定 N_sat 閾值，推升 Q
    for _ in range(phase2_iters):
        cand_gene = mutate_hybrid(env, current, pm=pm)
        cand_fit = evaluate(env, cand_gene, allocator=allocator)

        if cand_fit[0] > target_ns:
            target_ns = cand_fit[0]
        if cand_fit[0] >= target_ns:
            if cand_fit[0] > current_fit[0] or (cand_fit[0] == current_fit[0] and cand_fit[1] >= current_fit[1]):
                current = cand_gene
                current_fit = cand_fit
        else:
            # 小機率接受略微下降以逃脫局部最優
            if random.random() < 0.05:
                current = cand_gene
                current_fit = cand_fit

        if cand_fit > best_fit:
            best_gene = cand_gene
            best_fit = cand_fit
        if max_time is not None and (time.perf_counter() - start) >= max_time:
            break

    total = time.perf_counter() - start
    summarize("Two-Phase", allocator, best_gene, best_fit, total, iterations, env)
    return best_gene, best_fit, total, iterations


def summarize(name: str, allocator: str, best_gene: Chromosome, best_fit: Tuple[int, float], total_time: float, steps: int, env: GAEnv):
    """總結演算法執行結果。"""
    logger.info("=== %s 結束 ===", name)
    logger.info("分配器: %s", allocator)
    logger.info("總執行時間: %.3f 秒", total_time)
    logger.info("迭代次數: %d", steps)
    logger.info("最佳 N_sat=%d, Q=%.3f", best_fit[0], best_fit[1])

    logger.info("最佳部署方案:")
    for node_idx, node in enumerate(env.nodes):
        mask = best_gene[node_idx]
        svcs = decode_mask(mask, env.services)
        logger.info("  %s: mask=%d (%s) -> services=%s", node, mask, bin(mask), svcs)


def run_random_search(
    model_path: str = "system_model.json",
    iterations: int = 1000,
    allocator: str = "nsats_first",
    seed: int = 42,
    max_time: Optional[float] = None,
    initial_gene: Optional[Chromosome] = None,
):
    """隨機搜尋基線。"""
    logger.info("=== 啟動 Random Search === allocator=%s, iterations=%d, seed=%d", allocator, iterations, seed)
    if max_time is not None:
        logger.info("時間限制: %.2f 秒", max_time)
    random.seed(seed)
    model = load_model(model_path)
    env = GAEnv(model)

    TIMING.reset()
    start = time.perf_counter()

    best_gene: Optional[Chromosome] = initial_gene[:] if initial_gene is not None else None
    best_fit: Optional[Tuple[int, float]] = evaluate(env, best_gene, allocator=allocator) if best_gene else None
    logger.info("Random Search 初始解: %s", f"N_sat={best_fit[0]}, Q={best_fit[1]:.3f}" if best_fit else "None")

    for it in range(iterations):
        g = random_chromosome(env)
        fit = evaluate(env, g, allocator=allocator)
        if best_fit is None or fit > best_fit:
            best_fit = fit
            best_gene = g
            logger.debug("Random Search iter %d 最佳更新: N_sat=%d, Q=%.3f", it, best_fit[0], best_fit[1])
        if max_time is not None and (time.perf_counter() - start) >= max_time:
            break

    total = time.perf_counter() - start
    summarize("Random Search", allocator, best_gene, best_fit, total, iterations, env)
    return best_gene, best_fit, total, iterations


def run_hill_climb(
    model_path: str = "system_model.json",
    iterations: int = 1000,
    allocator: str = "nsats_first",
    pm: float = 0.2,
    seed: int = 42,
    max_time: Optional[float] = None,
    initial_gene: Optional[Chromosome] = None,
):
    """爬坡演算法。"""
    logger.info("=== 啟動 Hill Climb === allocator=%s, iterations=%d, pm=%.2f, seed=%d",
                allocator, iterations, pm, seed)
    if max_time is not None:
        logger.info("時間限制: %.2f 秒", max_time)
    random.seed(seed)
    model = load_model(model_path)
    env = GAEnv(model)

    TIMING.reset()
    start = time.perf_counter()

    current = initial_gene[:] if initial_gene is not None else random_chromosome(env)
    current_fit = evaluate(env, current, allocator=allocator)
    best_gene = current[:]
    best_fit = current_fit
    logger.info("Hill Climb 初始解: N_sat=%d, Q=%.3f", current_fit[0], current_fit[1])

    for it in range(iterations):
        cand = mutate_hybrid(env, current, pm=pm)
        cand_fit = evaluate(env, cand, allocator=allocator)
        # 接受不變差的；分開追蹤最佳
        if cand_fit >= current_fit:
            current = cand
            current_fit = cand_fit
        if cand_fit > best_fit:
            best_gene = cand
            best_fit = cand_fit
            logger.debug("Hill Climb iter %d 最佳更新: N_sat=%d, Q=%.3f", it, best_fit[0], best_fit[1])
        if max_time is not None and (time.perf_counter() - start) >= max_time:
            break

    total = time.perf_counter() - start
    summarize("Hill Climb", allocator, best_gene, best_fit, total, iterations, env)
    return best_gene, best_fit, total, iterations


def run_simulated_annealing(
    model_path: str = "system_model.json",
    iterations: int = 2000,
    allocator: str = "nsats_first",
    pm: float = 0.2,
    t_start: float = 1.0,
    t_end: float = 0.01,
    seed: int = 42,
    max_time: Optional[float] = None,
    initial_gene: Optional[Chromosome] = None,
):
    """模擬退火演算法。"""
    logger.info("=== 啟動 Simulated Annealing === allocator=%s, iterations=%d, pm=%.2f, T=[%.2f->%.2f], seed=%d",
                allocator, iterations, pm, t_start, t_end, seed)
    if max_time is not None:
        logger.info("時間限制: %.2f 秒", max_time)
    random.seed(seed)
    model = load_model(model_path)
    env = GAEnv(model)

    TIMING.reset()
    start = time.perf_counter()

    current = initial_gene[:] if initial_gene is not None else random_chromosome(env)
    current_fit = evaluate(env, current, allocator=allocator)
    best_gene = current[:]
    best_fit = current_fit
    logger.info("SA 初始解: N_sat=%d, Q=%.3f", current_fit[0], current_fit[1])

    def temperature(step: int) -> float:
        # 指數遞減
        r = step / max(1, iterations - 1)
        return t_start * (t_end / t_start) ** r

    for step in range(iterations):
        cand = mutate_hybrid(env, current, pm=pm)
        cand_fit = evaluate(env, cand, allocator=allocator)
        if cand_fit >= current_fit:
            accept = True
        else:
            T = temperature(step)
            # 能量 = -fitness（字典序 tuple 不直接；近似用加權和）
            delta = (cand_fit[0] - current_fit[0]) * 1e3 + (cand_fit[1] - current_fit[1])
            accept_prob = 1.0 if T <= 0 else min(1.0, pow(2.718281828, delta / max(T, 1e-12)))
            accept = random.random() < accept_prob

        if accept:
            current = cand
            current_fit = cand_fit
        if cand_fit > best_fit:
            best_gene = cand
            best_fit = cand_fit
            logger.debug("SA step %d 最佳更新: N_sat=%d, Q=%.3f (T=%.4f)",
                         step, best_fit[0], best_fit[1], temperature(step))
        if max_time is not None and (time.perf_counter() - start) >= max_time:
            break

    total = time.perf_counter() - start
    summarize("Simulated Annealing", allocator, best_gene, best_fit, total, iterations, env)
    return best_gene, best_fit, total, iterations


if __name__ == "__main__":
    # 選擇一個啟發式演算法快速執行。
    MODE = "ga"  # 選項："ga", "random", "hill", "sa", "lns", "two_phase"
    MAX_TIME = 0.3
    
    # 策略：用 'nsats_first' 搜尋（光滑景觀），用 'nsats_plus' 報告（提升 Q）
    SEARCH_ALLOCATOR = "nsats_first"
    FINAL_ALLOCATOR = "nsats_first"

    logger.info("=== 主程式啟動 === MODE=%s, MAX_TIME=%.2f, SEARCH_ALLOCATOR=%s, FINAL_ALLOCATOR=%s",
                MODE, MAX_TIME, SEARCH_ALLOCATOR, FINAL_ALLOCATOR)

    best_gene = None
    search_fit = None

    if MODE == "ga":
        best_gene, search_fit, _, _ = run_ga(
            model_path="system_model.json",
            pop_size=30,
            generations=1000,
            pc=0.9,
            pm=0.1,
            seed=42,
            log_csv="ga_population.csv",
            max_time=MAX_TIME,
            allocator=SEARCH_ALLOCATOR,
        )
    elif MODE == "random":
        best_gene, search_fit, _, _ = run_random_search(
            model_path="system_model.json",
            iterations=2000,
            allocator=SEARCH_ALLOCATOR,
            seed=42,
            max_time=MAX_TIME,
        )
    elif MODE == "hill":
        best_gene, search_fit, _, _ = run_hill_climb(
            model_path="system_model.json",
            iterations=2000,
            allocator=SEARCH_ALLOCATOR,
            pm=0.2,
            seed=42,
            max_time=MAX_TIME,
        )
    elif MODE == "sa":
        best_gene, search_fit, _, _ = run_simulated_annealing(
            model_path="system_model.json",
            iterations=3000,
            allocator=SEARCH_ALLOCATOR,
            pm=0.2,
            t_start=1.0,
            t_end=0.01,
            seed=42,
            max_time=MAX_TIME,
        )
    elif MODE == "lns":
        best_gene, search_fit, _, _ = run_lns(
            model_path="system_model.json",
            iterations=800,
            allocator=SEARCH_ALLOCATOR,
            destroy_k=(2, 5),
            candidates_per_node=3,
            max_combos=160,
            seed=42,
            max_time=MAX_TIME,
        )
    elif MODE == "two_phase":
        best_gene, search_fit, _, _ = run_two_phase(
            model_path="system_model.json",
            iterations=4000,
            allocator=SEARCH_ALLOCATOR,
            phase_ratio=0.7,
            pm=0.25,
            seed=42,
            max_time=MAX_TIME,
        )
    else:
        raise SystemExit(f"不明白的 MODE：{MODE}")

    # 最終優化步驟與增量部署
    if best_gene is not None:
        logger.info("=== 最終優化與增量部署 (後處理) ===")
        model = load_model("system_model.json")
        env = GAEnv(model)

        # 這裡單純負責最後的 Packing 與 Pruning
        # 使用 ff_bf_dict（First-Fit at f_h + Best-Fit fallback）進行最終分配，集中連線以利剪枝
        x_packed = allocate_x_ff_bf_dict(env, best_gene)
        fit_packed = score_from_x(env, x_packed)
        logger.info("Packing 分配結果: N_sat=%d, Q=%.3f", fit_packed[0], fit_packed[1])
        
        # 閒置服務剪枝 (Pruning)
        # 解析哪些 (node, service) 在收攏模式下真的有被連線用到
        used_services = set()
        for (agent, node, s), freq in x_packed.items():
            if freq > 0:
                used_services.add((node, s))
                
        pruned_gene = list(best_gene)
        for n_idx, node in enumerate(env.nodes):
            mask = pruned_gene[n_idx]
            new_mask = mask
            for s_idx, s in enumerate(env.services):
                if mask & (1 << s_idx):
                    if (node, s) not in used_services:
                        # 修剪閒置的服務
                        new_mask &= ~(1 << s_idx)
            pruned_gene[n_idx] = new_mask
            
            old_svcs = decode_mask(mask, env.services)
            new_svcs = decode_mask(new_mask, env.services)
            if mask != new_mask:
                logger.info("  [✂️ 修剪] 節點 %s: %s -> %s", node, old_svcs, new_svcs)
            else:
                logger.info("  [保留] 節點 %s: %s", node, old_svcs)
                
        # 最終驗證剪枝後的效能表現
        final_pruned_fit = evaluate(env, pruned_gene, allocator="ff_bf_dict")
        logger.info("[原始搜尋效能] (%s): N_sat=%d, Q=%.3f", SEARCH_ALLOCATOR, search_fit[0], search_fit[1])
        logger.info("[收攏與剪枝後] (packing_allocator): N_sat=%d, Q=%.3f", final_pruned_fit[0], final_pruned_fit[1])
        logger.info(">>> 增量部署與節點收攏成功！僅部署必需的服務以增加彈性。 <<<")
