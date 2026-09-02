"""
experiment_ctrl.py - 真實 AR emulator 實驗控制器
==================================================
與 experiment_ctrl_v2.py 相同的情境流程/設定（arrival_order、MAX 截斷、
FIRST_MSG_TIMEOUT、connected/FAILED 判定），但改用真實 AR emulator 子進程
（subprocess，非 in-process asyncio）發送 WebSocket 流量，並從每個 emulator
的 metrics JSON 讀取實際量測的每服務 FPS，取代理論值。

FPS 閾值 (f_j^l, 來自 serviceSpec_mul.json frequencyLimit[1]):
    pose: 10 fps, gesture: 10 fps, object: 10 fps

AgentManager 位址預設 10.52.52.111:30008，可用 --agent-manager-ip /
--agent-manager-ws-port 覆蓋（例如指向 AWS 環境）。

使用方式:
    python experiment_ctrl.py --scenario 1 --url http://10.52.52.111:30004
    python experiment_ctrl.py --scenario 2 --url http://10.52.52.111:30004 \\
        --profile user_profiles.json
    python experiment_ctrl.py --scenario 3 --url http://10.52.52.111:30004 --fail-node pdclab

    python3 experiment_ctrl.py --scenario 2 --url http://172.31.22.137:30004 \\
        --agent-manager-ip 172.31.22.137 --agent-manager-ws-port 30008
"""

import argparse
import csv
import json
import os
import random
import signal
import subprocess
import sys
import time
from datetime import datetime
from threading import Thread, Event, Lock
import requests

# QoS 閾值 (f_j^l) — 對齊 serviceSpec_mul.json frequencyLimit[1]
# 可被 user_profiles.json 的 fps_thresholds 覆蓋
FPS_THRESHOLDS = {
    "pose":    10,
    "gesture": 10,
    "object":  10,
}

SERVICES = ["gesture", "pose", "object"]
POD_COLD_START = 8   # seconds
METRICS_DIR = "/tmp/ar_emulator_metrics"

# 對齊 experiment_ctrl_v2.py：AgentManager 位址（可用 --agent-manager-ip/--agent-manager-ws-port 覆蓋）
AGENT_MANAGER_IP      = "10.52.52.111"
AGENT_MANAGER_WS_PORT = 30008


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def subscribe(url, ip, port, service_types):
    """持續重試直到拿到真實 HTTP 回應，排除 timeout 造成的假性拒絕。"""
    while True:
        try:
            r = requests.post(f"{url}/subscribe",
                              json={"ip": ip, "port": port, "serviceTypes": service_types},
                              timeout=120)
            return r.status_code, r.json()
        except requests.exceptions.Timeout:
            time.sleep(2)   # 等 reconcile 釋放 lock 後重試
        except Exception as e:
            return 0, {"error": str(e)}


def subscribe_reason(code):
    if code == 200:  return "OK"
    if code == 503:  return "REJECTED(admission)"
    if code == 0:    return "ERROR(network)"
    return f"REJECTED(code={code})"


def unsubscribe(url, ip, port):
    while True:
        try:
            r = requests.post(f"{url}/unsubscribe",
                              json={"ip": ip, "port": port},
                              timeout=30)
            return r.status_code, r.json()
        except requests.exceptions.Timeout:
            time.sleep(2)
        except Exception as e:
            return 0, {"error": str(e)}


def node_failure(url, node_name):
    try:
        r = requests.post(f"{url}/alert",
                          json={"alertType": "workernode_failure",
                                "alertContent": {"nodeName": node_name}},
                          timeout=5)
        return r.status_code
    except Exception:
        return 0


def node_recovery(url, node_name):
    try:
        r = requests.post(f"{url}/noderecovery",
                          json={"nodeName": node_name},
                          timeout=5)
        return r.status_code
    except Exception:
        return 0


def get_status(url):
    try:
        r = requests.get(f"{url}/status", timeout=5)
        return r.json()
    except Exception:
        return {}


