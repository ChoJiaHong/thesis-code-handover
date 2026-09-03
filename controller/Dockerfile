FROM python:3.10.13-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# 核心程式
COPY controller_v2.py ./
COPY bench_ga_v2_CUR.py ./
# optimizer.py：未收錄於本交接包（舊版/已由上述solver模組取代之探索性程式碼，
# controller_v2.py 現行版本未import此模組，已排除）

# Solver 模組
# - usc_ts_solver   : USC-TS Knapsack 部署求解器（EMS / ems_dl3 / ems_rr 使用）
# - svc_updating_decision : SUD Q-learning 增量部署決策（EMS 使用）
# - lsr_solver      : LSR two-stage heuristic（Baseline 2）
# - minlp_solver    : 精確 MILP 聯合優化求解器（Pyomo + HiGHS，minlp 模式使用，作為對照 Oracle）
# - hudson_solver   : Hudson 貪婪邊際增量部署+分配（Baseline 4，單服務獨立容量版）
# - lai_eua_solver  : Lai EUA 貪婪裝箱部署+分配（Baseline 5）
#
# 以下兩個模組未收錄於本交接包，故不COPY：
# - simple_baseline_solver : LSR_v2 / EMS_v2 簡化版部署+分配邏輯（可控實驗用，非論文最終採用邏輯，已排除）
# - hudson_combo_solver    : Hudson 共置感知容量對照版（僅供新舊容量模型比較用之探索性程式碼，已排除）
COPY usc_ts_solver.py ./
COPY svc_updating_decision.py ./
COPY lsr_solver.py ./
COPY minlp_solver.py ./
COPY hudson_solver.py ./
COPY lai_eua_solver.py ./

# Q-table 預訓練工具（可在 Pod 內執行以重新訓練）
COPY pretrain_sud.py ./

# 靜態設定檔（由 init container 在首次啟動時寫入 PVC；PVC 存在則跳過）
# - controller_config.json : solver_mode / admission_control / minlp 求解參數 預設值
# - usc_ts_config.json     : USC-TS uniformCapacity / nodeCapacity
# - serviceSpec_mul.json   : 2-node 生產環境服務規格與離線量測值
# - q_table.json           : 預訓練 Q-table（PVC 覆蓋以保留線上學習結果）
# serviceSpec_mul_8node.json（8-node測試環境用之替代規格檔）未收錄於本交接包，已排除
COPY information/controller_config.json ./information/
COPY information/usc_ts_config.json ./information/
COPY information/serviceSpec_mul.json ./information/
COPY information/q_table.json ./information/

# Kubernetes Pod 部署 YAML 範本（controller 動態產生 Pod 時使用）
COPY service_yaml ./service_yaml

RUN mkdir -p ./logdir

CMD ["uvicorn", "controller_v2:app", "--host", "0.0.0.0", "--port", "5000"]