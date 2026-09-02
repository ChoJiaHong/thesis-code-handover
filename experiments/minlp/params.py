import random
import math
import json
from itertools import combinations

def create_example_data():
    """
    建立一組對應「正式規格」與「範例資料」的示範參數。

    來源：
    - 問題與數學模型定義：正式規格書.md
    - 範例節點/服務/需求/吞吐量/頻率門檻：範例資料.md
    """

    data = {}

    # 節點集合 M、服務集合 Q、Agent 集合 G
    data["M"] = ["m1", "m2", "m3"]
    data["Q"] = ["q1", "q2", "q3"]
    data["G"] = [f"g{i}" for i in range(1, 21)]

    # 使用者訂閱 R(g_i) — 以三種訂閱組合循環分配
    _subs = [["q1", "q2"], ["q1", "q3"], ["q2", "q3"]]
    data["R"] = {f"g{i}": list(_subs[(i - 1) % 3]) for i in range(1, 21)}

    # 服務組合與吞吐量 W(n,c,k)
    # 這裡用 combos 結構，同時包含該組合內有哪些服務與對應吞吐量
    # 範例資料中的表格直接硬編 (hard-code) 在這裡，方便測試。
    data["combos"] = {
        "m1": {
            "c1": {
                "services": ["q1"],
                "W": {"q1": 100},
            },
            "c2": {
                "services": ["q2"],
                "W": {"q2": 220},
            },
            "c3": {
                "services": ["q3"],
                "W": {"q3": 90},
            },
            "c4": {
                "services": ["q1", "q2"],
                "W": {"q1": 58, "q2": 127},
            },
            "c5": {
                "services": ["q1", "q3"],
                "W": {"q1": 58, "q3": 52},
            },
        },
        "m2": {
            "c1": {
                "services": ["q1"],
                "W": {"q1": 130},
            },
            "c2": {
                "services": ["q2"],
                "W": {"q2": 260},
            },
            "c3": {
                "services": ["q3"],
                "W": {"q3": 110},
            },
            "c4": {
                "services": ["q1", "q2"],
                "W": {"q1": 75, "q2": 150},
            },
            "c5": {
                "services": ["q1", "q3"],
                "W": {"q1": 75, "q3": 63},
            },
            "c6": {
                "services": ["q2", "q3"],
                "W": {"q2": 150, "q3": 63},
            },
            "c7": {
                "services": ["q1", "q2", "q3"],
                "W": {"q1": 56, "q2": 113, "q3": 48},
            },
        },
        "m3": {
            "c1": {
                "services": ["q2"],
                "W": {"q2": 190},
            },
            "c2": {
                "services": ["q3"],
                "W": {"q3": 75},
            },
            "c3": {
                "services": ["q2", "q3"],
                "W": {"q2": 109, "q3": 43},
            },
        },
    }

    # 服務頻率門檻
    data["f_l"] = {
        "q1": 10,
        "q2": 20,
        "q3": 15,
    }
    data["f_h"] = {
        "q1": 20,
        "q2": 40,
        "q3": 30,
    }

    return data

def filter_dominated_combos(data):
    """
    刪除被支配的部署組合：若 combo A 的服務集合為 B 子集，且對每個服務容量皆 <= B，
    則 A 不可能優於 B，可移除。
    """
    combos = data["combos"]
    for n, nd in list(combos.items()):
        ids = list(nd.keys())
        keep = set(ids)
        for c1, c2 in combinations(ids, 2):
            s1 = set(nd[c1]["services"])
            s2 = set(nd[c2]["services"])
            if s1 == s2:
                # 保留容量較大的；若相同容量則保留 c1
                cap1 = nd[c1]["W"]
                cap2 = nd[c2]["W"]
                if all(cap2.get(k, 0) >= cap1.get(k, 0) for k in s1) and any(cap2.get(k, 0) > cap1.get(k, 0) for k in s1):
                    keep.discard(c1)
                elif all(cap1.get(k, 0) >= cap2.get(k, 0) for k in s2) and any(cap1.get(k, 0) > cap2.get(k, 0) for k in s2):
                    keep.discard(c2)
                continue
            # 子集支配
            if s1.issubset(s2):
                if all(nd[c2]["W"].get(k, 0) >= nd[c1]["W"].get(k, 0) for k in s1):
                    keep.discard(c1)
            elif s2.issubset(s1):
                if all(nd[c1]["W"].get(k, 0) >= nd[c2]["W"].get(k, 0) for k in s2):
                    keep.discard(c2)
        data["combos"][n] = {cid: nd[cid] for cid in ids if cid in keep}
    return data