def _read_connected_ever(metrics_file) -> bool:
    """讀 metrics_file 的 connected_ever 欄位；檔案不存在/格式錯誤一律視為 False。"""
    if not os.path.exists(metrics_file):
        return False
    try:
        with open(metrics_file) as f:
            return bool(json.load(f).get("connected_ever", False))
    except Exception:
        return False


# ── AR Emulator process manager ───────────────────────────────────────────────

class EmulatorManager:
    """每個已訂閱用戶對應一個 AR emulator 子進程"""

    def __init__(self, emulator_path, metrics_dir=METRICS_DIR,
                 agent_manager_ip=AGENT_MANAGER_IP, agent_manager_ws_port=AGENT_MANAGER_WS_PORT):
        self.emulator_path = emulator_path
        self.emulator_dir  = os.path.dirname(os.path.abspath(emulator_path))
        self.metrics_dir   = metrics_dir
        self._am_ip        = agent_manager_ip
        self._am_ws_port   = agent_manager_ws_port
        self._lock  = Lock()
        self._active = {}  # uid -> {"process", "services", "metrics_file"}
        os.makedirs(metrics_dir, exist_ok=True)

    def spawn(self, uid, services):
        """啟動 emulator 子進程，以 emulator 所在目錄為 cwd（需要 1280hand.jpg）"""
        metrics_file = os.path.join(self.metrics_dir, f"ar_metrics_{uid}.json")
        if os.path.exists(metrics_file):
            os.remove(metrics_file)

        cmd = [
            sys.executable, self.emulator_path,
            str(uid),
            ",".join(services),
            "--metrics-file", metrics_file,
            "--agent-manager-ip", self._am_ip,
            "--agent-manager-ws-port", str(self._am_ws_port),
        ]
        log_path = os.path.join(self.metrics_dir, f"emulator_{uid}.log")
        log_file = open(log_path, "w")
        proc = subprocess.Popen(
            cmd,
            cwd=self.emulator_dir,           # 讓 emulator 找到 1280hand.jpg
            stdout=log_file,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid,            # 新 process group，方便整組 kill
        )
        with self._lock:
            self._active[uid] = {
                "process":      proc,
                "services":     list(services),
                "metrics_file": metrics_file,
                "log_file":     log_file,
            }

    def wait_for_connection(self, uid, timeout=None) -> bool:
        """對齊 experiment_ctrl_v2.py 的 EmulatorInstance.wait_for_connection()：
        阻塞直到收到第一筆真實服務結果（metrics_file 的 connected_ever=True）。
        因為這裡是 subprocess、無法直接讀物件屬性，改成輪詢 metrics_file 內容 +
        檢查子行程是否已經結束（結束但從未 connected_ever 過 = 失敗）。"""
        with self._lock:
            entry = self._active.get(uid)
        if not entry:
            return False
        proc         = entry["process"]
        metrics_file = entry["metrics_file"]

        deadline = time.time() + timeout if timeout is not None else None
        while deadline is None or time.time() < deadline:
            with self._lock:
                if uid not in self._active:   # 被外部 terminate() 了
                    return False
            if proc.poll() is not None:
                # 子行程已結束：只有在結束前真的 connected_ever 過才算成功
                # （理論上不會發生，因為 connected 後 emulator 會持續運行；
                #  這裡純粹防呆，避免子行程異常退出時 wait_for_connection 卡死）
                return _read_connected_ever(metrics_file)
            if _read_connected_ever(metrics_file):
                return True
            time.sleep(0.5)
        return False

    def terminate(self, uid):
        """終止 emulator 子進程：用 SIGINT 觸發 emulator 的 KeyboardInterrupt 清理
        （emulator 會對 Docker agent 發 DELETE /subscribe，agent 再通知 controller）"""
        with self._lock:
            entry = self._active.pop(uid, None)
        if not entry:
            return
        proc = entry["process"]
        try:
            entry.get("log_file") and entry["log_file"].close()
        except Exception:
            pass
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        except Exception:
            proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=5)   # 等 emulator 完成 unsubscribe 清理
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()

    def terminate_all(self):
        with self._lock:
            ids = list(self._active.keys())
        for uid in ids:
            self.terminate(uid)

    def read_metrics(self):
        """
        讀取所有活躍 emulator 的 metrics JSON。
        回傳: { uid: {
            "services": [...],
            "service_fps": {...},            # total received FPS per service
            "service_eff_fps": {...},        # effective FPS (latency < 100ms)
            "service_avg_latency_ms": {...}, # avg end-to-end latency per service
            "recv_fps": N, "send_fps": N
        } }
        """
        result = {}
        with self._lock:
            snapshot = dict(self._active)
        for uid, entry in snapshot.items():
            mf = entry["metrics_file"]
            if not os.path.exists(mf):
                continue
            try:
                with open(mf) as f:
                    data = json.load(f)
                result[uid] = {
                    "services":               entry["services"],
                    "service_fps":            data.get("service_fps",            {}),
                    "service_eff_fps":        data.get("service_eff_fps",        {}),
                    "service_avg_latency_ms": data.get("service_avg_latency_ms", {}),
                    "recv_fps":               data.get("recv_fps", 0),
                    "send_fps":               data.get("send_fps", 0),
                }
            except Exception:
                pass
        return result

    def compute_nsats(self, metrics):
        """
        滿足條件：用戶訂閱的每個服務 eff_fps（latency < 100ms）>= 閾值。
        沒有收到有效幀 = 不滿足，不使用 fallback。
        """
        nsats = 0
        for uid, m in metrics.items():
            svcs    = m["services"]
            eff_fps = m["service_eff_fps"]
            ok = all(eff_fps.get(s, 0) >= FPS_THRESHOLDS.get(s, 0) for s in svcs)
            if ok:
                nsats += 1
        return nsats


