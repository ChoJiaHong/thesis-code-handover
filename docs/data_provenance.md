# 實驗資料來源對照表

`data/` 目錄收錄論文實驗之**全部重跑次數**原始CSV（2026-09-02更新：先前版本僅各挑一份代表，
現已依使用者要求補齊所有重複實驗）。檔名保留原始時間戳格式，方便對照
`data_mapping_raw_notes.txt`（使用者原始筆記）與`plotting_guide_raw.txt`。

## 主要比較圖（`plotting/plot_compare.py` 產生）

| 資料夾 | 對應論文圖表 | 對應章節 | 各方法重跑次數 |
|---|---|---|---|
| `data/exp1_動態日常_fig5/` | 圖5 | 5-2-3 實驗一 | ga_bf: 3次／ga_bf_no: 2次／ems: 3次／lsr: 3次 |
| `data/exp2_高負載_fig6/` | 圖6 | 5-2-4 實驗二 | ga_bf: 3次／ga_bf_no: 3次／ems: 3次／lsr: 3次 |
| `data/exp3_節點故障_fig7/` | 圖7 | 5-2-5 實驗三 | ga_bf: 3次／ga_bf_no: 3次／ems: 2次／lsr: 1次 |

每個方法子資料夾內為該方法在該情境下的全部重跑CSV（檔名=原始時間戳）。使用者原始筆記
（`data_mapping_raw_notes.txt`）自述「每個方法下都有做三次實驗，有些只有兩次」，上表次數不對稱
為真實紀錄，非本次整理遺漏。

論文正文圖5/6/7之折線圖，實際採用各方法之其中一次重跑結果（並非跨重跑平均）；若該方法有多次
重跑，資料夾內對應時間戳最接近筆記中「最終定案」欄位者即為論文採用版本（見
`data_mapping_raw_notes.txt`原始編號）。其餘重跑保留供交叉驗證重現性、抽樣檢查一致性之用。

重現指令範例（使用任一重跑之CSV）：

```bash
cd plotting
python3 plot_compare.py \
  ga_bf:../data/exp1_動態日常_fig5/ga_bf/s1_20260720_010431.csv \
  ems:../data/exp1_動態日常_fig5/ems/s1_20260720_013211.csv \
  lsr:../data/exp1_動態日常_fig5/lsr/s1_20260720_015906.csv \
  --until=450 \
  fig5_reproduced.png
```

各情境對應的 `--until`（統計時間窗口秒數）：實驗一 450、實驗二 390、實驗三 230。

## 逐服務圖（`plotting/plot_trace.py` 產生，對應 fig5b/c/d、fig6b/c/d、fig7b/c/d）

對任一CSV個別執行 `plot_trace.py` 即可產生該次重跑之逐服務 Effective FPS／延遲／Invalid
FPS／節點部署 Gantt 圖：

```bash
python3 plotting/plot_trace.py data/exp1_動態日常_fig5/ga_bf/s1_20260720_010431.csv output/fig5b_reproduced.png
```

## 消融實驗（過渡期SLA維持機制，對應論文5-2-6節、圖 fig_ablation1/2）

| 資料夾 | 對應論文圖表 | 對應章節 |
|---|---|---|
| `data/ablation1_動態日常消融/` | fig_ablation1 | 5-2-6-1 |
| `data/ablation2_高負載消融/` | fig_ablation2 | 5-2-6-2（與`exp2_高負載_fig6/ga_bf`／`ga_bf_no`之第一次重跑為同一份資料，此處另存一份方便單獨取用）|

```bash
python3 plotting/plot_compare.py \
  ga_bf:../data/ablation1_動態日常消融/有過渡期機制_s1_20260721_210126.csv \
  ga_bf_no:../data/ablation1_動態日常消融/無過渡期機制_s1_20260721_211111.csv \
  --until=450 \
  fig_ablation1_reproduced.png
```

## 已知的資料標記問題（2026-09-02，供未來查核）

- 使用者原始筆記「實驗3」段落中，`ems`與`lsr`各自的「1.」項目所指路徑，實際內容與「實驗1」段落的
  `ems`/`lsr`「2.」/「1.」項目路徑完全相同（時間戳皆為`s1_20260720_012241`／`s1_20260720_015032`），
  判斷為使用者手動整理筆記時的複製貼上殘留，**非實驗3本身的資料**。本次整理已排除這兩筆、僅保留
  實驗3筆記中明確標示為該情境的其餘重跑（ems 2筆、lsr 1筆）。如有疑義請對照
  `data_mapping_raw_notes.txt` 原文第44-52行。

## 重要提醒

- `result_aws/` 原始目錄底下還有數百個其他CSV，是實驗過程中的探索性重跑或未採用的baseline
  （Hudson/Lai-EUA等），與本論文最終數字無關，未收錄於本次交接。
- 上述CSV之情境歸屬經交叉比對使用者原始筆記與shell操作紀錄之重複模式後確認，並非100%具備
  原始程式自動化的可追溯標記（CSV內部無「此為論文最終版」欄位），若之後對數字有疑義，建議以
  本文件記錄的來源為準。
