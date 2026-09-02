from pyomo.environ import (
    ConcreteModel, Set, Param, Var, Constraint, Objective,
    Binary, NonNegativeReals, maximize, Expression
)


def build_model(data):
    """
    根據給定參數資料建立 Pyomo ConcreteModel。

    期待的 data 結構：
    data = {
        "M": ["m1", "m2", ...],          # 節點集合
        "Q": ["q1", "q2", ...],          # 服務集合
        "G": ["g1", "g2", ...],          # Agent 集合
        "R": {                           # 每個 Agent 所需服務集合 R(g_i)
            "g1": ["q1", "q2"],
            ...
        },
        "combos": {                      # 每個節點可部署的服務組合 C_n
            "m1": {
                "c1": {
                    "services": ["q1"],
                    "W": {"q1": 100}
                },
                ...
            },
            ...
        },
        "f_l": {"q1": 10, ...},          # 最低頻率門檻 f_k^l
        "f_h": {"q1": 20, ...},          # 標準頻率上限 f_k^h
    }
    """
    model = ConcreteModel()

    # --------------------
    # 1. Sets
    # --------------------
    M_list = list(data["M"])
    Q_list = list(data["Q"])
    G_list = list(data["G"])
    combos_data = data["combos"]   # dict: node -> combo_id -> {services, W}

    model.M = Set(initialize=M_list, doc="節點集合 M")
    model.Q = Set(initialize=Q_list, doc="服務集合 Q")
    model.G = Set(initialize=G_list, doc="Agent 集合 G")

    # R(g_i) : 每個 Agent 所需服務集合
    def R_init(model, i):
        return data["R"].get(i, [])
    model.R = Set(model.G, within=model.Q, initialize=R_init, doc="每個 Agent 所需服務集合 R(g_i)")

    # Z_INDEX: (n, c) 的索引集合，代表節點 n 的一個部署組合 c
    def Z_INDEX_init(model):
        for n, combo_dict in combos_data.items():
            for c in combo_dict.keys():
                yield (n, c)

    model.Z_INDEX = Set(dimen=2, initialize=Z_INDEX_init, doc="(節點,部署組合) 索引集合")

    # GK_R: (i,k) 之稀疏索引集合，僅包含 k in R(i) 者。x/s/f 若對每個 agent 之
    # 「未訂閱」服務也建立變數，會產生大量與目標函數、可行性完全無關的自由二元
    # 變數（設為0或1皆不影響目標，只受限於 x<=d 這種鬆散上界），使搜尋空間中充滿
    # 對稱、無意義的分支，讓 B\&B 求解器浪費大量時間在剪枝這些變數上，而非真正
    # 逼近最優解。以稀疏索引集合直接排除這些變數（而非建立後再用約束鎖死為0），
    # 可讓變數規模與求解效率大幅改善，且不改變原模型之可行解與最優解本身。
    def GK_R_init(model):
        for i in G_list:
            for k in data["R"].get(i, []):
                yield (i, k)
    model.GK_R = Set(dimen=2, initialize=GK_R_init, doc="(Agent,服務) 稀疏索引集合，僅含 k in R(i)")

    # GNK_R: (i,n,k) 之稀疏索引集合，僅包含 k in R(i) 者，x/s 之定義域。
    def GNK_R_init(model):
        for i in G_list:
            for k in data["R"].get(i, []):
                for n in M_list:
                    yield (i, n, k)
    model.GNK_R = Set(dimen=3, initialize=GNK_R_init, doc="(Agent,節點,服務) 稀疏索引集合，僅含 k in R(i)")

    # --------------------
    # 2. Parameters
    # --------------------
    # f_k^l, f_k^h
    model.f_l = Param(model.Q, initialize=data["f_l"], within=NonNegativeReals, doc="最低服務頻率門檻 f_k^l")
    model.f_h = Param(model.Q, initialize=data["f_h"], within=NonNegativeReals, doc="標準頻率上限 f_k^h")

    # W((n,c), k) 與 HasService((n,c), k)
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
        model.Z_INDEX, model.Q,
        initialize=W_init,
        default=0.0,
        within=NonNegativeReals,
        doc="節點 n 在部署組合 c 下，對服務 k 可提供的最大吞吐量 W(n,c,k)"
    )
    model.HasService = Param(
        model.Z_INDEX, model.Q,
        initialize=HasService_init,
        default=0,
        within=NonNegativeReals,   # 實際為 0/1，用於條件判斷
        doc="指示服務 k 是否包含在部署組合 (n,c) 中的 0/1 參數"
    )

    # --------------------
    # 3. Decision Variables
    # --------------------
    # z_{n,c}
    model.z = Var(model.Z_INDEX, domain=Binary, doc="若節點 n 選擇部署組合 c 則 z_{n,c}=1")

    # 刪除：d 變數，改為 Expression（見下方）
    # model.d = Var(model.M, model.Q, domain=Binary)

    # x_{i,n,k}：僅在 k in R(i) 時才建立，見上方 GNK_R 之說明
    model.x = Var(model.GNK_R, domain=Binary, doc="若 Agent i 連到節點 n 的服務 k 則 x_{i,n,k}=1（僅 k in R(i)）")

    # s_{i,n,k}：僅在 k in R(i) 時才建立
    model.s = Var(model.GNK_R, domain=NonNegativeReals,
                  doc="Agent i 在節點 n 上獲得服務 k 的頻率（僅 k in R(i)）")

    # 刪除：f 變數，改為 Expression（見下方）
    # model.f = Var(model.G, model.Q, domain=NonNegativeReals)

    # y_{i,k}：僅在 k in R(i) 時才建立
    model.y = Var(model.GK_R, domain=Binary, doc="若 Agent i 在服務 k 上達到最低頻率門檻則 y_{i,k}=1（僅 k in R(i)）")

    # Y_i
    model.Y = Var(model.G, domain=Binary, doc="若 Agent i 所需的所有服務皆達到 QoS 則 Y_i=1")

    # --------------------
    # 3.1 Derived Expressions
    # --------------------
    # d_{n,k} := sum_{c in C_n, k in c} z_{n,c}
    def d_expr_rule(model, n, k):
        return sum(
            model.z[n, c]
            for (nn, c) in model.Z_INDEX
            if nn == n and model.HasService[(n, c), k] >= 0.5
        )
    model.d = Expression(model.M, model.Q, rule=d_expr_rule)

    # f_{i,k} := sum_n s_{i,n,k}，僅對 (i,k) in GK_R 定義（對應 x/s 之稀疏定義域）
    def f_expr_rule(model, i, k):
        return sum(model.s[i, n, k] for n in model.M)
    model.f = Expression(model.GK_R, rule=f_expr_rule)

    # --------------------
    # 4. Constraints
    # --------------------

    # (1) 每個節點「至多」選擇一個部署組合
    def node_choose_one_combo_rule(model, n):
        combos_n = [c for (nn, c) in model.Z_INDEX if nn == n]
        return sum(model.z[n, c] for c in combos_n) <= 1
    model.NodeChooseOneCombo = Constraint(model.M, rule=node_choose_one_combo_rule)

    # (2) 指派只可到有部署之節點：x_{i,n,k} <= d_{n,k}（僅 (i,n,k) in GNK_R）
    def x_leq_d_rule(model, i, n, k):
        return model.x[i, n, k] <= model.d[n, k]
    model.Link_x_d = Constraint(model.GNK_R, rule=x_leq_d_rule)

    # 刪除：部署一致性（d 的定義已改為 Expression）
    # model.DeployConsistency = Constraint(...)

    # (3) 服務唯一指派（與達標指標一致）：sum_n x_{i,n,k} = y_{i,k}，(i,k) in GK_R
    def single_node_per_service_rule(model, i, k):
        return sum(model.x[i, n, k] for n in model.M) == model.y[i, k]
    model.SingleNodePerService = Constraint(model.GK_R, rule=single_node_per_service_rule)

    # (4) 容量限制：sum_i s_{i,n,k} <= sum_c W(n,c,k) z_{n,c}（左式僅加總 (i,n,k) in GNK_R 者）
    def capacity_rule(model, n, k):
        left = sum(model.s[i, n, k] for i in model.G if (i, n, k) in model.GNK_R)
        right = sum(
            model.W[(n, c), k] * model.z[n, c]
            for (nn, c) in model.Z_INDEX if nn == n
        )
        return left <= right
    model.Capacity = Constraint(model.M, model.Q, rule=capacity_rule)

    # (5) s 與指派連結：s_{i,n,k} <= f_k^h * x_{i,n,k}（僅 (i,n,k) in GNK_R）
    def s_leq_fx_rule(model, i, n, k):
        return model.s[i, n, k] <= model.f_h[k] * model.x[i, n, k]
    model.SxLink = Constraint(model.GNK_R, rule=s_leq_fx_rule)

    # 刪除：f 定義與連結（已以 Expression 定義，且由 SxLink 蘊含）
    # model.FreqDef = Constraint(...)
    # model.FreqConnection = Constraint(...)

    # (6) QoS 門檻（以 f 的表達式表示），(i,k) in GK_R
    def qos_lower_rule(model, i, k):
        return model.f[i, k] >= model.f_l[k] * model.y[i, k]

    def qos_upper_rule(model, i, k):
        return model.f[i, k] <= model.f_h[k] * model.y[i, k]

    model.QoSLower = Constraint(model.GK_R, rule=qos_lower_rule)
    model.QoSUpper = Constraint(model.GK_R, rule=qos_upper_rule)

    # (7) 使用者整體 QoS
    def user_qos_upper_rule(model, i, k):
        return model.Y[i] <= model.y[i, k]
    model.UserQoSUpper = Constraint(model.GK_R, rule=user_qos_upper_rule)

    def user_qos_lower_rule(model, i):
        R_i = list(model.R[i])
        if len(R_i) == 0:
            return model.Y[i] == 0
        return model.Y[i] >= sum(model.y[i, k] for k in R_i) - (len(R_i) - 1)
    model.UserQoSLower = Constraint(model.G, rule=user_qos_lower_rule)

    # --------------------
    # 5. Objective
    # --------------------
    def objective_rule(model):
        # 主目標：最大化達成最低服務品質之使用者數量
        return sum(model.Y[i] for i in model.G)
    model.Obj = Objective(rule=objective_rule, sense=maximize)

    return model
