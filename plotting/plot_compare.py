"""
Plan B: Multi-solver comparison figure.

Usage:
    python3 plot_compare.py ga:/home/hiro/git_repo/ha/Controller_v2/results/ga_bf/s2_20260717_152950.csv ems:/home/hiro/git_repo/ha/Controller_v2/results/ems/s2_20260717_143827.csv 
Each argument is  label:csvfile  (colon-separated).
The last argument can be an output filename ending in .pdf/.png (optional).
Optional  --align=<label>  picks which solver's run length is used as the
summary-table time window cutoff (default: auto-pick the shortest run).
Use this when one run ended abnormally early (e.g. crashed) and would
otherwise truncate the comparison window for everyone else.
Optional  --warmup=<seconds>  sets the cold-start cutoff used to exclude
the ramp-up period from steady-state stats (default: 20).
Optional  --until=<seconds>  prints an extra table with, for each csv,
the cumulative A_sat·s (sum/integral of n_sat) and mean q over [0, t].
Optional  --from=<seconds>  sets the start time for the --until cumulative
table (default: 0), e.g. --from=100 --until=175 sums over [100, 175].
"""
import sys
import json
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from pathlib import Path
from experiment_ctrl_v2 import LATENCY_MODE

### 字體大小換算說明 ###
# 論文內文為 12pt，圖片以 \includegraphics[width=1.0\linewidth] 嵌入；
# 論文文字寬度（A4 扣除 inner=3cm、outer=2cm 邊界）= 16cm = 6.30in，
# 主要比較圖（out2，2 個子圖）畫布為 figsize=(13,10)，縮放比例 = 1.0*6.30/13 ≈ 0.48，
# 故 matplotlib 字級需設為 9.5/0.48 ≈ 20pt（較內文 12pt 略小）。
plt.rcParams.update({
    "font.size": 20,
    "axes.titlesize": 22,
    "axes.labelsize": 20,
    "xtick.labelsize": 17,
    "ytick.labelsize": 17,
    "legend.fontsize": 17,
    "figure.dpi": 150,
    "lines.linewidth": 1.5,
    "font.sans-serif": ["Noto Sans CJK JP", "Noto Sans CJK TC", "DejaVu Sans"],
    "axes.unicode_minus": False,
})

# 色弱（紅綠色覺缺陷）友善配色：藍／朱紅／黑三色彼此色相、明度差異最大。
# 線型統一改為實線（使用者要求），辨識僅依賴色相區分。
METHOD_COLOR = {"ga_bf": "#0072B2", "ems": "#D55E00", "lsr": "#000000", "minlp": "#CC79A7"}
METHOD_STYLE = {"ga_bf": "-", "ems": "--", "lsr": "-.", "minlp": ":"}
METHOD_LABEL = {"ga_bf": "Proposed Method", "ems": "EMS", "lsr": "LSR", "minlp": "MINLP"}
DEFAULT_COLOR_CYCLE = ["#0072B2", "#D55E00", "#000000", "#CC79A7", "#009E73", "#56B4E9"]
DEFAULT_STYLE_CYCLE = ["-", "--", "-.", ":", "-", "--"]

def _assign_visuals(labels):
    """為每個 label 分配 (color, style)，保證同一次比較裡不會撞色/撞線型。
    已知方法名（ga_bf/ems/lsr/minlp）優先保留慣用固定色；其餘 label
    （或撞到已知色的情況）依序從預設色環挑「目前尚未被用掉」的下一組，
    確保四路以上比較時每條線都能區分。"""
    assigned = {}
    used_colors = set()
    for label in labels:
        if label in METHOD_COLOR and METHOD_COLOR[label] not in used_colors:
            assigned[label] = (METHOD_COLOR[label], METHOD_STYLE[label])
            used_colors.add(METHOD_COLOR[label])
    cycle_i = 0
    for label in labels:
        if label in assigned:
            continue
        while DEFAULT_COLOR_CYCLE[cycle_i % len(DEFAULT_COLOR_CYCLE)] in used_colors:
            cycle_i += 1
        color = DEFAULT_COLOR_CYCLE[cycle_i % len(DEFAULT_COLOR_CYCLE)]
        style = DEFAULT_STYLE_CYCLE[cycle_i % len(DEFAULT_STYLE_CYCLE)]
        used_colors.add(color)
        assigned[label] = (color, style)
        cycle_i += 1
    return assigned