# ── Recorder ─────────────────────────────────────────────────────────────────

class Recorder:
    def __init__(self, csv_path, url, emulator_mgr, interval=3, enable_status_poll=True):
        self.csv_path            = csv_path
        self.url                 = url
        self.mgr                 = emulator_mgr
        self.interval            = interval
        self.enable_status_poll  = enable_status_poll
        self.stop_evt            = Event()
        self.t0                  = time.time()
        self._rows               = []
        self._cumulative_pod_changes = 0
        self._last_reconcile_ts  = 0   # 追蹤上次 reconcile 時間戳，避免重複計算 Pod 變更

    def start(self):
        self._thread = Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.stop_evt.set()
        self._thread.join()

    def log_event(self, event_name, detail=""):
        elapsed = round(time.time() - self.t0, 1)
        print(f"  [{elapsed:>7.1f}s] {event_name:35s} {detail}")

    def _loop(self):
        with open(self.csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "elapsed_s", "n_users", "nsats", "sat_rate",
                "pods_added", "pods_deleted", "cumulative_pod_changes",
                "total_pods", "effective_pods", "effective_pod_ratio",
                "deployed_capacity_fps", "total_allocated_fps", "capacity_utilization",
                "wasted_fps", "pods_per_sat_user",
                "avg_response_ms",
                # 真實量測 FPS（總收到）
                "real_pose_fps", "real_gesture_fps", "real_object_fps",
                # 有效 FPS（延遲 < 100ms）
                "eff_pose_fps", "eff_gesture_fps", "eff_object_fps",
                # 平均端到端延遲（ms）
                "avg_pose_latency_ms", "avg_gesture_latency_ms", "avg_object_latency_ms",
                "real_total_recv_fps",
                "node_status", "trigger_type",
                # 求解器指標
                "solver_time_ms", "q_score",
                # 兩階段遷移
                "phase1_triggered", "phase1_alloc_count",
                # 節點拓樸（JSON）
                "node_topology",
                # 每使用者明細（JSON）
                "per_user_detail",
            ])
            f.flush()

            while not self.stop_evt.wait(self.interval):
                st      = get_status(self.url) if self.enable_status_poll else {}
                metrics = self.mgr.read_metrics()

                elapsed  = round(time.time() - self.t0, 1)
                n_users  = st.get("n_users", 0) if st else len(metrics)
                nsats    = self.mgr.compute_nsats(metrics)
                sat_rate = round(nsats / n_users, 3) if n_users else 0

                nodes   = st.get("node_status", {}) if st else {}
                trigger = st.get("last_trigger", {}).get("type", "") if st else ""

                # 只在 reconcile 有更新時才計入（last_ts 不同代表新的一次 reconcile）
                cur_ts = st.get("last_ts", 0) if st else 0
                if cur_ts != self._last_reconcile_ts:
                    added   = len(st.get("pods_added",   [])) if st else 0
                    deleted = len(st.get("pods_deleted", [])) if st else 0
                    self._cumulative_pod_changes += added + deleted
                    self._last_reconcile_ts = cur_ts
                else:
                    added, deleted = 0, 0

                # 彙總各 emulator 的 FPS / 有效 FPS / 延遲
                fps_buck = {"pose": [], "gesture": [], "object": []}
                eff_buck = {"pose": [], "gesture": [], "object": []}
                lat_buck = {"pose": [], "gesture": [], "object": []}
                total_recv = 0
                per_user   = {}   # uid -> per-service detail dict

                for uid, m in metrics.items():
                    sfps  = m.get("service_fps",            {})
                    seff  = m.get("service_eff_fps",        {})
                    slat  = m.get("service_avg_latency_ms", {})
                    total_recv += m.get("recv_fps", 0)
                    per_user[str(uid)] = {
                        "services":    m["services"],
                        "fps":         {s: sfps.get(s, 0) for s in m["services"]},
                        "eff_fps":     {s: seff.get(s, 0) for s in m["services"]},
                        "latency_ms":  {s: slat.get(s, 0) for s in m["services"]},
                    }
                    for svc in SERVICES:
                        if svc in m["services"]:
                            if sfps.get(svc, 0) > 0:
                                fps_buck[svc].append(sfps[svc])
                            if seff.get(svc, 0) > 0:
                                eff_buck[svc].append(seff[svc])
                            if slat.get(svc, 0) > 0:
                                lat_buck[svc].append(slat[svc])

                def mean(vals):
                    return round(sum(vals) / len(vals), 2) if vals else 0

                row = [
                    elapsed, n_users, nsats, sat_rate,
                    added, deleted, self._cumulative_pod_changes,
                    st.get("total_pods",           0) if st else 0,
                    st.get("effective_pods",        0) if st else 0,
                    st.get("effective_pod_ratio",   0) if st else 0,
                    st.get("deployed_capacity_fps", 0) if st else 0,
                    st.get("total_allocated_fps",   0) if st else 0,
                    st.get("capacity_utilization",  0) if st else 0,
                    st.get("wasted_fps",            0) if st else 0,
                    st.get("pods_per_sat_user",     0) if st else 0,
                    st.get("avg_response_ms",       0) if st else 0,
                    mean(fps_buck["pose"]),
                    mean(fps_buck["gesture"]),
                    mean(fps_buck["object"]),
                    mean(eff_buck["pose"]),
                    mean(eff_buck["gesture"]),
                    mean(eff_buck["object"]),
                    mean(lat_buck["pose"]),
                    mean(lat_buck["gesture"]),
                    mean(lat_buck["object"]),
                    total_recv,
                    json.dumps(nodes), trigger,
                    st.get("solver_time_ms", 0) if st else 0,
                    st.get("q_score",        0) if st else 0,
                    int(st.get("phase1_triggered",   False)) if st else 0,
                    st.get("phase1_alloc_count", 0) if st else 0,
                    json.dumps(st.get("node_topology", {}) if st else {}),
                    json.dumps(per_user),
                ]
                w.writerow(row)
                f.flush()
                self._rows.append(row)

    def summary(self):
        if not self._rows:
            print("  (no data)")
            return

        def col(i):
            return [r[i] for r in self._rows]

        def avg(vals):
            v = [x for x in vals if x not in (0, float("inf"))]
            return round(sum(v) / len(v), 3) if v else 0

        # CSV column indices (0-based):
        # 0=elapsed 1=n_users 2=nsats 3=sat_rate 4=pods_added 5=pods_deleted
        # 6=cumulative 7=total_pods 8=eff_pods 9=eff_pod_ratio
        # 10=dep_cap_fps 11=alloc_fps 12=cap_util 13=wasted 14=pods_per_sat
        # 15=avg_response_ms
        # 16=real_pose_fps 17=real_gesture_fps 18=real_object_fps
        # 19=eff_pose_fps  20=eff_gesture_fps  21=eff_object_fps
        # 22=avg_pose_lat  23=avg_gesture_lat  24=avg_object_lat
        # 25=total_recv 26=node_status 27=trigger 28=per_user_detail

        # nsats × time: 用實際 elapsed 差值積分，避免 wait() 誤差累積
        elapsed_col = col(0)
        nsats_col   = col(2)
        sat_time = nsats_col[0] * elapsed_col[0]
        for i in range(1, len(elapsed_col)):
            dt = elapsed_col[i] - elapsed_col[i - 1]
            sat_time += nsats_col[i] * dt
        sat_time = round(sat_time, 1)
        total_time = elapsed_col[-1]
        avg_nsats = round(sat_time / total_time, 3) if total_time > 0 else 0
        print(f"  最大同時在線人數     : {max(col(1))}")
        print(f"  平均 nsats           : {avg_nsats}  (= {sat_time} / {total_time}s)")
        print(f"  平均滿足率           : {round(avg(col(3))*100, 1)}%")
        print(f"  ★ nsats × time       : {sat_time} 人·秒")
        print(f"  累計 Pod 變更次數    : {self._rows[-1][6]}")
        print()
        print(f"  ── 精準部署指標 ──────────────────────────────")
        print(f"  平均有效 Pod 率       : {round(avg(col(9))*100, 1)}%")
        print(f"  平均容量使用率        : {round(avg(col(12))*100, 1)}%")
        print(f"  平均無效投入 fps      : {avg(col(13))}")
        print(f"  平均每人 Pod 成本     : {avg(col(14))}")
        print()
        print(f"  ── 端到端延遲 ────────────────────────────────")
        print(f"  平均回應時間 (controller 量) : {avg(col(15))} ms")
        print(f"  pose    avg latency  : {avg(col(22))} ms")
        print(f"  gesture avg latency  : {avg(col(23))} ms")
        print(f"  object  avg latency  : {avg(col(24))} ms")
        print()
        print(f"  ── 真實量測 FPS（各服務，各使用者平均）────────")
        print(f"  pose    FPS total/eff : {avg(col(16))} / {avg(col(19))}")
        print(f"  gesture FPS total/eff : {avg(col(17))} / {avg(col(20))}")
        print(f"  object  FPS total/eff : {avg(col(18))} / {avg(col(21))}")
        print(f"  total recv            : {avg(col(25))} fps")
        print()
        print(f"  資料輸出             : {self.csv_path}")


