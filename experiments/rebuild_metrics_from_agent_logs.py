#!/usr/bin/env python3
"""
rebuild_metrics_from_agent_logs.py
====================================
用 agent 自己的 log（server 端權威資料）重新計算 experiment_ctrl_v2.py CSV 裡
「由 client 端 EmulatorInstance 自行追蹤」而受目前已知 bug 影響的欄位，其餘欄位
（來自 controller /status，server 端資料，不受影響）維持原始 CSV 的值不變。

背景：experiment_ctrl_v2.py 的 real_*_fps / eff_*_fps / avg_*_svclat_ms / nsats /
sat_rate / real_total_recv_fps / per_user_detail 這幾欄，是靠 client 端
EmulatorInstance._recv_agent() 收到 agent 回傳訊息才會更新；已確認在某些情況下
agent 明明成功處理並送出結果，client 端卻沒有正確收到/登記（bug 仍在追查中）。
但 agent 自己的 log 會忠實記錄每一次「X detection inference time = Y」，這是
agent→service 的純 gRPC 延遲——剛好就是這個系統 LATENCY_MODE="service" 設定下，
eff_fps / nsats 判定所使用的同一個指標，不是近似值。

用法：
    python3 rebuild_metrics_from_agent_logs.py \\
        --csv results/ga_bf/s2_20260715_221055.csv \\
        --agent-log-dir /home/logs \\
        --start-time "2026-07-15 22:10:55" \\
        --out results/ga_bf/s2_20260715_221055_corrected.csv

--start-time 可以從 CSV 檔名的時間戳推得（例如 s2_20260715_221055 → 2026-07-15 22:10:55），
也可以手動指定（例如已知 controller "Starting Reconcile Loop" 第一筆事件的實際時間）。

輸出的 CSV 跟原始 experiment_ctrl_v2.py 的 schema 完全一致，只是把受影響欄位換成
agent log 算出來的權威數字，其餘（controller 來源）欄位原封不動複製。
"""
import argparse
import csv
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

SERVICES = ["pose", "gesture", "object"]
LATENCY_THRESHOLD_MS = 100.0
FPS_THRESHOLD = 10

INFER_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(?P<ms>\d+) INFO: "
    r"(?P<svc>pose|gesture|object) detection inference time = (?P<sec>[\d.]+)"
)
CONN_OPEN_RE = re.compile(r"WebSocket Client connected from")
SELF_TERM_RE = re.compile(r"Agent self-terminating|Container self-terminating")


def parse_agent_log(path: Path):
    """回傳 {svc: [(datetime, latency_ms), ...]}，以及該 agent 第一次/最後一次出現推論紀錄的時間。"""
    per_svc = defaultdict(list)
    first_ts = None
    last_ts = None
    with path.open(errors="replace") as f:
        for line in f:
            m = INFER_RE.match(line)
            if not m:
                continue
            ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
            lat_ms = float(m.group("sec")) * 1000.0
            per_svc[m.group("svc")].append((ts, lat_ms))
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts
    return per_svc, first_ts, last_ts


