"""
Plan A: Thesis-quality 4-panel scenario trace figure.

Panels:
  (a) n_users / N_sat / sat_rate
  (b) Effective FPS per service
  (c) Avg latency per service
  (d) Service deployment per node (Gantt-style, requires node_topology column)
python3 plot_trace.py /home/hiro/git_repo/ha/Controller_v2/results/ga/s1_20260527_192440.csv
Usage:
    python3 plot_trace.py experiment_ctrl_s2_20260513_214007.csv [output.pdf]
Optional  --panels=bcd  只畫指定的子面板（依 a/b/c/d/e/f/g 代號，任意子集合、
任意順序皆可，例如 --panels=bcd 只留 (b)(c)(d) 三格；預設畫全部 7 格）。
用於論文需要「以服務區分、每個方法一張圖」時，只保留跟服務相關的面板，
省去與其他圖表（使用者/達標率、部署拓樸、供需、Q 分數）重複的部分。
"""
import sys
import json
import pandas as pd
from experiment_ctrl_v2 import LATENCY_MODE
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
from pathlib import Path

# ── 論文字型設定 ────────────────────────────────────────────────────────────
plt.rcParams.update({
    "font.size": 11,
    "axes.titlesize": 12,
    "axes.labelsize": 11,
    "xtick.labelsize": 10,
    "ytick.labelsize": 10,
    "legend.fontsize": 10,
    "figure.dpi": 150,
    "lines.linewidth": 1.8,
    # matplotlib 解析 NotoSansCJK-Regular.ttc 這類多臉字體檔時，只會註冊掃到
    # 的第一個臉（實測為 "Noto Sans CJK JP"），指定 "...TC" 在字體快取裡查不
    # 到、會整段缺字變空白方塊；若之後在此圖加中文標籤，比照 plot_compare.py
    # 改用下面這個 matplotlib 實際能找到的名稱。
    "font.sans-serif": ["Noto Sans CJK JP", "Noto Sans CJK TC", "DejaVu Sans"],
    "axes.unicode_minus": False,
})

_args = sys.argv[1:]
PANELS = list("abcdefg")
_remaining = []
for _a in _args:
    if _a.startswith("--panels="):
        requested = _a.split("=", 1)[1].strip().lower()
        PANELS = [p for p in "abcdefg" if p in requested]
    else:
        _remaining.append(_a)
_args = _remaining

CSV = _args[0] if len(_args) > 0 else \
    "experiment_ctrl_s2_20260513_214007.csv"
OUT = _args[1] if len(_args) > 1 else Path(CSV).stem + "_trace.pdf"

df = pd.read_csv(CSV)
t = df["elapsed_s"]

# ── 從 per_user_detail 重算 nsats（使用本檔案定義的 FL 門檻，與 nsats 欄來源無關）──

def _recompute_nsats(row):
    """Recompute nsats from measured eff_fps using FL thresholds defined in this script."""
    try:
        detail = json.loads(row["per_user_detail"]) if isinstance(row["per_user_detail"], str) else {}
    except Exception:
        return int(row.get("nsats", 0))
    thresholds = {"pose": FL_POSE, "gesture": FL_GESTURE, "object": FL_OBJECT}
    count = 0
    for uid, info in detail.items():
        svcs = info.get("services", [])
        eff  = info.get("eff_fps", {})
        if svcs and all(eff.get(s, 0) >= thresholds.get(s, 0) for s in svcs):
            count += 1
    return count