# ── 情境 ──────────────────────────────────────────────────────────────────────

def _pick_services(uid, user_map):
    """user_map 有指定就用指定的，否則隨機選。"""
    if uid in user_map:
        return list(user_map[uid])
    k = random.choices([1, 2, 3], weights=[4, 3, 1])[0]
    return random.sample(SERVICES, k)


def _spawn_and_watch(uid, svcs, mgr, rec):
    """啟動 emulator 並在背景 thread 中等待連線結果，不阻塞情境時序。
    對齊 experiment_ctrl_v2.py 的同名函式。"""
    rec.log_event(f"user-{uid} 訂閱服務", f"services={svcs}")
    mgr.spawn(uid, svcs)

    def _watch():
        if mgr.wait_for_connection(uid, timeout=None):
            rec.log_event(f"user-{uid} connected", "")
        else:
            rec.log_event(f"user-{uid} FAILED",
                          "no agent assigned (admission rejected, or stopped while waiting)")

    Thread(target=_watch, daemon=True).start()


def scenario_normal(url, rec, mgr, user_map=None, arrival_order=None):
    ARRIVAL_INTERVAL = 5
    STAY_DURATION    = 60
    MAX              = 50  # 最多進場人數，超過的部分直接截斷

    user_map = user_map or {}

    if arrival_order:
        slots = arrival_order
    else:
        slots = list(range(1, 11))
    slots = slots[:MAX]

    timers = []

    rec.log_event("=== 情境 1：日常負載 開始 ===")
    t_start = time.time()

    for i, uid in enumerate(slots):
        if uid is None:
            rec.log_event("(空位，不加入使用者)")
        else:
            svcs = _pick_services(uid, user_map)

            def _join_and_leave(uid=uid, svcs=svcs):
                _spawn_and_watch(uid, svcs, mgr, rec)
                if mgr.wait_for_connection(uid, timeout=None):  # 等到真正收到第一個服務結果
                    time.sleep(STAY_DURATION)
                    rec.log_event(f"unsubscribe user-{uid}", "")
                mgr.terminate(uid)  # 不論是否連線成功都要清理（SIGINT → emulator 自行退訂）

            t = Thread(target=_join_and_leave, daemon=True)
            t.start()
            timers.append(t)

        if i < len(slots) - 1:
            next_t = t_start + (i + 1) * ARRIVAL_INTERVAL
            time.sleep(max(0, next_t - time.time()))

    rec.log_event("=== 所有使用者已加入，等待離開 ===")
    for t in timers:
        t.join()
    time.sleep(5)
    rec.log_event("=== 情境 1 結束 ===")