def complexity_estimate(data):
    """
    估算目前模型主要變數數量（基於現行 build_model：x,y,s,z,Y）。
    注意：x, s 在現行 model.py 仍為 |G|*|M|*|Q|（未稀疏化）。
    """
    M = len(data["M"])
    Q = len(data["Q"])
    G = len(data["G"])
    z = sum(len(v) for v in data["combos"].values())
    x = G * M * Q
    s = G * M * Q
    y = G * Q
    Y = G
    return {
        "M": M, "Q": Q, "G": G,
        "z_vars": z,
        "x_vars": x,
        "s_vars": s,
        "y_vars": y,
        "Y_vars": Y,
        "total_vars_est": z + x + s + y + Y,
    }

def enforce_w_properties(data, strict=True, margin=1):
    """
    強制服務吞吐量表 W 具有：
    (A) 若 C_n 有包含多個服務的組合，則對其中每個服務 k 都有單獨組合 {k}
    (B) 單獨部署的吞吐量 W(n,{k},k) 嚴格為該節點上服務 k 的最高值
    - strict=True 則確保單獨部署嚴格大於其他組合（>）；否則僅 >=
    - margin：若需抬高單獨部署容量，使用加 margin 的方式
    """
    combos = data["combos"]
    Q = list(data["Q"])

    for n, node_combos in combos.items():
        # 1) 準備：找出每個服務目前的最大容量
        max_cap_by_service = {k: 0 for k in Q}
        for c_id, info in node_combos.items():
            for k, cap in info["W"].items():
                if cap > max_cap_by_service[k]:
                    max_cap_by_service[k] = cap

        # 2) 為每個被任何組合出現的服務補齊單獨組合
        services_present = set()
        for info in node_combos.values():
            services_present.update(info["services"])

        # 產生不衝突的單獨組合 ID
        def singleton_id(k):
            base = f"single_{k}"
            if base not in node_combos:
                return base
            # fallback：c{編號}
            idx = 1
            while f"c{idx}" in node_combos:
                idx += 1
            return f"c{idx}"

        # 建立或調整單獨組合
        for k in services_present:
            # 若不存在單獨組合，建立一個，容量先設為 max_cap+margin
            single_key = None
            for cid, info in node_combos.items():
                if set(info["services"]) == {k}:
                    single_key = cid
                    break
            if single_key is None:
                single_key = singleton_id(k)
                node_combos[single_key] = {"services": [k], "W": {k: max_cap_by_service[k] + max(1, margin)}}
            else:
                # 已存在：確保單獨容量 >= 目前最大，其餘清空
                current = node_combos[single_key]["W"].get(k, 0)
                target = max_cap_by_service[k] + (margin if strict else 0)
                if current < target:
                    node_combos[single_key]["W"][k] = target
                # 確保只有 k 這個 key
                node_combos[single_key]["services"] = [k]
                node_combos[single_key]["W"] = {k: node_combos[single_key]["W"][k]}

        # 3) 調整其他組合，使其對應服務容量不超過單獨容量（嚴格小於或小於等於）
        #    單獨容量更新後再做一輪 max
        single_cap = {}
        for cid, info in node_combos.items():
            if len(info["services"]) == 1:
                k = info["services"][0]
                single_cap[k] = info["W"][k]

        for cid, info in node_combos.items():
            if len(info["services"]) == 1:
                continue
            for k in list(info["services"]):
                if k not in info["W"]:
                    continue
                cap = info["W"][k]
                cap_max = single_cap.get(k, cap)
                limit = cap_max - (1 if strict else 0)
                if cap > limit:
                    info["W"][k] = max(0, limit)

    return data

