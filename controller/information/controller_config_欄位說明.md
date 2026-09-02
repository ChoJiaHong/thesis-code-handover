# controller_config.json 欄位說明

來源：`information/controller_config.json`，欄位讀取／驗證邏輯見 `controller_v2.py`
（讀取預設值主要在檔案開頭 `cfg_*()` 系列函式，`/config` POST 的驗證在
`update_config()`，約第1886-1920行）。

**重要提醒**：`solver_mode` 打錯字時，透過 `/config` API 設定會直接被拒絕
（HTTP 400），不會靜默生效——但如果 API 呼叫本身沒送出去或失敗，controller
會沿用上一次殘留的值，不會自動變成你以為的新模式。跑正式實驗前，建議先
`GET /config` 確認 `solver_mode` 真的是預期值，不要只看自己上次「以為」送出的
POST 請求。

## solver_mode

當前實際跑哪個演算法的開關。合法值（`_VALID_MODES`，第1897-1898行）：

| 值 | 說明 |
|---|---|
| `ga` | 本研究方法，基礎版（`allocate_x_packed_dict`） |
| `ga_ff` | 本研究方法，first-fit/best-fit 分配變體（`allocate_x_ff_bf_dict`） |
| `ga_bf` | 本研究方法，正式實驗使用版本，分配器依 `ga_routing` 決定 |
| `ga_bf_prune` | `ga_bf` 的剪枝變體 |
| `ems` | Baseline：EMS（USC-TS/SUD 部署 + Round Robin 路由 + 保底f_l分配） |
| `ems_dl3` | EMS 部署 + DL3 P2C 路由的混合變體 |
| `ems_rr` | EMS 的更 naive 對照：純 f_h 分配、無 f_l 保底 |
| `ems_v2` | EMS 只增不減版本 |
| `lsr` | Baseline：LSR（兩階段貪婪部署+負載均衡路由，固定 f_l 分配） |
| `lsr_v2` | LSR 只增不減版本 |
| `dl3` | Baseline：DL3（Static Max 部署 + P2C 路由，已捨棄成正式對照組，見
  `baseline_design.md`，僅供程式碼參考） |
| `minlp` | 精確解（HiGHS/Pyomo 求解器），用於 5-2-7 節演算法品質驗證 |
| `hudson` | Baseline：Hudson 貪婪邊際增量部署+分配（`hudson_solver.py`） |
| `lai_eua` | Baseline：Lai EUA 貪婪裝箱部署+分配（`lai_eua_solver.py`） |

**除錯技巧**：controller 在每次求解完成時會印
`logging.info(f"[{cfg_solver_mode().upper()}] Solver completed. ...")`
（第1325行），部署決策時也會印 `[HUDSON]`／`[GA_BF]`／`[LSR]` 等字樣
（第1027、1066、1072行）。要確認一次實驗跑測「真的」用了哪個 solver_mode，
去查 controller 自己的 server 端 log（不是 experiment_ctrl 那個 client 端
連線 log），找這些帶方括號大寫模式名稱的行，比看 `/config` 回報或看結果檔案
存在哪個資料夾都更直接可信——**資料夾名稱是實驗腳本執行當下自動用
`solver_mode` 字串產生的，但如果檔案事後被人工搬動/改名過，資料夾名稱就不
再能保證對應真實跑測時的模式**。

## admission_control

`null`（目前值）。**注意：這個欄位不在 `/config` POST 允許更新的欄位清單裡**
（第1889-1892行 `allowed` 集合沒有它），也沒有找到對應的 `cfg_admission_
control()` 讀取函式——目前看起來像是保留在檔案裡但實際未被程式碼使用的欄位，
如果要真的靠它控制行為，需要先確認有沒有其他地方讀取它，不要假設改這個值
會生效。

## phase1_enabled

布林值，預設 `True`（未設定時，`cfg_phase1_enabled()` 第64-66行）。控制新增
使用者時是否啟用「過渡期先降頻服務」的 Phase 1 快捷路徑（見第1415行判斷式）。
只有 GA 家族與 MINLP 會實際檢查這個旗標；LSR/EMS/Hudson/Lai 等 baseline 一律
忽略、每次都走完整重解（見 `baseline_design.md` 已記錄的既有結論）。

## deploy_switch_policy

決定「候選配置何時觸發實際部署切換」的判準。合法值（`_VALID_SWITCH_
POLICIES`，第1901行）：

| 值 | 說明 |
|---|---|
| `nsat_only` | 只要候選配置的 A_sat（達標人數）提升就切換（目前設定值） |
| `q_superset` | A_sat 打平但 Q 提升，且新配置是舊配置的超集時也觸發切換 |
| `q_any` | A_sat 打平但 Q 提升即觸發切換，不要求超集關係 |

## q_switch_min_gain

浮點數，預設 `0.01`（第84行）。搭配 `deploy_switch_policy` 使用，Q 提升幅度
需超過這個門檻才視為「有意義的提升」、觸發切換（見第1223行 `_Q_MIN_GAIN`）。

## notify_wave_gap_s

浮點數，預設 `0.0`，需 ≥ 0（第1913-1914行有驗證）。過渡期執行「先降後升」時，
兩波通知之間的間隔秒數，讓 agent 端真的把頻率降下來後再進行下一步（第437、
451行）。

## pre_delete_gap_s

浮點數，預設 `0.0`，需 ≥ 0（第1915-1916行有驗證）。刪除 Pod 前，通知送出後
等待的秒數（第1783-1787行）。

## ga_pop_size / ga_generations / ga_n_restarts / ga_time_limit

GA 求解器的族群規模、世代數、重啟次數、時間限制（秒）。僅 `ga`/`ga_ff`/
`ga_bf`/`ga_bf_prune` 模式使用。目前值：20 / 150 / 1 / 1。

## ga_routing

僅 `ga_bf`/`ga_bf_prune` 模式使用（第1313-1316行）。

| 值 | 說明 |
|---|---|
| `optimize`（目前設定值，也是未匹配 `nsats` 時的預設分支） | 用 `allocate_x_optimize_dict` |
| `nsats` | 用 `allocate_x_nsats_dict` |

## minlp_time_limit / minlp_rel_gap / minlp_max_nodes

僅 `minlp` 模式使用：求解器時間上限（秒）、相對最適間隙、最大節點數。
目前值：30 / 0.1 / 10000。

## keep_pods_on_empty

布林值，預設 `False`（第39-40行）。控制節點上服務需求歸零時，是否保留該
Pod（不自動刪除）。目前 `controller_config.json` 沒有這個欄位（用預設值
False）。