def scenario_overload(url, rec, mgr, user_map=None, arrival_order=None):
    ARRIVAL_INTERVAL = 5
    MAX              = 50   # 最多進場人數，超過的部分直接截斷
    user_map         = user_map or {}

    if arrival_order:
        slots = arrival_order
    else:
        total_users = max(20, max(user_map.keys()) if user_map else 20)
        slots       = list(range(1, total_users + 1))
    slots = slots[:MAX]

    admitted_users = []
    join_lock      = Lock()

    rec.log_event("=== 情境 2：高負載 開始 ===")
    t_start      = time.time()
    join_threads = []

    for i, uid in enumerate(slots):
        if uid is None:
            rec.log_event("(空位，不加入使用者)")
        else:
            svcs = _pick_services(uid, user_map)

            def _join(uid=uid, svcs=svcs):
                _spawn_and_watch(uid, svcs, mgr, rec)
                with join_lock:
                    admitted_users.append(uid)

            t = Thread(target=_join, daemon=True)
            t.start()
            join_threads.append(t)

        if i < len(slots) - 1:
            next_t = t_start + (i + 1) * ARRIVAL_INTERVAL
            time.sleep(max(0, next_t - time.time()))

    for t in join_threads:
        t.join()

    rec.log_event("=== 所有使用者已加入，觀察系統表現中 ===")
    time.sleep(120)

    rec.log_event("=== 統一退訂所有使用者（並行）===")
    def _unsub(uid):
        mgr.terminate(uid)   # SIGINT → emulator 自行退訂真實 agent
        rec.log_event(f"unsubscribe user-{uid}", "")

    threads = [Thread(target=_unsub, args=(uid,), daemon=True)
               for uid in admitted_users]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rec.log_event("=== 情境 2 結束 ===")