def _recompute_wasted(row):
    """從 per_user_detail 重算每服務的「浪費 FPS」：以使用者為單位的 all-or-nothing
    判定——若該使用者訂閱的服務中有任一項未達 f_l（不論是延遲超標或頻率本身不足），
    視為此使用者整體未達標，其訂閱的每一項服務之 real_fps 就整段算作浪費（因為對
    這個使用者而言，即使某一項服務本身有達標，使用者體驗仍是破碎的，該服務的產出
    並未真正轉換成有效體驗）；只有使用者全部服務皆達標時，才只把「real - eff」
    這段延遲超標的差額算作浪費（原本的定義）。回傳 {service: wasted_fps} 之平均值
    （對訂閱該服務的使用者取平均，與 real_*_fps/eff_*_fps 欄位一致為 avg per user）。"""
    thresholds = {"pose": FL_POSE, "gesture": FL_GESTURE, "object": FL_OBJECT}
    try:
        detail = json.loads(row["per_user_detail"]) if isinstance(row["per_user_detail"], str) else {}
    except Exception:
        detail = {}
    sums = {"pose": 0.0, "gesture": 0.0, "object": 0.0}
    counts = {"pose": 0, "gesture": 0, "object": 0}
    for uid, info in detail.items():
        svcs = info.get("services", [])
        if not svcs:
            continue
        fps = info.get("fps", {})
        eff = info.get("eff_fps", {})
        user_ok = all(eff.get(s, 0) >= thresholds.get(s, 0) for s in svcs)
        for s in svcs:
            if s not in sums:
                continue
            real_v = fps.get(s, 0)
            eff_v  = eff.get(s, 0)
            wasted = real_v if not user_ok else max(real_v - eff_v, 0)
            sums[s] += wasted
            counts[s] += 1
    return {s: (sums[s] / counts[s] if counts[s] > 0 else 0.0) for s in sums}


def _recompute_qscore(row):
    """從 per_user_detail 以實際量測到的 eff_fps 重算 q：先算「每個使用者自己」
    的服務平均分數（該使用者各訂閱服務的 eff_fps/f_h，cap 在 1，未達 f_l 該項計 0，
    再除以他訂閱的服務數量），最後對所有使用者取平均（每人權重相等）。
    不能直接對所有 (使用者,服務) 項做全域平均——那樣訂閱多個服務的使用者會比
    只訂閱一個服務的使用者在平均裡佔更重的權重，扭曲「每個使用者體驗同等重要」
    的語意（與 q_score 欄來源無關，CSV 欄是 solver 分配的目標頻率，非實測值）。"""
    try:
        detail = json.loads(row["per_user_detail"]) if isinstance(row["per_user_detail"], str) else {}
    except Exception:
        return min(float(row.get("q_score", 0) or 0), 1.0)
    thresholds_l = {"pose": FL_POSE, "gesture": FL_GESTURE, "object": FL_OBJECT}
    thresholds_h = {"pose": FH_POSE, "gesture": FH_GESTURE, "object": FH_OBJECT}
    user_scores = []
    for uid, info in detail.items():
        svcs = info.get("services", [])
        if not svcs:
            continue
        eff = info.get("eff_fps", {})
        term_sum = 0.0
        for svc in svcs:
            fps = eff.get(svc, 0)
            if fps >= thresholds_l.get(svc, 0):
                term_sum += min(fps / thresholds_h.get(svc, 1), 1.0)
        user_scores.append(term_sum / len(svcs))
    return round(min(sum(user_scores) / len(user_scores), 1.0), 4) if user_scores else 0.0

# ── 色盤（colorblind-friendly）─────────────────────────────────────────────
C_POSE    = "#0072B2"   # blue
C_GESTURE = "#E69F00"   # orange
C_OBJECT  = "#009E73"   # green
C_USERS   = "#999999"   # grey fill
C_NSAT    = "#D55E00"   # vermillion
C_SAT     = "#56B4E9"   # sky blue

# 黑白列印區分用：三個服務除了顏色，額外用線型／填充紋理區分，避免灰階印刷時
# 三條線（或三種色塊）混在一起分不出來。呼應 plot_compare.py 已用過的
# solid/dashed/dash-dot 慣例。
S_POSE    = "-"
S_GESTURE = "--"
S_OBJECT  = "-."
H_POSE    = ""
H_GESTURE = "//"
H_OBJECT  = "xx"

# QoS 門檻（來自 information/serviceSpec_mul.json，三服務一律 f_l=10, f_h=30）
FL_POSE    = 10   # f_l
FH_POSE    = 30   # f_h
FL_GESTURE = 10
FH_GESTURE = 30
FL_OBJECT  = 10
FH_OBJECT  = 30

# 用 FL/FH 門檻從 per_user_detail 重算 nsats / sat_rate / q_score，覆蓋 CSV 欄位
if "per_user_detail" in df.columns:
    df["nsats"]    = df.apply(_recompute_nsats, axis=1)
    df["sat_rate"] = df.apply(
        lambda r: r["nsats"] / r["n_users"] if r["n_users"] > 0 else 0.0, axis=1)
    df["q_score"]  = df.apply(_recompute_qscore, axis=1)
    _wasted = df.apply(_recompute_wasted, axis=1)
    df["wasted_pose_fps"]    = _wasted.apply(lambda d: d["pose"])
    df["wasted_gesture_fps"] = _wasted.apply(lambda d: d["gesture"])
    df["wasted_object_fps"]  = _wasted.apply(lambda d: d["object"])