VISUALS = {}  # 於 datasets 建立後填入（見下方），_color_for/_style_for 於呼叫當下查表

def _color_for(label, i=None):
    return VISUALS[label][0]

def _style_for(label, i=None):
    return VISUALS[label][1]

def _label_for(label):
    return METHOD_LABEL.get(label, label)

# ── 解析命令列 ───────────────────────────────────────────────────────────────
args = sys.argv[1:]
out = "compare_solvers.pdf"
if args and (args[-1].endswith(".pdf") or args[-1].endswith(".png")):
    out = args.pop()

align_to = None
WARMUP_S = 20.0
UNTIL_T = None
FROM_T = 0.0
EVENTS = []  # list of (time_s, label)
remaining = []
for a in args:
    if a.startswith("--align="):
        align_to = a.split("=", 1)[1]
    elif a.startswith("--warmup="):
        WARMUP_S = float(a.split("=", 1)[1])
    elif a.startswith("--until="):
        UNTIL_T = float(a.split("=", 1)[1])
    elif a.startswith("--from="):
        FROM_T = float(a.split("=", 1)[1])
    elif a.startswith("--events="):
        for item in a.split("=", 1)[1].split(","):
            t_str, _, ev_label = item.partition(":")
            EVENTS.append((float(t_str), ev_label))
    else:
        remaining.append(a)
args = remaining

if not args:
    print("Usage: python3 plot_compare.py label1:file1.csv label2:file2.csv ... "
          "[--align=label] [--warmup=seconds] [--from=seconds] [--until=seconds] "
          "[--events=time1:label1,time2:label2] [out.pdf]")
    sys.exit(1)

# QoS 門檻（來自 information/serviceSpec_mul.json，三服務一律 f_l=10, f_h=30）
F_L = 10
F_H = 30

def _recompute_qscore(row):
    """從 per_user_detail 以實際量測到的 eff_fps 重算 q：先算「每個使用者自己」
    的服務平均分數（該使用者各訂閱服務的 eff_fps/f_h，cap 在 1，未達 f_l 該項計 0，
    再除以他訂閱的服務數量），最後對所有使用者取平均（每人權重相等）。
    不能直接對所有 (使用者,服務) 項做全域平均——那樣訂閱多個服務的使用者會比
    只訂閱一個服務的使用者在平均裡佔更重的權重，扭曲「每個使用者體驗同等重要」
    的語意（與 plot_trace.py 的 _recompute_qscore 邏輯一致；CSV 的 q_score 欄是
    solver 分配的目標頻率，非實測值）。"""
    try:
        detail = json.loads(row["per_user_detail"]) if isinstance(row["per_user_detail"], str) else {}
    except Exception:
        return min(float(row.get("q_score", 0) or 0), 1.0)
    user_scores = []
    for uid, info in detail.items():
        svcs = info.get("services", [])
        if not svcs:
            continue
        eff = info.get("eff_fps", {})
        term_sum = 0.0
        for svc in svcs:
            fps = eff.get(svc, 0)
            if fps >= F_L:
                term_sum += min(fps / F_H, 1.0)
        user_scores.append(term_sum / len(svcs))
    return round(min(sum(user_scores) / len(user_scores), 1.0), 4) if user_scores else 0.0

def _recompute_nsats(row):
    """從 per_user_detail 以實際量測到的 eff_fps 重算 nsats：一個 Agent 算達標，
    必須其所有訂閱服務的 eff_fps 皆 >= f_l（與 plot_trace.py 的 _recompute_nsats
    邏輯一致）。CSV 的 nsats 欄是 solver 分配當下的名義決策（x_{i,j,k} 是否 >= f_l），
    不是事後在真實硬體上量測到的結果，兩者在容量信念與真實值有落差時會分岔，
    故主指標一律改用此重算值，與次要指標 q_score 採同一套真實量測基準。"""
    try:
        detail = json.loads(row["per_user_detail"]) if isinstance(row["per_user_detail"], str) else {}
    except Exception:
        return int(row.get("nsats", 0) or 0)
    count = 0
    for uid, info in detail.items():
        svcs = info.get("services", [])
        eff = info.get("eff_fps", {})
        if svcs and all(eff.get(s, 0) >= F_L for s in svcs):
            count += 1
    return count