def scenario_node_failure(url, rec, mgr, fail_node, user_map=None, arrival_order=None):
    FAIL_AT    = 60
    RECOVER_AT = 150
    END_AT     = 210
    MAX        = 10   # 最多進場人數，超過的部分直接截斷
    user_map   = user_map or {}

    if arrival_order:
        slots = arrival_order
    else:
        slots = list(range(1, 11))
    slots = slots[:MAX]

    rec.log_event("=== 情境 3：節點突發故障 開始 ===")
    t_start      = time.time()
    join_threads = []
    joined_uids  = []

    for i, uid in enumerate(slots):
        if uid is None:
            rec.log_event("(空位，不加入使用者)")
        else:
            svcs = _pick_services(uid, user_map)

            def _join(uid=uid, svcs=svcs):
                _spawn_and_watch(uid, svcs, mgr, rec)

            t = Thread(target=_join, daemon=True)
            t.start()
            join_threads.append(t)
            joined_uids.append(uid)
        if i < len(slots) - 1:
            next_t = t_start + (i + 1) * 2
            time.sleep(max(0, next_t - time.time()))

    # 按真實時鐘觸發故障，不等慢速 subscribe 完成
    time.sleep(max(0, FAIL_AT - (time.time() - t_start)))
    rec.log_event(f">>> 節點故障：{fail_node}", "alertType=workernode_failure")
    node_failure(url, fail_node)

    time.sleep(RECOVER_AT - FAIL_AT)
    rec.log_event(f">>> 節點恢復：{fail_node}", "noderecovery")
    node_recovery(url, fail_node)

    time.sleep(END_AT - RECOVER_AT)

    # 等尚未完成的 subscribe（最多 10s）
    for t in join_threads:
        t.join(timeout=10)

    rec.log_event("清除使用者...")

    cleanup = [Thread(target=mgr.terminate, args=(uid,), daemon=True)
               for uid in joined_uids]
    for t in cleanup:
        t.start()
    for t in cleanup:
        t.join(timeout=15)

    rec.log_event("=== 情境 3 結束 ===")