# 保險：不論走哪條路徑（重算或舊版未正規化的 CSV 原始欄），畫出來的 q 一律不超過 1
if "q_score" in df.columns:
    df["q_score"] = df["q_score"].clip(upper=1.0)

_ALL_HEIGHT_RATIOS = {"a": 2, "b": 1.5, "c": 1.5, "d": 1.5, "e": 1.8, "f": 1.5, "g": 1.5}
_height_ratios = [_ALL_HEIGHT_RATIOS[p] for p in PANELS]
_figsize = (10, 3 * len(PANELS))

fig, _axes_arr = plt.subplots(len(PANELS), 1, figsize=_figsize, sharex=True,
                         layout="constrained",
                         gridspec_kw={"hspace": 0.08, "height_ratios": _height_ratios})
axes = list(np.atleast_1d(_axes_arr))
_ax_of = dict(zip(PANELS, axes))
# 顯示用字母：不管原始代號(a-g)選了哪些子集合，畫出來一律從 (a) 依選取順序
# 重新編號，避免例如 --panels=bcde 卻在論文裡單獨成圖時，標題顯示「(b)」
# 卻找不到「(a)」在哪裡的疑惑。
_disp = {orig: chr(ord("a") + i) for i, orig in enumerate(PANELS)}

# ── Panel (a): n_users vs N_sat + sat_rate ─────────────────────────────────
if "a" in PANELS:
    ax1 = _ax_of["a"]
    ax1r = ax1.twinx()

    ax1.plot(t, df["n_users"], color=C_USERS, linewidth=1.5, drawstyle="steps-post", label="# Users (total)")
    ax1.plot(t, df["nsats"], color=C_NSAT, linewidth=2.2, drawstyle="steps-post", label="$N_{sat}$")
    ax1r.plot(t, df["sat_rate"], color=C_SAT, linewidth=1.5, drawstyle="steps-post", label="Sat. Rate")
    ax1r.axhline(1.0, color="black", linewidth=0.7, linestyle="--", alpha=0.4)
    ax1r.set_ylim(-0.05, 1.15)
    ax1r.set_ylabel("Satisfaction Rate", color=C_SAT)
    ax1r.tick_params(axis="y", labelcolor=C_SAT)

    ax1.set_ylabel("User Count")
    ax1.set_title(f"({_disp['a']}) Users and QoS Satisfaction Over Time")

    h1, l1 = ax1.get_legend_handles_labels()
    h2, l2 = ax1r.get_legend_handles_labels()
    ax1.legend(h1 + h2, l1 + l2, loc="upper left", framealpha=0.85)
    ax1.grid(True, alpha=0.25)

# ── Panel (b): Effective FPS per service ───────────────────────────────────
if "b" in PANELS:
    ax2 = _ax_of["b"]

    ax2.plot(t, df["eff_pose_fps"],    color=C_POSE,    linestyle=S_POSE,    label="pose (eff)")
    ax2.plot(t, df["eff_gesture_fps"], color=C_GESTURE, linestyle=S_GESTURE, label="gesture (eff)")
    ax2.plot(t, df["eff_object_fps"],  color=C_OBJECT,  linestyle=S_OBJECT,  label="YOLOv8n (eff)")

    # f_l 參考線
    ax2.axhline(FL_POSE,    color=C_POSE,    linewidth=0.9, linestyle="--", alpha=0.5)
    ax2.axhline(FL_GESTURE, color=C_GESTURE, linewidth=0.9, linestyle="--", alpha=0.5)
    ax2.axhline(FL_OBJECT,  color=C_OBJECT,  linewidth=0.9, linestyle="--", alpha=0.5)

    # 標注 f_l 位置
    ax2.text(t.max() * 1.001, FL_POSE,    f"$f^l$={FL_POSE}",    va="center",
             fontsize=8, color=C_POSE)
    ax2.text(t.max() * 1.001, FL_GESTURE, f"$f^l$={FL_GESTURE}", va="center",
             fontsize=8, color=C_GESTURE)
    ax2.text(t.max() * 1.001, FL_OBJECT,  f"$f^l$={FL_OBJECT}",  va="center",
             fontsize=8, color=C_OBJECT)

    ax2.set_ylabel("Effective FPS (avg per user)")
    ax2.set_title(f"({_disp['b']}) Effective FPS per Service (below $f^l$ = unsatisfied)")
    ax2.legend(loc="upper right", framealpha=0.85)
    ax2.grid(True, alpha=0.25)