def _load_one(path):
    df = pd.read_csv(path)
    if "per_user_detail" in df.columns:
        df["nsats"] = df.apply(_recompute_nsats, axis=1)
        df["sat_rate"] = df.apply(
            lambda r: r["nsats"] / r["n_users"] if r["n_users"] > 0 else 0.0, axis=1)
        df["q_score"] = df.apply(_recompute_qscore, axis=1)
    # 保險：不論走哪條路徑（重算或舊版未正規化的 CSV 原始欄），畫出來的 q 一律不超過 1
    if "q_score" in df.columns:
        df["q_score"] = df["q_score"].clip(upper=1.0)
    return df

# label:file 語法額外支援 label:file1.csv,file2.csv,...（同一方法之多次重跑，
# 逗號分隔）；只給一個檔案時行為與之前完全相同。all_runs 保留每個 label 的完整
# 重跑清單，供下方 summary 表格取跨重跑之 mean±std；datasets 只取第一次重跑，
# 供時序圖（a-e）繪製單次代表性曲線（時序圖不因多次重跑而改變畫法）。
all_runs = {}
datasets = {}
for a in args:
    label, paths = a.split(":", 1)
    dfs = [_load_one(p) for p in paths.split(",")]
    all_runs[label] = dfs
    datasets[label] = dfs[0]

VISUALS.update(_assign_visuals(list(datasets.keys())))

# ── 共用函式 ────────────────────────────────────────────────────────────────
def smooth(series, window=5):
    return series.rolling(window, center=True, min_periods=1).mean()

def nsat_seconds_until(df, t_end, t_start=0.0):
    """A_sat(t) 對時間積分到 t_end 秒的面積（人·秒），從 t_start 開始算（預設 0）。
    採用跟 experiment_ctrl_v2.py summary 相同的 step 假設：第 i 筆的 nsats
    代表從上一筆到這一筆之間維持的狀態（drawstyle="steps-post" 的面積）；t_start
    落在兩筆資料中間時，該區間仍算入右端點（第 i 筆）的 nsats 值。"""
    d = df[df["elapsed_s"] <= t_end].sort_values("elapsed_s")
    if d.empty:
        return 0.0
    t = d["elapsed_s"].to_numpy()
    n = d["nsats"].to_numpy()
    area = 0.0
    prev_t = 0.0
    for i in range(len(t)):
        seg_start = max(prev_t, t_start)
        seg_end = max(t[i], t_start)
        if seg_end > seg_start:
            area += n[i] * (seg_end - seg_start)
        prev_t = t[i]
    if t[-1] < t_end:
        seg_start = max(t[-1], t_start)
        if t_end > seg_start:
            area += n[-1] * (t_end - seg_start)
    return area

SVCS = ["pose", "gesture", "object"]
# 依 experiment_ctrl_v2.LATENCY_MODE 決定要看 e2e 還是純 service(gRPC) 延遲欄位，
# 與 plot_trace.py 保持一致；本批 AWS 資料為 "service" 模式，avg_*_latency_ms
# 全為 0（未量測 e2e），若沿用舊的 avg_*_latency_ms 會導致平均值全部是 N/A。
_LAT_SUFFIX = "svclat_ms" if LATENCY_MODE == "service" else "latency_ms"
LAT_COLS = [f"avg_{s}_{_LAT_SUFFIX}" for s in SVCS]
EFF_COLS = [f"eff_{s}_fps" for s in SVCS]
REAL_COLS = [f"real_{s}_fps" for s in SVCS]

def mean_latency_all_services(s):
    """三服務平均延遲：0 代表該服務該筆無使用者，排除後再取平均，避免被拉低。"""
    cols = [c for c in LAT_COLS if c in s.columns]
    return s[cols].replace(0, np.nan).mean(axis=1).mean()