# ── 主程式 ────────────────────────────────────────────────────────────────────

def _find_emulator():
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "../../ar_emulator/AR_emulator_nosub.py"),
    ]
    for c in candidates:
        p = os.path.normpath(c)
        if os.path.exists(p):
            return p
    return None


def load_user_profiles(profile_path, scenario=2):
    """對齊 experiment_ctrl_v2.py：回傳 (user_map, arrival_order)。

    user_map      : {uid: services} 供 _pick_services() 查詢
    arrival_order : 依設定檔原始順序排列的到達序列，每個元素是
                    uid（int 或 str）或 None。None 代表設定檔中的佔位項
                    （例如 "[]": []），不會產生使用者，純粹讓抵達
                    序列多等一個 ARRIVAL_INTERVAL。

    支援兩種格式:
      格式 A (scenario-based):  {"scenario_2": [{"id":1,"services":[...]}, ...]}
      格式 B (flat):            {"users": {"1": [...], "2": [...]}}
    """
    if not profile_path or not os.path.exists(profile_path):
        return {}, []
    with open(profile_path) as f:
        data = json.load(f)

    key = f"scenario_{scenario}"
    if key in data:
        user_map      = {entry["id"]: entry["services"] for entry in data[key]}
        arrival_order = [entry["id"] for entry in data[key]]
        return user_map, arrival_order

    if "users" in data:
        user_map      = {}
        arrival_order = []
        for k, v in data["users"].items():
            if k == "[]" or v == []:
                arrival_order.append(None)
                continue
            try:
                uid = int(k)
            except ValueError:
                uid = k
            user_map[uid] = v
            arrival_order.append(uid)
        return user_map, arrival_order

    return {}, []


