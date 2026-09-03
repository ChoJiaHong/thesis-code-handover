# 論文程式碼與實驗資料交接

本 repo 為碩士論文「邊緣AI推論服務之QoS門檻達標人數最大化之聯合優化機制」之系統實作、
實驗腳本與實驗資料交接封裝。內容經過整理，排除了開發過程中的探索性版本、過時分支與未採用
之baseline，僅保留與論文最終版本直接相關的程式碼與資料。

## 系統架構

系統分為使用者端與邊緣伺服器端兩層（對應論文圖1、圖8）：

| 元件 | 目錄 | 角色 |
|---|---|---|
| Controller | `controller/` | 核心決策模組：系統模型求解（GA）、EMS/LSR baseline、准入決策、狀態轉換 |
| AgentManager | `agentmanager/` | 接收使用者訂閱請求，建立對應 Agent 容器 |
| Agent | `agent/` | 使用者端代理，依 Controller 指令控制請求路由與速率 |
| Monitor | `monitor/` | 監控各運算節點健康狀態 |
| 推論服務 (infer_server) | `infer_server/` | gesture／pose 推論服務實際執行程式（object 服務沿用 pose model，見下方說明）|
| AR Emulator | `ar_emulator/` | 實驗用使用者流量模擬器 |

實際運行時各元件之容器映像檔版本：`controller_v2:45`、`agentmanager_refact:12`、
`monitor_mul:2`、`infer_server:9`（gesture/pose/object 共用）。

**每個元件目錄下皆有一份`操作手冊.md`**，詳細記錄該元件之啟動方式、CLI參數、設定檔、
埠號與依賴關係：[`infra/`](infra/操作手冊.md)（叢集建置，需最先完成）、
[`controller/`](controller/操作手冊.md)、
[`agentmanager/`](agentmanager/操作手冊.md)、[`agent/`](agent/操作手冊.md)、
[`monitor/`](monitor/操作手冊.md)、[`infer_server/`](infer_server/操作手冊.md)、
[`ar_emulator/`](ar_emulator/操作手冊.md)、[`experiments/`](experiments/操作手冊.md)、
[`plotting/`](plotting/操作手冊.md)。

## 目錄結構

```
infra/            Kubernetes叢集建置、GPU Time-Slicing、Prometheus監控等基礎設施腳本
controller/       系統核心：GA求解器、EMS/LSR/MINLP baseline、部署yaml、種子設定
agentmanager/     AgentManager 程式與部署yaml
agent/            使用者端 Agent 程式與 gRPC proto stub
monitor/          Monitor 程式與部署yaml
infer_server/     AI推論服務（gesture/pose）程式與模型權重
ar_emulator/      實驗用 AR 使用者流量模擬器
experiments/      實驗控制腳本、離線壓測腳本、複雜度驗證腳本
plotting/         論文圖表繪製腳本（plot_compare.py、plot_trace.py）
data/             論文fig5/6/7與消融實驗fig_ablation1/2之原始CSV資料
docs/             部署指令、資料來源對照等說明文件
```

## 部署方式

參見 `docs/aws_deploy_commands.md`——記錄了原始 AWS 部署與 Kubernetes 叢集建置的實際指令
（機敏資訊如帳號、主機位址、kubeadm token 已用佔位符取代，需依實際環境自行填入）。

系統以 Kubernetes 部署，各元件之 `*-deployment.yaml`／`*-service.yaml` 位於對應目錄下；
`controller/service_yaml/`、`controller/deamonse_service/` 為 gesture/pose/object 三項推論服務
之部署設定。

## 如何重現論文實驗數字

參見 `docs/data_provenance.md`，詳列 `data/` 底下每份CSV對應論文哪一張圖表、如何用
`plotting/plot_compare.py`／`plotting/plot_trace.py` 重新繪製。

離線壓測（產生 `controller/information/serviceSpec_mul.json` 所需之服務容量表）之腳本為
`experiments/bench_workability.py`，對應論文4-1-2、4-1-10節與表5。

## 已知限制與說明

1. **object 推論服務並無獨立模型程式碼**：實驗設計上，"object" 服務直接沿用 pose
   model（`infer_server/service/pose_service_no_batch.py`，已由部署yaml之
   `SERVICE: "pose"`環境變數證實），在實驗中僅被視為一個獨立的邏輯服務
   （已於論文口試前告知指導教授），並非程式碼缺漏。
2. **`infer_server/` 未包含第三方物件偵測函式庫 `vision/`**：`gesture_service.py`
   背後依賴之 SSD 偵測模型架構取自開源 pytorch-ssd 相關實作，未隨本 repo一併提供，
   上游來源連結亦不提供，使用前需自行尋找相容之pytorch-ssd實作原始碼並放置於
   `infer_server/vision/`。
3. **`experiments/complexity_experiment_4_3_5.py`（對應論文5-2-7節GA vs精確解比較）**
   依賴 `experiments/minlp/`（`model.py`／`run.py`／`params.py`／`config.py`，已一併包含，
   從原本7.9GB的獨立`minlp/`專案中僅抽取此腳本實際用到的4個檔案，其餘如PPO/Lagrangian/
   Bender's分解等探索性內容與該專案的完整`venv`未納入）。需另外用
   `experiments/minlp/requirements.txt` 建立獨立 venv 安裝 Pyomo/HiGHS。
   已實際驗證可運作，完整操作說明見 `docs/how_to_run_complexity_experiment.md`。
4. **`controller/information/` 僅保留系統啟動所需之種子設定**（`serviceSpec_mul.json`、
   `usc_ts_config.json`、`q_table.json`等），執行期間才會產生/更新的狀態檔
   （節點狀態、訂閱狀態等）未包含，需視實際部署環境重新產生。
5. **`data/` 收錄與論文最終圖表對應之CSV**（實驗一、二各方法2~3次重跑；實驗三因論文採單次
   代表性數據〔非跨重跑平均〕，各方法僅收錄該單一對應版本，詳見
   `docs/data_provenance.md`），原始AWS實驗過程中產生的數百份探索性/未採用baseline
   之CSV未納入，如需完整實驗歷程請另行取得 `result_aws/` 原始資料。
6. **`controller/minlp_solver.py`**：`controller_v2.py`執行期若切換至MINLP精確解
   solver_mode會動態import此檔案，需另外安裝pyomo/highs；與`experiments/minlp/`
   （供離線複雜度驗證用）同源但為獨立維護的兩份程式碼。
7. **`controller/hudson_solver.py`、`controller/lai_eua_solver.py`**：Baseline
   4（Hudson等人之QoS-aware Edge AI Placement and Scheduling移植）與Baseline
   5（Lai等人之向量裝箱式EUA移植），為`論文_全文_延伸版.tex`（新增兩基準之延伸版論文，
   與本交接包對應之正式版論文分屬不同文件）比較之對象；正式版論文僅比較EMS、LSR，
   `data/`目錄之實驗CSV亦僅涵蓋此二者，未包含Hudson/Lai-EUA之實驗數據。