def series_mean_eff_fps(df):
    """逐筆之三服務平均有效訊框率（0＝該筆未訂閱該服務，排除後再平均）。"""
    cols = [c for c in EFF_COLS if c in df.columns]
    return df[cols].replace(0, np.nan).mean(axis=1)

def series_mean_latency(df):
    """逐筆之三服務平均延遲（0＝該筆未訂閱該服務，排除後再平均）。"""
    cols = [c for c in LAT_COLS if c in df.columns]
    return df[cols].replace(0, np.nan).mean(axis=1)

def series_wasted_fps(df):
    """逐筆之三服務浪費 FPS 加總（real－eff，延遲超標之無效產能，語意與
    plot_trace.py panel (d) 一致；用加總而非平均，因為這是絕對量而非比例）。"""
    total = pd.Series(0.0, index=df.index)
    for svc in SVCS:
        eff_c, real_c = f"eff_{svc}_fps", f"real_{svc}_fps"
        if eff_c in df.columns and real_c in df.columns:
            total = total + (df[real_c] - df[eff_c]).clip(lower=0)
    return total

def sat_by_users(df, bins=None):
    """
    回傳每個 n_users 值的平均 sat_rate（排除冷啟動初期 elapsed < WARMUP_S）。
    """
    df = df[df["elapsed_s"] >= WARMUP_S].copy()
    return df.groupby("n_users")["sat_rate"].mean()


fig, axes = plt.subplots(1, 3, figsize=(14, 4.5))