def main():
    parser = argparse.ArgumentParser(description="AR Emulator 實驗控制器（真實 WebSocket 流量）")
    parser.add_argument("--scenario", type=int, choices=[1, 2, 3], required=True,
                        help="1=日常負載  2=超載  3=節點故障")
    parser.add_argument("--url", default="http://localhost:5000",
                        help="Controller URL")
    parser.add_argument("--fail-node", default="pdclab",
                        help="情境 3 故障的節點名稱")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--emulator-path", default=None,
                        help="AR_emulator_nosub.py 路徑（預設: 自動偵測）")
    parser.add_argument("--metrics-dir", default=METRICS_DIR,
                        help=f"emulator metrics JSON 目錄（預設: {METRICS_DIR}）")
    parser.add_argument("--profile", default=None,
                        help="user_profiles.json 路徑（指定後以檔案取代隨機服務選擇）")
    parser.add_argument("--no-status", action="store_true",
                        help="停用 /status API 輪詢（減少 controller 負擔；關閉後 node_topology / q_score / supply 等欄位為空）")
    parser.add_argument("--agent-manager-ip", default=AGENT_MANAGER_IP,
                        help=f"AgentManager IP（預設: {AGENT_MANAGER_IP}）")
    parser.add_argument("--agent-manager-ws-port", type=int, default=AGENT_MANAGER_WS_PORT,
                        help=f"AgentManager WebSocket port（預設: {AGENT_MANAGER_WS_PORT}）")
    args = parser.parse_args()

    random.seed(args.seed)

    # 載入用戶訂閱設定檔
    profile_path = args.profile
    if profile_path is None:
        for name in ("user_profiles.json", "user_subscriptions.json", ):
            candidate = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
            if os.path.exists(candidate):
                profile_path = candidate
                break
    user_map, arrival_order = load_user_profiles(profile_path, scenario=args.scenario)

    emulator_path = args.emulator_path or _find_emulator()
    if not emulator_path or not os.path.exists(emulator_path):
        print("錯誤: 找不到 AR_emulator_nosub.py，請用 --emulator-path 指定")
        sys.exit(1)

    emulator_dir = os.path.dirname(os.path.abspath(emulator_path))
    img_path = os.path.join(emulator_dir, "1280hand.jpg")
    if not os.path.exists(img_path):
        print(f"錯誤: {img_path} 不存在，AR emulator 需要此圖片檔案")
        sys.exit(1)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    st = get_status(args.url)
    if not st:
        print(f"無法連線到 {args.url}，請確認 controller 是否運行")
        sys.exit(1)

    solver_mode = st.get('solver_mode', 'unknown')
    results_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", solver_mode)
    os.makedirs(results_dir, exist_ok=True)
    csv_out = os.path.join(results_dir, f"s{args.scenario}_{ts}.csv")

    print(f"\n{'='*60}")
    print(f" 實驗情境 {args.scenario}  |  controller: {args.url}")
    print(f" solver_mode={solver_mode}  keep_pods_on_empty={st.get('keep_pods_on_empty', False)}")
    print(f" emulator   : {emulator_path}")
    print(f" AgentManager: {args.agent_manager_ip}:{args.agent_manager_ws_port}")
    print(f" metrics 目錄: {args.metrics_dir}")
    print(f" 輸出 CSV   : {csv_out}")
    print(f" FPS 閾值   : {FPS_THRESHOLDS}")
    if user_map:
        print(f" 用戶設定檔 : {profile_path} ({len(user_map)} 人)")
    else:
        print(f" 用戶設定檔 : 未指定（隨機選服務）")
    print(f"{'='*60}\n")

    enable_status = not args.no_status
    if not enable_status:
        print(" [!] --no-status 模式：停用 /status 輪詢")
        print("     受影響欄位（填 0）：n_users, node_topology, q_score,")
        print("     deployed_capacity_fps, total_allocated_fps, capacity_utilization,")
        print("     pods_added/deleted, solver_time_ms, phase1_*")
        print()

    mgr = EmulatorManager(emulator_path, metrics_dir=args.metrics_dir,
                          agent_manager_ip=args.agent_manager_ip,
                          agent_manager_ws_port=args.agent_manager_ws_port)
    rec = Recorder(csv_out, args.url, mgr, interval=1, enable_status_poll=enable_status)
    rec.start()

    try:
        if args.scenario == 1:
            scenario_normal(args.url, rec, mgr, user_map, arrival_order)
        elif args.scenario == 2:
            scenario_overload(args.url, rec, mgr, user_map, arrival_order)
        elif args.scenario == 3:
            scenario_node_failure(args.url, rec, mgr, args.fail_node, user_map, arrival_order)
    except KeyboardInterrupt:
        rec.log_event("KeyboardInterrupt — 終止所有 emulator")
    finally:
        mgr.terminate_all()
        rec.stop()

    print(f"\n{'='*60}")
    print(" 實驗摘要")
    print(f"{'='*60}")
    rec.summary()


if __name__ == "__main__":
    main()