def bucket_by_second(records):
    """[(datetime, latency_ms), ...] -> {epoch_second_int: [latency_ms, ...]}"""
    buckets = defaultdict(list)
    for ts, lat in records:
        buckets[int(ts.timestamp())].append(lat)
    return buckets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="原始 experiment_ctrl_v2.py 輸出的 CSV")
    ap.add_argument("--agent-log-dir", required=True, nargs="+",
                     help="agent log 所在目錄，可以給多個（例如兩台 host 各自的 /home/logs），"
                          "彼此用空白分隔")
    ap.add_argument("--start-time", required=True,
                     help='實驗起始的實際時間，格式 "YYYY-MM-DD HH:MM:SS"（對齊 CSV 的 elapsed_s=0）')
    ap.add_argument("--out", required=True, help="輸出的修正版 CSV 路徑")
    ap.add_argument("--threshold", type=int, default=FPS_THRESHOLD)
    ap.add_argument("--latency-ms", type=float, default=LATENCY_THRESHOLD_MS)
    args = ap.parse_args()

    start_dt = datetime.strptime(args.start_time, "%Y-%m-%d %H:%M:%S")
    start_epoch = int(start_dt.timestamp())

    log_files = []
    for d in args.agent_log_dir:
        log_dir = Path(d)
        found = sorted(log_dir.glob("Agent_Refactored_*.log")) or sorted(log_dir.glob("*.log"))
        if not found:
            print(f"警告：{log_dir} 底下沒找到任何 log 檔")
        log_files.extend(found)
    if not log_files:
        raise SystemExit(f"找不到任何 agent log 於 {args.agent_log_dir}")

    print(f"讀取 {len(log_files)} 份 agent log ...")

    # agents[i] = {"services": {svc: {epoch_sec: [lat_ms,...]}}, "first": ts, "last": ts, "name": str}
    agents = []
    for lf in log_files:
        per_svc, first_ts, last_ts = parse_agent_log(lf)
        if not per_svc:
            continue
        agents.append({
            "name": lf.name,
            "services": {svc: bucket_by_second(recs) for svc, recs in per_svc.items()},
            "first": first_ts,
            "last": last_ts,
        })
    print(f"其中 {len(agents)} 份有實際推論紀錄")

    # ── 讀原始 CSV，逐列重算受影響欄位 ──────────────────────────────────────
    with open(args.csv, newline="") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames
        rows = list(reader)

    out_rows = []
    for row in rows:
        elapsed_s = float(row["elapsed_s"])
        target_epoch = start_epoch + int(round(elapsed_s))

        fps_buck = {s: [] for s in SERVICES}
        eff_buck = {s: [] for s in SERVICES}
        svclat_buck = {s: [] for s in SERVICES}
        total_recv = 0
        per_user = {}
        nsats = 0
        n_agents_with_data = 0

        for ag in agents:
            agent_active = ag["first"] and ag["last"] and \
                ag["first"].timestamp() - 5 <= target_epoch <= ag["last"].timestamp() + 5
            if not agent_active:
                continue

            agent_services = list(ag["services"].keys())
            agent_fps = {}
            agent_eff = {}
            agent_lat = {}
            agent_satisfied = True
            has_any = False

            for svc in agent_services:
                lats = ag["services"][svc].get(target_epoch, [])
                if not lats:
                    agent_satisfied = False
                    agent_fps[svc] = 0
                    agent_eff[svc] = 0
                    agent_lat[svc] = 0.0
                    continue
                has_any = True
                fps = len(lats)
                eff = sum(1 for l in lats if l < args.latency_ms)
                avg_lat = sum(lats) / len(lats)

                agent_fps[svc] = fps
                agent_eff[svc] = eff
                agent_lat[svc] = round(avg_lat, 2)

                if svc in SERVICES:
                    fps_buck[svc].append(fps)
                    eff_buck[svc].append(eff)
                    svclat_buck[svc].append(avg_lat)
                total_recv += fps

                if eff < args.threshold:
                    agent_satisfied = False

            if has_any:
                n_agents_with_data += 1
                per_user[ag["name"]] = {
                    "services": agent_services,
                    "fps": agent_fps,
                    "eff_fps": agent_eff,
                    "svclat_ms": agent_lat,
                }
                if agent_satisfied:
                    nsats += 1

        def mean(vals):
            return round(sum(vals) / len(vals), 2) if vals else 0

        n_users = row.get("n_users") or n_agents_with_data
        n_users = int(n_users) if str(n_users).strip() else n_agents_with_data
        sat_rate = round(nsats / n_users, 3) if n_users else 0

        new_row = dict(row)  # 保留所有 controller 來源欄位不變
        new_row["nsats"] = nsats
        new_row["sat_rate"] = sat_rate
        new_row["real_pose_fps"]    = mean(fps_buck["pose"])
        new_row["real_gesture_fps"] = mean(fps_buck["gesture"])
        new_row["real_object_fps"]  = mean(fps_buck["object"])
        new_row["eff_pose_fps"]    = mean(eff_buck["pose"])
        new_row["eff_gesture_fps"] = mean(eff_buck["gesture"])
        new_row["eff_object_fps"]  = mean(eff_buck["object"])
        new_row["avg_pose_svclat_ms"]    = mean(svclat_buck["pose"])
        new_row["avg_gesture_svclat_ms"] = mean(svclat_buck["gesture"])
        new_row["avg_object_svclat_ms"]  = mean(svclat_buck["object"])
        new_row["real_total_recv_fps"] = total_recv
        new_row["per_user_detail"] = str(per_user)
        # e2e 延遲（avg_pose_latency_ms 等）無法從 agent log 重建（缺 client 送出時間），
        # 保留原始值供參考，但不可信；不動它，讓使用者自行判斷是否要用。

        out_rows.append(new_row)

    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(out_rows)

    print(f"\n寫入修正版 CSV: {args.out}")
    if out_rows:
        last = out_rows[-1]
        print(f"最後一列： nsats={last['nsats']} sat_rate={last['sat_rate']} "
              f"real_total_recv_fps={last['real_total_recv_fps']}")


if __name__ == "__main__":
    main()