# ── Panel (c): Latency per service ─────────────────────────────────────────
# 依 experiment_ctrl_v2.LATENCY_MODE 決定要畫 e2e 還是純 service(gRPC) 延遲，
# 與 nsats/eff_fps 的判定基準保持一致。
_LAT_COL    = "svclat_ms" if LATENCY_MODE == "service" else "latency_ms"
_LAT_LABEL  = "service latency" if LATENCY_MODE == "service" else "e2e latency"

if "c" in PANELS:
    ax3 = _ax_of["c"]

    ax3.plot(t, df[f"avg_pose_{_LAT_COL}"],    color=C_POSE,    linestyle=S_POSE,    linewidth=1.5, drawstyle="steps-post", label=f"pose ({_LAT_LABEL})")
    ax3.plot(t, df[f"avg_gesture_{_LAT_COL}"], color=C_GESTURE, linestyle=S_GESTURE, linewidth=1.5, drawstyle="steps-post", label=f"gesture ({_LAT_LABEL})")
    ax3.plot(t, df[f"avg_object_{_LAT_COL}"],  color=C_OBJECT,  linestyle=S_OBJECT,  linewidth=1.5, drawstyle="steps-post", label=f"YOLOv8n ({_LAT_LABEL})")

    ax3.axhline(100, color="gray", linewidth=0.9, linestyle="--", alpha=0.6)
    ax3.text(2, 105, "100 ms (QoS alert threshold)", fontsize=8, color="gray")

    ax3.set_ylabel("Avg Latency (ms)")
    ax3.set_title(f"({_disp['c']}) Average {_LAT_LABEL.title()} per Service")
    ax3.set_ylim(bottom=0)
    ax3.legend(loc="upper right", framealpha=0.85)
    ax3.grid(True, alpha=0.25)

# ── Panel (d): Invalid (wasted) FPS ────────────────────────────────────────
if "d" in PANELS:
    ax4 = _ax_of["d"]

    if "wasted_pose_fps" in df.columns:
        # 以使用者為單位的 all-or-nothing 浪費（見 _recompute_wasted）：使用者只要
        # 有任一訂閱服務未達 f_l，其訂閱之全部服務 real_fps 皆計入浪費，而非僅計
        # 「real - eff」這段延遲超標的差額，與 nsats/A_sat 的達標判準一致。
        wasted_pose    = df["wasted_pose_fps"]
        wasted_gesture = df["wasted_gesture_fps"]
        wasted_object  = df["wasted_object_fps"]
    else:
        wasted_pose    = (df["real_pose_fps"]    - df["eff_pose_fps"]).clip(lower=0)
        wasted_gesture = (df["real_gesture_fps"] - df["eff_gesture_fps"]).clip(lower=0)
        wasted_object  = (df["real_object_fps"]  - df["eff_object_fps"]).clip(lower=0)

    ax4.plot(t, wasted_pose,    color=C_POSE,    linestyle=S_POSE,    linewidth=1.2, label="pose (wasted)")
    ax4.plot(t, wasted_gesture, color=C_GESTURE, linestyle=S_GESTURE, linewidth=1.2, label="gesture (wasted)")
    ax4.plot(t, wasted_object,  color=C_OBJECT,  linestyle=S_OBJECT,  linewidth=1.2, label="YOLOv8n (wasted)")

    ax4.set_ylabel("FPS")
    ax4.set_title(f"({_disp['d']}) Invalid FPS per Service  (unsatisfied user: all real FPS wasted; else real − effective)")
    ax4.legend(loc="upper right", framealpha=0.85)
    ax4.grid(True, alpha=0.25)

# ── Panel (e): Node deployment Gantt ───────────────────────────────────────
SVC_COLORS  = {"pose": C_POSE, "gesture": C_GESTURE, "object": C_OBJECT}
SVC_HATCHES = {"pose": H_POSE, "gesture": H_GESTURE, "object": H_OBJECT}
SVC_DISPLAY = {"pose": "pose", "gesture": "gesture", "object": "YOLOv8n"}

