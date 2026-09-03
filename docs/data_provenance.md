# 實驗資料來源對照表

`data/` 目錄收錄論文實驗之**全部重跑次數**原始CSV（2026-09-02更新：先前版本僅各挑一份代表，
現已依使用者要求補齊所有重複實驗）。檔名保留原始時間戳格式，方便對照
`data_mapping_raw_notes.txt`（使用者原始筆記）與`plotting_guide_raw.txt`。

## 主要比較圖（`plotting/plot_compare.py` 產生）

| 資料夾 | 對應論文圖表 | 對應章節 | 各方法重跑次數 |
|---|---|---|---|
| `data/exp1_動態日常_fig5/` | 圖5 | 5-2-3 實驗一 | ga_bf: 3次／ga_bf_no: 2次／ems: 3次／lsr: 3次 |
| `data/exp2_高負載_fig6/` | 圖6 | 5-2-4 實驗二 | ga_bf: 3次／ga_bf_no: 3次／ems: 3次／lsr: 3次 |
| `data/exp3_節點故障_fig7/` | 圖7 | 5-2-5 實驗三 | ga_bf: 1次／ems: 1次／lsr: 1次（見下方2026-09-03更正說明） |

`exp1`／`exp2`每個方法子資料夾內為該方法在該情境下的全部重跑CSV（檔名=原始時間戳）。使用者原始筆記
（`data_mapping_raw_notes.txt`）自述「每個方法下都有做三次實驗，有些只有兩次」，上表次數不對稱
為真實紀錄，非本次整理遺漏。`exp3`則不同：論文表~11（故障前後 $A_{sat}$ 相對變化）本身即為單次代表
性數據、非跨重跑平均，故`exp3`資料夾僅收錄該單一對應版本，非本次整理遺漏。

論文正文圖5/6之折線圖，實際採用各方法之其中一次重跑結果（並非跨重跑平均）；若該方法有多次
重跑，資料夾內對應時間戳最接近筆記中「最終定案」欄位者即為論文採用版本（見
`data_mapping_raw_notes.txt`原始編號）。其餘重跑保留供交叉驗證重現性、抽樣檢查一致性之用。圖7
則因故障事件為人工手動觸發、且論文採單次代表性數據，僅有一份對應版本，見下方2026-09-03更正說明。

重現指令範例（使用任一重跑之CSV）：

```bash
cd plotting
export PYTHONPATH=../experiments
python3 plot_compare.py \
  ga_bf:../data/exp1_動態日常_fig5/ga_bf/s1_20260720_010431.csv \
  ems:../data/exp1_動態日常_fig5/ems/s1_20260720_013211.csv \
  lsr:../data/exp1_動態日常_fig5/lsr/s1_20260720_015906.csv \
  --until=450 \
  fig5_reproduced.png
```

圖7（實驗三）之重現方式不同於圖5/6：三方法之故障觸發時間點各異，需先以各自CSV之`node_status`
欄位找出首次出現非`healthy`節點的資料列（即該方法自身之故障偵測時間點），將該方法之`elapsed_s`
欄位減去該時間點後再繪圖，使三方法以「故障發生後之相對時間」對齊比較，而非採用同一絕對時間軸：

```bash
cd plotting
export PYTHONPATH=../experiments
python3 plot_compare.py \
  ga_bf:../data/exp3_節點故障_fig7/ga_bf/s2_20260721_213143.csv \
  ems:../data/exp3_節點故障_fig7/ems/s2_20260721_215459.csv \
  lsr:../data/exp3_節點故障_fig7/lsr/s2_20260721_214938.csv \
  fig7_reproduced.png
# 注意：此指令直接繪圖為絕對時間軸版本，僅供快速檢視資料是否正確；
# 若要重現論文圖7之故障對齊版本，須先對每份CSV做上述elapsed_s平移前處理。
```

各情境對應的 `--until`（統計時間窗口秒數）：實驗一 450、實驗二 390、實驗三 230。

## 逐服務圖（`plotting/plot_trace.py` 產生，對應 fig5b/c/d、fig6b/c/d、fig7b/c/d）

對任一CSV個別執行 `plot_trace.py` 即可產生該次重跑之逐服務 Effective FPS／延遲／Invalid
FPS／節點部署 Gantt 圖：

```bash
PYTHONPATH=experiments python3 plotting/plot_trace.py data/exp1_動態日常_fig5/ga_bf/s1_20260720_010431.csv output/fig5b_reproduced.png
```