# ── (a) 達標率 vs 使用者數 ───────────────────────────────────────────────────
ax = axes[0]
for i, (label, df) in enumerate(datasets.items()):
    sr = sat_by_users(df)
    ax.plot(sr.index, sr.values,
            color=_color_for(label, i), linestyle=_style_for(label, i),
            marker="o", markersize=5, alpha=0.9,
            markevery=max(1, len(sr)//10),
            label=_label_for(label))
ax.set_xlabel("使用者數量（$n$）")
ax.set_ylabel("達標率")
ax.set_ylim(-0.02, 1.05)
ax.axhline(1.0, color="gray", linewidth=0.7, linestyle="--", alpha=0.5)
ax.legend()
ax.grid(True, alpha=0.25)

# ── (b) A_sat 時序（smoothed）──────────────────────────────────────────────
ax = axes[1]
for i, (label, df) in enumerate(datasets.items()):
    ax.plot(df["elapsed_s"], smooth(df["nsats"]),
            color=_color_for(label, i), linestyle=_style_for(label, i),
            alpha=0.9, label=_label_for(label))
ax.set_xlabel("經過時間（秒）")
ax.set_ylabel("$A_{sat}$（達標人數，平滑後）")
ax.legend()
ax.grid(True, alpha=0.25)

# ── (c) 平均延遲時序（pose，smoothed，截斷 2000ms）────────────────────────
ax = axes[2]
LAT_CAP = 2000
for i, (label, df) in enumerate(datasets.items()):
    lat = smooth(df["avg_pose_latency_ms"].clip(upper=LAT_CAP))
    ax.plot(df["elapsed_s"], lat, color=_color_for(label, i),
            linestyle=_style_for(label, i), alpha=0.9, label=_label_for(label))
ax.axhline(100, color="gray", linewidth=0.8, linestyle="--", alpha=0.6)
ax.text(5, 110, "100 ms", fontsize=22, color="gray")
ax.set_xlabel("經過時間（秒）")
ax.set_ylabel("Pose 平均延遲（毫秒，上限2000）")
ax.set_ylim(bottom=0)
ax.legend()
ax.grid(True, alpha=0.25)

Path(out).parent.mkdir(parents=True, exist_ok=True)
plt.tight_layout()
plt.savefig(out, bbox_inches="tight")
print(f"Saved: {out}")

# ── 摘要表格 ────────────────────────────────────────────────────────────────
# 對齊到指定（--align）或最短實驗的長度，避免不同 solver 跑的時長不同造成平均值涵蓋不同階段的使用者爬升曲線。
run_end_times = {label: [df["elapsed_s"].max() for df in dfs] for label, dfs in all_runs.items()}

print("\n=== Run durations (elapsed_s max) ===")
for label, ends in run_end_times.items():
    if len(ends) == 1:
        print(f"  {label:10s} {ends[0]:.1f}s")
    else:
        print(f"  {label:10s} " + ", ".join(f"{e:.1f}s" for e in ends) + f"  (n_runs={len(ends)})")

too_short = [label for label, ends in run_end_times.items() if min(ends) < WARMUP_S]
if too_short:
    print(f"錯誤：--warmup={WARMUP_S:.0f}s 超過以下 solver 實際跑完的時間，"
          f"完全沒有資料落在穩態區間內：{too_short}。"
          f"請改小 --warmup，或從比較中移除這些 solver。")
    sys.exit(1)

if align_to:
    if align_to not in run_end_times:
        print(f"錯誤：--align={align_to} 不是有效的 solver 標籤，可用：{list(run_end_times)}")
        sys.exit(1)
    common_end_label, common_end = align_to, min(run_end_times[align_to])
else:
    common_end_label, common_end = min(
        ((label, min(ends)) for label, ends in run_end_times.items()), key=lambda x: x[1])

if WARMUP_S >= common_end:
    print(f"錯誤：--warmup={WARMUP_S:.0f}s 超過（或等於）共同時間窗上限 "
          f"{common_end:.1f}s（{common_end_label}），穩態區間會是空的。"
          f"請改小 --warmup，或用 --align 指定一個跑得比 {WARMUP_S:.0f}s 久的 solver。")
    sys.exit(1)

def _per_run_stats(df, common_end):
    s = df[(df["elapsed_s"] >= WARMUP_S) & (df["elapsed_s"] <= common_end)]
    return {
        "n_samples": len(s),
        "A_sat_s_warmup": nsat_seconds_until(df, WARMUP_S),
        "mean_sat_rate": s["sat_rate"].mean(),
        "mean_A_sat": s["nsats"].mean(),
        "mean_utilization": s["capacity_utilization"].mean() if "capacity_utilization" in s.columns else float("nan"),
        "mean_lat": mean_latency_all_services(s),
        "max_pods": s["total_pods"].max(),
        "pod_changes": s["cumulative_pod_changes"].max(),
    }

def _fmt(values, decimals):
    """單次重跑顯示單一數值(與改動前輸出格式相同)；多次重跑顯示跨重跑之 mean ± std。"""
    arr = np.array(values, dtype=float)
    if np.all(np.isnan(arr)):
        return "N/A"
    if len(arr) == 1:
        return f"{arr[0]:.{decimals}f}"
    return f"{np.nanmean(arr):.{decimals}f} ± {np.nanstd(arr, ddof=1):.{decimals}f}"

basis = "manually aligned to" if align_to else "shortest run:"
print(f"\n=== Summary (steady-state, {WARMUP_S:.0f}s <= elapsed <= {common_end:.1f}s, "
      f"{basis} {common_end_label}) ===")
MIN_SAMPLES = 5
rows = []
low_sample_solvers = []
for label, dfs in all_runs.items():
    per_run = [_per_run_stats(df, common_end) for df in dfs]
    n_runs = len(per_run)
    min_samples_this_label = min(r["n_samples"] for r in per_run)
    if min_samples_this_label < MIN_SAMPLES:
        low_sample_solvers.append((label, min_samples_this_label))
    rows.append({
        "Solver": label if n_runs == 1 else f"{label} (n={n_runs})",
        "n_samples": _fmt([r["n_samples"] for r in per_run], 0),
        f"A_sat·s in first {WARMUP_S:.0f}s (人·秒)": _fmt([r["A_sat_s_warmup"] for r in per_run], 1),
        "mean sat_rate": _fmt([r["mean_sat_rate"] for r in per_run], 3),
        "mean A_sat": _fmt([r["mean_A_sat"] for r in per_run], 2),
        "mean utilization": _fmt([r["mean_utilization"] for r in per_run], 3),
        "mean lat all svc (ms)": _fmt([r["mean_lat"] for r in per_run], 1),
        "max pods": _fmt([r["max_pods"] for r in per_run], 0),
        "pod changes": _fmt([r["pod_changes"] for r in per_run], 0),
    })
print(pd.DataFrame(rows).to_string(index=False))

if low_sample_solvers:
    detail = ", ".join(f"{label}={n}筆" for label, n in low_sample_solvers)
    print(f"\n⚠ 警告：以下 solver 在穩態區間內樣本數過少（<{MIN_SAMPLES}筆），統計量不可信：{detail}。"
          f"通常代表 --warmup 太接近該 solver 的實際跑完時間，建議改用 --align 換一個對齊基準，"
          f"或把該 solver 從比較中移除。")

# ── --until=t（可搭配 --from=t0）：[t0,t] 的 A_sat 加總（人·秒）與 q 平均 ──────
if UNTIL_T is not None:
    if FROM_T >= UNTIL_T:
        print(f"錯誤：--from={FROM_T:.0f}s 超過（或等於）--until={UNTIL_T:.0f}s，區間會是空的。")
        sys.exit(1)
    print(f"\n=== Cumulative stats in [{FROM_T:.1f}s, {UNTIL_T:.1f}s] ===")
    too_short_for_until = [label for label, ends in run_end_times.items() if min(ends) < UNTIL_T]
    if too_short_for_until:
        detail = ", ".join(f"{label}({min(run_end_times[label]):.1f}s)" for label in too_short_for_until)
        print(f"⚠ 注意：以下 solver 之最短重跑時間為：{detail}，短於指定的 t={UNTIL_T:.1f}s，"
              f"A_sat·s 的計算會假設其最後一筆 nsats 持續到 t（可能高估）。")
    rows3 = []
    for label, dfs in all_runs.items():
        n_runs = len(dfs)
        n_samples_list, sums, qmeans = [], [], []
        for df in dfs:
            seg = df[(df["elapsed_s"] >= FROM_T) & (df["elapsed_s"] <= UNTIL_T)]
            n_samples_list.append(len(seg))
            sums.append(nsat_seconds_until(df, UNTIL_T, FROM_T))
            qmeans.append(seg["q_score"].mean() if "q_score" in seg.columns and len(seg) else float("nan"))
        rows3.append({
            "Solver": label if n_runs == 1 else f"{label} (n={n_runs})",
            "n_samples": _fmt(n_samples_list, 0),
            f"sum A_sat·s in [{FROM_T:.0f},{UNTIL_T:.0f}s] (人·秒)": _fmt(sums, 1),
            f"mean q in [{FROM_T:.0f},{UNTIL_T:.0f}s]": _fmt(qmeans, 4),
        })
    print(pd.DataFrame(rows3).to_string(index=False))

# ── 新增圖表 (d)(e): A_sat + Q score 時序比較 ────
_p   = Path(out)
out2 = str(_p.parent / (_p.stem + "_nsat_q" + _p.suffix))

fig2, (ax_nsat, ax_q) = plt.subplots(
    2, 1, figsize=(13, 10.5), sharex=True,
    layout="constrained",
    gridspec_kw={"height_ratios": [1, 1], "hspace": 0.18},
)

for i, (label, df) in enumerate(datasets.items()):
    color = _color_for(label, i)
    style = _style_for(label, i)
    name  = _label_for(label)
    t     = df["elapsed_s"]

    # (d) A_sat 比較
    ax_nsat.plot(
        t, smooth(df["nsats"], window=7),
        color=color, linestyle=style, alpha=0.9,
        label=name,
    )

    # (e) Q score 比較
    if "q_score" in df.columns:
        ax_q.plot(
            t, smooth(df["q_score"], window=7),
            color=color, linestyle=style, alpha=0.9,
            label=name,
        )
    else:
        ax_q.text(
            0.5, 0.5 - i * 0.12,
            f"[{name}] q_score column missing",
            ha="center", va="center",
            transform=ax_q.transAxes, color=color, fontsize=22,
        )

ax_nsat.set_ylabel("$A_{sat}$ (Satisfied Users)")
ax_nsat.legend(loc="lower center", bbox_to_anchor=(0.5, 1.02), ncol=3,
               framealpha=0.9, handlelength=2.2, columnspacing=1.5)
ax_nsat.grid(True, alpha=0.25)
ax_nsat.set_ylim(bottom=0)

ax_q.set_ylabel("$Q$ (Average Service Quality)")
ax_q.legend(loc="lower center", bbox_to_anchor=(0.5, 1.02), ncol=3,
            framealpha=0.9, handlelength=2.2, columnspacing=1.5)
ax_q.grid(True, alpha=0.25)
ax_q.set_ylim(-0.02, 1.05)

# ── 事件標註（例如節點故障／復原時間點） ──
for ev_t, ev_label in EVENTS:
    for ax in (ax_nsat, ax_q):
        ax.axvline(x=ev_t, color="gray", linestyle=":", linewidth=1.3, alpha=0.8, zorder=0)
    y_top = ax_nsat.get_ylim()[1]
    ax_nsat.text(
        ev_t, y_top * 0.98, ev_label,
        rotation=90, ha="right", va="top",
        color="gray", fontsize=15,
    )

for ax in (ax_nsat, ax_q):
    ax.tick_params(axis="x", labelbottom=True)
    ax.set_xlabel("Elapsed Time (s)")

Path(out2).parent.mkdir(parents=True, exist_ok=True)
plt.savefig(out2, bbox_inches="tight")
print(f"Saved: {out2}")

# ── 新增圖表：逐服務比較（有效訊框率／延遲／浪費FPS），解釋 A_sat 差異成因 ──────
# 對應 plot_trace.py 的 panel (b)(c)(d)，但改成跨方法疊圖比較（而非單一方法逐服務
# 拆開），三服務先各自排除未訂閱（0）之後取平均／加總，維持與摘要表格同一套統計基準。
out3 = str(_p.parent / (_p.stem + "_svc" + _p.suffix))

fig3, (ax_eff, ax_lat, ax_waste) = plt.subplots(
    3, 1, figsize=(13, 15.5), sharex=True,
    layout="constrained",
    gridspec_kw={"height_ratios": [1, 1, 1], "hspace": 0.18},
)

for i, (label, df) in enumerate(datasets.items()):
    color = _color_for(label, i)
    style = _style_for(label, i)
    name  = _label_for(label)
    t     = df["elapsed_s"]

    ax_eff.plot(t, smooth(series_mean_eff_fps(df), window=7),
                color=color, linestyle=style, alpha=0.9, label=name)
    ax_lat.plot(t, smooth(series_mean_latency(df), window=7),
                color=color, linestyle=style, alpha=0.9, label=name)
    ax_waste.plot(t, smooth(series_wasted_fps(df), window=7),
                  color=color, linestyle=style, alpha=0.9, label=name)

ax_eff.axhline(F_L, color="gray", linewidth=0.9, linestyle="--", alpha=0.6)
ax_eff.text(t.max() * 1.005 if len(t) else 0, F_L, f"$f^l$={F_L}",
            va="center", fontsize=15, color="gray")
ax_eff.set_ylabel("Mean Effective FPS\n(active services)")
ax_eff.legend(loc="lower center", bbox_to_anchor=(0.5, 1.02), ncol=3,
              framealpha=0.9, handlelength=2.2, columnspacing=1.5)
ax_eff.grid(True, alpha=0.25)
ax_eff.set_ylim(bottom=0)

ax_lat.axhline(100, color="gray", linewidth=0.9, linestyle="--", alpha=0.6)
ax_lat.text(2, 105, "100 ms", fontsize=15, color="gray")
ax_lat.set_ylabel(f"Mean {LATENCY_MODE.title()} Latency\n(active services, ms)")
ax_lat.grid(True, alpha=0.25)
ax_lat.set_ylim(bottom=0)

ax_waste.set_ylabel("Wasted FPS\n(sum, latency-exceeded)")
ax_waste.grid(True, alpha=0.25)
ax_waste.set_ylim(bottom=0)

for ev_t, ev_label in EVENTS:
    for ax in (ax_eff, ax_lat, ax_waste):
        ax.axvline(x=ev_t, color="gray", linestyle=":", linewidth=1.3, alpha=0.8, zorder=0)

for ax in (ax_eff, ax_lat, ax_waste):
    ax.tick_params(axis="x", labelbottom=True)
    ax.set_xlabel("Elapsed Time (s)")

Path(out3).parent.mkdir(parents=True, exist_ok=True)
plt.savefig(out3, bbox_inches="tight")
print(f"Saved: {out3}")