# 全域固定的真實 IP → node 編號對照表（全篇論文共用同一組 7 台 AWS 節點，
# 用同一份對照表確保同一節點在不同實驗的圖裡永遠對應同一個編號），避免圖上
# 直接暴露內部 IP。依 IP 字串排序訂出 node1~node7；未知 IP（理論上不會出現）
# 則原樣顯示，不會讓程式壞掉。
NODE_DISPLAY = {
    "ip-172-31-0-204":  "node1",
    "ip-172-31-1-171":  "node2",
    "ip-172-31-10-12":  "node3",
    "ip-172-31-11-132": "node4",
    "ip-172-31-13-134": "node5",
    "ip-172-31-14-68":  "node6",
    "ip-172-31-40-239": "node7",
}

if "e" in PANELS:
    ax5 = _ax_of["e"]

    if "node_topology" not in df.columns:
        ax5.text(0.5, 0.5,
                 "node_topology column not in CSV\n"
                 "(re-run experiment with updated experiment_ctrl.py)",
                 ha="center", va="center", transform=ax5.transAxes,
                 color="gray", fontsize=10)
        ax5.set_title(f"({_disp['e']}) Service Deployment per Node  [data not available]")
    else:
        # Parse JSON → list of dicts
        def _parse_topo(v):
            try:
                return json.loads(v) if isinstance(v, str) else {}
            except Exception:
                return {}

        parsed = [_parse_topo(v) for v in df["node_topology"]]

        # Collect all (node, service) pairs that ever appear
        all_pairs = set()
        for d in parsed:
            for node, svcs in d.items():
                for svc in svcs:
                    all_pairs.add((node, svc))

        pairs = sorted(all_pairs)          # sorted: node asc, then service asc
        n_rows = len(pairs)

        if n_rows == 0:
            ax5.text(0.5, 0.5, "no topology data recorded",
                     ha="center", va="center", transform=ax5.transAxes, color="gray")
        else:
            y_labels = [f"{NODE_DISPLAY.get(node, node)}  /  {SVC_DISPLAY.get(svc, svc)}" for node, svc in pairs]

            for row_idx, (node, svc) in enumerate(pairs):
                deployed = [1 if (node in d and svc in d.get(node, {})) else 0
                            for d in parsed]
                color = SVC_COLORS.get(svc, "gray")
                hatch = SVC_HATCHES.get(svc, "")
                ax5.fill_between(t, row_idx - 0.42, row_idx + 0.42,
                                 where=deployed,
                                 color=color, alpha=0.75, hatch=hatch,
                                 edgecolor="black", linewidth=0, step="post")
                # thin background stripe for readability
                ax5.axhspan(row_idx - 0.5, row_idx + 0.5,
                            color="black", alpha=0.03)

            ax5.set_yticks(range(n_rows))
            ax5.set_yticklabels(y_labels, fontsize=9)
            ax5.set_ylim(-0.6, n_rows - 0.4)

            # Service legend
            patches = [mpatches.Patch(facecolor=SVC_COLORS[s], edgecolor="black", hatch=SVC_HATCHES.get(s, ""),
                                       label=SVC_DISPLAY.get(s, s), alpha=0.75)
                       for s in ["pose", "gesture", "object"] if s in SVC_COLORS]
            ax5.legend(handles=patches, loc="upper right", framealpha=0.85)

        ax5.set_title(f"({_disp['e']}) Service Deployment per Node")
        ax5.grid(axis="x", alpha=0.25)

    ax5.set_ylabel("Node  /  Service")
    ax5.set_xlabel("Elapsed Time (s)")

