# 如何執行 5-2-7 節「演算法複雜度與求解品質驗證」實驗

對應論文 5-2-7 節：GA（本研究方法）vs MINLP 精確解（Pyomo/HiGHS）之求解品質與耗時比較。
腳本：`experiments/complexity_experiment_4_3_5.py`。

本文件記錄實際安裝、修正路徑並跑通一次縮小規模測試的完整過程（2026-09-02驗證）。

## 1. 環境需求

此腳本需要 `pyomo` 與 `highspy`（HiGHS 求解器的 Python binding），**與其餘實驗腳本使用的
Python 環境分開**，建議另建一個獨立 venv：

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r experiments/minlp/requirements.txt
```

已驗證版本：`pyomo==6.9.5`、`highspy==1.12.0`。

## 2. 目錄結構前提

腳本以 `Path(__file__).resolve().parent.parent / "controller"` 定位到 `controller/`
目錄（用以 import `bench_ga_v2_CUR.py` 並讀取 `controller/information/` 底下的種子資料），
以 `Path(__file__).resolve().parent / "minlp"` 定位到同目錄下的 `minlp/` 子目錄
（`model.py`／`run.py`／`params.py`／`config.py`，MINLP 精確解模型定義）。

**因此執行時必須保持以下相對關係**（複製本 repo 後不要任意搬動子目錄）：

```
experiments/
├── complexity_experiment_4_3_5.py
└── minlp/
    ├── model.py
    ├── run.py
    ├── params.py
    └── config.py
../controller/
├── bench_ga_v2_CUR.py
└── information/
    ├── aws_prod_serviceSpec_mul.json
    ├── complexity_experiment_model.json
    └── complexity_experiment_subscription_patterns.json
```

> 原始開發環境路徑寫死為 `/home/hiro/git_repo/minlp`，交接時已修正為上述相對路徑。

## 3. 執行方式

```bash
cd experiments
python3 complexity_experiment_4_3_5.py            # 預設 group_size=12（情境A，3服務，36人；
                                                    # 論文實際情境A為72人，需帶入 group_size=24）
python3 complexity_experiment_4_3_5.py 24          # 情境A：3服務，72人（對應論文表15/16）
python3 complexity_experiment_4_3_5.py 16 --4services   # 情境B：4服務，96人（對應論文表17）
```

輸出：
- `controller/information/complexity_experiment_4_3_5_result_n{total_agents}{tag}.json`
- 終端機會印出 GA 各世代數之 N_sat/Q 平均與標準差，以及 MINLP 各checkpoint時間點之最佳可行解

**注意**：預設 `CHECKPOINT_TIMES=[1,10,60,300,600]`（秒）、`OUTER_TRIALS=10`、`N_SEEDS=5`，
完整跑一次情境（含600秒MINLP求解）需要約10-15分鐘，屬正常現象、非腳本卡住。

## 4. 已驗證可運作（縮小規模整合測試，2026-09-02）

為快速驗證環境與路徑設定正確（不需等待完整600秒求解），可比照以下方式，將
`CHECKPOINT_TIMES`／`N_SEEDS`／`OUTER_TRIALS`／`group_size`／GA世代數暫時調小：

```python
import sys
sys.path.insert(0, "experiments")
import complexity_experiment_4_3_5 as m

m.CHECKPOINT_TIMES = [1, 3]
m.N_SEEDS = 2
m.OUTER_TRIALS = 1

ga_model, minlp_data = m.build_shared_data(group_size=4)   # 真實資料，僅縮小人數
# ... 呼叫 m.run_ga_sweep(...) 與 m.run_minlp_with_checkpoints(...) 驗證流程
```

此縮小規模測試已於2026-09-02實際執行成功：真實資料載入、GA sweep、MINLP checkpoint
求解三個環節皆正常運作（7節點、3服務、12 agent，MINLP於1秒內即找到最佳解12）。

**執行測試後請注意**：若直接呼叫 `build_shared_data()` 並將結果寫入
`GA_MODEL_OUT_PATH`（即 `controller/information/complexity_experiment_model.json`），
會覆寫論文正式使用的種子檔案。測試後若要復原，需重新從版本庫或原始來源取回該檔案。
建議測試時另存到其他檔名，不要直接覆寫種子檔案。