def create_random_data(
    num_nodes=5,
    num_services=4,
    num_agents=60,
    combos_per_node=6,
    avg_services_per_combo=2.5,
    min_req_services=2,
    max_req_services=3,
    capacity_low=50,
    capacity_high=300,
    req_pattern="random",
    seed=42,
    drop_dominated=True,
    base_caps=None,  # 新增：{q1:100, q2:220, ...}
    interference_mode="linear",  # "linear" 或 "random"
    linear_factor=0.15,          # linear：每多一服務衰減 15%
    interference_range=(0.6, 0.9),  # random：在此比例 * 基礎值之間
    fl_ratio=0.08,               # 新增：最低頻率比例
    fh_ratio=0.16                # 新增：最高頻率比例
):
    """
    base_caps: dict 指定單服務部署時的基礎吞吐量 W。若為 None 則隨機生成於 [capacity_low, capacity_high]
    多服務組合吞吐量規則：
      - linear: W(k | S) = base_caps[k] * (1 - linear_factor * (|S|-1))
      - random: W(k | S) = base_caps[k] * U(interference_range[0], interference_range[1])
    最後強制：
      - 單服務 combo 容量 >= 任何包含該服務之多服務 combo（若 strict 修正）
    """
    import random
    random.seed(seed)

    Q = [f"q{i}" for i in range(1, num_services + 1)]
    if base_caps is None:
        base_caps = {
            k: random.randint(capacity_low, capacity_high)
            for k in Q
        }
    else:
        # 補齊缺失
        for k in Q:
            if k not in base_caps:
                base_caps[k] = random.randint(capacity_low, capacity_high)

    # 產生節點集合 / 服務 / 使用者
    M = [f"m{i}" for i in range(1, num_nodes + 1)]
    G = [f"g{i}" for i in range(1, num_agents + 1)]

    # 使用者需求集合 R
    R = {}
    for g in G:
        need_cnt = random.randint(min_req_services, max_req_services)
        need_cnt = min(need_cnt, len(Q))  # 確保不超過可用服務數量
        if req_pattern == "front_heavy":
            pool = Q[: max(2, len(Q)//2)]
            R[g] = random.sample(pool, min(need_cnt, len(pool)))
        elif req_pattern == "cycle":
            idx = int(g[1:]) % len(Q)
            arr = Q[idx:] + Q[:idx]
            R[g] = arr[:need_cnt]
        else:  # random
            R[g] = random.sample(Q, need_cnt)

    # 服務頻率門檻（比例可由 config 調整）
    f_l = {k: int(base_caps[k] * fl_ratio) for k in Q}
    f_h = {k: int(base_caps[k] * fh_ratio) for k in Q}

    # 節點 combo 生成
    combos = {}
    for n in M:
        combos[n] = {}
        target = combos_per_node
        tried = 0
        c_id = 1

        # 確保所有單服務存在
        for k in Q:
            combos[n][f"c{c_id}"] = {
                "services": [k],
                "W": {k: base_caps[k]}
            }
            c_id += 1

        while len(combos[n]) < target and tried < target * 4:
            tried += 1
            size = max(2, int(random.gauss(avg_services_per_combo, 0.8)))
            size = min(size, len(Q))
            svc_set = sorted(random.sample(Q, size))
            key = tuple(svc_set)
            if any(set(svc_set) == set(info["services"]) for info in combos[n].values()):
                continue

            W_entry = {}
            for k in svc_set:
                if interference_mode == "linear":
                    degr = max(0.2, 1 - linear_factor * (len(svc_set) - 1))
                    W_entry[k] = int(base_caps[k] * degr)
                else:
                    r = random.uniform(*interference_range)
                    W_entry[k] = int(base_caps[k] * r)
            combos[n][f"c{c_id}"] = {"services": svc_set, "W": W_entry}
            c_id += 1

    # 修正：保證單服務容量 >= 多服務版本
    for n in M:
        single_caps = {info["services"][0]: info["W"][info["services"][0]]
                       for cid, info in combos[n].items() if len(info["services"]) == 1}
        for cid, info in combos[n].items():
            if len(info["services"]) > 1:
                for k in info["services"]:
                    if info["W"][k] > single_caps[k]:
                        info["W"][k] = single_caps[k]

    data = {
        "M": M,
        "Q": Q,
        "G": G,
        "R": R,
        "combos": combos,
        "f_l": f_l,
        "f_h": f_h,
        "base_caps": base_caps
    }

    # 可選：移除被支配組合
    if drop_dominated:
        filter_dominated_combos(data)

    return data

def load_data_from_json(path):
    """從 JSON 檔載入 data 結構"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