# ── Panel (f): Supply vs Demand ────────────────────────────────────────────
if "f" in PANELS:
    ax6 = _ax_of["f"]
    ax6r = ax6.twinx()

    C_SUPPLY = "#009E73"   # green
    C_DEMAND = "#CC79A7"   # purple
    C_UTIL   = "#56B4E9"   # sky blue

    HAS_SUPPLY = "deployed_capacity_fps" in df.columns and "total_allocated_fps" in df.columns

    if HAS_SUPPLY:
        supply = df["deployed_capacity_fps"].fillna(0)
        demand = df["total_allocated_fps"].fillna(0)
        util   = df["capacity_utilization"].fillna(0) if "capacity_utilization" in df.columns else None

        ax6.plot(t, supply, color=C_SUPPLY, linewidth=2.0, drawstyle="steps-post", label="Supply (deployed capacity)")
        ax6.plot(t, demand, color=C_DEMAND, linewidth=1.5, drawstyle="steps-post", label="Demand (allocated FPS)")
        ax6.fill_between(t, demand, supply, step="post", alpha=0.12, color=C_SUPPLY, label="Unused capacity")

        if util is not None:
            ax6r.plot(t, util, color=C_UTIL, linewidth=1.5, drawstyle="steps-post", label="Utilization")
            ax6r.axhline(1.0, color="black", linewidth=0.7, linestyle="--", alpha=0.4)
            ax6r.set_ylim(-0.05, 1.15)
            ax6r.set_ylabel("Utilization", color=C_UTIL)
            ax6r.tick_params(axis="y", labelcolor=C_UTIL)

        h1, l1 = ax6.get_legend_handles_labels()
        h2, l2 = ax6r.get_legend_handles_labels()
        ax6.legend(h1 + h2, l1 + l2, loc="upper left", framealpha=0.85)
        ax6.set_ylim(bottom=0)
    else:
        ax6.text(0.5, 0.5, "deployed_capacity_fps / total_allocated_fps not in CSV",
                 ha="center", va="center", transform=ax6.transAxes, color="gray", fontsize=10)

    ax6.set_ylabel("FPS")
    ax6.set_title(f"({_disp['f']}) Service Supply vs Demand")
    ax6.grid(True, alpha=0.25)

# ── Panel (g): Q score vs N_sat ────────────────────────────────────────────
if "g" in PANELS:
    ax7 = _ax_of["g"]
    ax7r = ax7.twinx()

    C_QSCORE = "#F0E442"   # yellow
    C_NSAT2  = "#D55E00"   # same as C_NSAT

    if "q_score" in df.columns:
        ax7.plot(t, df["q_score"], color=C_QSCORE, linewidth=2.0,
                 drawstyle="steps-post", label="$q$ (avg eff_fps/f_h)")
        ax7.set_ylabel("Q Score (avg, 0-1)", color=C_QSCORE)
        ax7.tick_params(axis="y", labelcolor=C_QSCORE)
        ax7.set_ylim(-0.02, 1.05)

    ax7r.plot(t, df["nsats"], color=C_NSAT2, linewidth=1.5,
              drawstyle="steps-post", label="$N_{sat}$")
    ax7r.set_ylabel("$N_{sat}$", color=C_NSAT2)
    ax7r.tick_params(axis="y", labelcolor=C_NSAT2)
    ax7r.set_ylim(bottom=0)

    h1, l1 = ax7.get_legend_handles_labels()
    h2, l2 = ax7r.get_legend_handles_labels()
    ax7.legend(h1 + h2, l1 + l2, loc="upper left", framealpha=0.85)
    ax7.set_title(f"({_disp['g']}) Achieved Quality $q$ vs $N_{{sat}}$")
    ax7.grid(True, alpha=0.25)

# ── 事件標注（冷啟動、pod 擴充）────────────────────────────────────────────
pod_events = df[df["pods_added"] > 0][["elapsed_s", "pods_added"]]
for _, row in pod_events.iterrows():
    for ax in axes:
        ax.axvline(row["elapsed_s"], color="purple", linewidth=0.7,
                   linestyle=":", alpha=0.5)

# 冷啟動期標注（第一個 nsats>0 之前）
first_sat_t = df[df["nsats"] > 0]["elapsed_s"].iloc[0] if (df["nsats"] > 0).any() else 0
for ax in axes:
    ax.axvspan(0, first_sat_t, alpha=0.06, color="red")
axes[0].text(first_sat_t / 2, axes[0].get_ylim()[1] * 0.85, "cold\nstart",
             ha="center", fontsize=8, color="red", alpha=0.7)

# ── 所有 Panel 顯示橫軸刻度 ─────────────────────────────────────────────────
for ax in axes:
    ax.tick_params(axis="x", labelbottom=True, labelsize=9)
    ax.set_xlabel("Elapsed Time (s)", fontsize=9)

Path(OUT).parent.mkdir(parents=True, exist_ok=True)
plt.savefig(OUT, bbox_inches="tight")
print(f"Saved: {OUT}")