## 消融實驗（過渡期SLA維持機制，對應論文5-2-6節、圖 fig_ablation1/2）

| 資料夾 | 對應論文圖表 | 對應章節 |
|---|---|---|
| `data/ablation1_動態日常消融/` | fig_ablation1 | 5-2-6-1 |
| `data/ablation2_高負載消融/` | fig_ablation2 | 5-2-6-2（與`exp2_高負載_fig6/ga_bf`／`ga_bf_no`之第一次重跑為同一份資料，此處另存一份方便單獨取用）|

```bash
PYTHONPATH=experiments python3 plotting/plot_compare.py \
  ga_bf:data/ablation1_動態日常消融/有過渡期機制_s1_20260721_210126.csv \
  ga_bf_no:data/ablation1_動態日常消融/無過渡期機制_s1_20260721_211111.csv \
  --until=450 \
  fig_ablation1_reproduced.png
```

## 已知的資料標記問題（2026-09-02，供未來查核）

- 使用者原始筆記「實驗3」段落中，`ems`與`lsr`各自的「1.」項目所指路徑，實際內容與「實驗1」段落的
  `ems`/`lsr`「2.」/「1.」項目路徑完全相同（時間戳皆為`s1_20260720_012241`／`s1_20260720_015032`），
  判斷為使用者手動整理筆記時的複製貼上殘留，**非實驗3本身的資料**。如有疑義請對照
  `data_mapping_raw_notes.txt` 原文第44-52行。

## 更正：exp3 資料夾先前收錄錯誤批次（2026-09-03）

先前版本之`exp3_節點故障_fig7/`（`ga_bf`/`ems`/`lsr`，含已移除之`ga_bf_no`子資料夾）收錄的是
`data_mapping_raw_notes.txt`「實驗3」段落所列、時間戳為**2026-07-19**的一批CSV，經重新查核發現
**這批資料的使用者峰值人數為36人**（`n_users`欄位），與論文表~11（實驗三故障前後
$A_{sat}$相對變化）所述「故障前24人」之敘述及具體數字（24→8／24→3／24→4）皆不吻合——判斷這批
07-19資料對應的是一個更早、後來未採用之36人版本實驗設計，`data_mapping_raw_notes.txt`「實驗3」
段落記錄的很可能正是這個被放棄的版本，而非論文最終採用之版本。

真正對應論文表~11數字的，是同三個方法、但**時間戳為2026-07-21**的另一批24人資料，確認依據：
1. 各CSV之`node_status`欄位顯示故障期間關閉的節點集合為`{ip-172-31-0-204, ip-172-31-1-171,
   ip-172-31-10-12}`（ga_bf、ems皆同；lsr因偵測窗口較短僅捕捉到其中1個），且故障前之
   $A_{sat}$（以`per_user_detail`重新計算之實測值，而非CSV原始`nsats`欄位之solver名義決策）
   確實在24附近，故障後之谷底值精確對應24→8／24→3／24→4。
2. 與`plotting_guide_raw.txt`中殘留的`plot_trace.py`歷史指令逐一比對，`s2_20260721_213143.csv`
   （ga_bf）、`s2_20260721_215459.csv`（ems）、`s2_20260721_214938.csv`（lsr）三份檔案之輸出檔名
   分別為`s3_ga_trace.png`／`s3_ems_trace.png`／`s3_lsr_trace.png`，與論文圖7b/c/d之來源吻合。

`exp3_節點故障_fig7/`資料夾已更新為這三份正確檔案（`ga_bf_no`子資料夾因節點故障情境不納入消融
比較〔見論文5-2-6節〕、原內容亦屬07-19錯誤批次，已一併移除，非本情境所需）。若後續需要圖7之
「故障對齊」重現版本，須先對這三份CSV各自之`elapsed_s`做平移前處理，見上方重現指令說明。

## 重要提醒

- `result_aws/` 原始目錄底下還有數百個其他CSV，是實驗過程中的探索性重跑或未採用的baseline
  （Hudson/Lai-EUA等），與本論文最終數字無關，未收錄於本次交接。
- 上述CSV之情境歸屬經交叉比對使用者原始筆記與shell操作紀錄之重複模式後確認，並非100%具備
  原始程式自動化的可追溯標記（CSV內部無「此為論文最終版」欄位），若之後對數字有疑義，建議以
  本文件記錄的來源為準。
