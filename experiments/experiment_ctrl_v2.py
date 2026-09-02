"""
experiment_ctrl_v2.py - AR Emulator 實驗控制器（合併版）
=========================================================
將 AR_emulator_nosub.py 的邏輯合併為 in-process EmulatorInstance，
每個 instance 在獨立 thread 中執行自己的 asyncio event loop。

差異（對比 experiment_ctrl.py）：
  - 不再使用 subprocess.Popen；每個使用者對應一個 EmulatorInstance 物件
  - terminate() 直接送 DELETE 給 agent，不依賴 SIGINT → emulator → agent 鏈
  - 可透過 wait_for_connection() 即時得知是否成功分配 agent（准入狀態）
  - 生命週期完整可控，不再有殘留使用者問題

使用方式:
    python experiment_ctrl_v2.py --scenario 1 --url http://10.52.52.111:30004
    python experiment_ctrl_v2.py --scenario 2 --url http://10.52.52.111:30004 \\
        --profile user_profiles.json
    python experiment_ctrl_v2.py --scenario 3 --url http://10.52.52.111:30004 --fail-node pdclab
    python3 experiment_ctrl_v2.py --scenario 2 --url http://172.31.22.137:30004
    python3 experiment_ctrl_v2.py --scenario 1 --url http://172.31.22.137:30004
    
    
"""

import argparse
import asyncio
import csv
import json
import logging
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Thread, Event, Lock

import requests
import websockets

# ── Constants ─────────────────────────────────────────────────────────────────

FPS_THRESHOLDS = {
    "pose":    10,
    "gesture": 10,
    "object":  10,
}

SERVICES              = ["gesture", "pose", "object"]
AGENT_MANAGER_IP      = "10.52.52.111"
AGENT_MANAGER_WS_PORT = 30008
# 判定門檻（100ms）現在由 agent 端自己套用（agent/agent_refactored.py 的
# LATENCY_THRESHOLD_MS），GET /metrics 回傳的 eff_fps 已經是套用過門檻的結果。

# 「端到端延遲」要看哪一段：
#   "e2e"     - client 送出 → client 收到（含網路 + agent 排隊 + gRPC 推論，預設）
#   "service" - agent → service 的純 gRPC 呼叫耗時（不含 client↔agent 網路）
# 同時決定 eff_fps / nsats 的判定基準（兩者皆會完整寫入 CSV，此處只影響判定與 summary 顯示）。
LATENCY_MODE          = "service"


# ── EmulatorInstance ──────────────────────────────────────────────────────────

class EmulatorInstance:
    """
    Single in-process AR emulator — 輕量版。

    Agent 容器改為自產幀（見 agent/agent_refactored.py 的 self-driving frame
    generator），不再需要 client 維持對 agent 的資料面 websocket。這個物件現在
    只做兩件事：
      1. 跟 AgentManager 完成一次性的准入 handshake，取得 agent_ip/port/admitted
      2. 結束時透過 HTTP DELETE 通知 agent 自我終止

    eff_fps 等指標改由 EmulatorManager 直接輪詢 agent 的 GET /metrics 取得，
    不再依賴這個物件內部的逐幀累加（那套邏輯正是先前「幽靈 FAILED」bug 的根源：
    wait_for_connection() 舊版要等收到第一筆資料面訊息才算成功，若該訊息因
    thread/GIL 排程問題遲遲沒被處理，使用者就會在情境結束時才被回溯性判定
    FAILED，即使 admission 早就成功）。
    """

    def __init__(self, uid, services,
                 agent_manager_ip=AGENT_MANAGER_IP,
                 agent_manager_ws_port=AGENT_MANAGER_WS_PORT):
        self.uid           = uid
        self.services      = list(services)
        self._services_arg = ",".join(services)
        self._am_ip        = agent_manager_ip
        self._am_ws_port   = agent_manager_ws_port

        # Agent assignment（AgentManager 回傳後寫入）
        self.agent_ip      = ""
        self.agent_port    = "0"
        self.agent_ws_port = "0"
        self.admitted      = None    # controller 准入結果（True/False），AgentManager 交接時回傳

        self._stop      = Event()      # 外部呼叫 stop() 時設定
        self._main_done = Event()      # _main coroutine 結束時設定
        self._loop   = None            # asyncio event loop（worker thread 內設定）
        self._ws     = None            # 目前的 websocket（供外部關閉用）
        self._thread = None

    # ── Public API ────────────────────────────────────────────────────────

    def start(self):
        self._thread = Thread(target=self._run, daemon=True,
                              name=f"emu-{self.uid}")
        self._thread.start()

    def stop(self):
        """
        可靠的關閉流程：
        1. 送 DELETE 給 agent HTTP → agent 通知 controller → agent 自我終止
        2. 關閉 WebSocket（若 AgentManager handshake 仍在進行中）
        3. 等待 thread 結束
        """
        # Step 1: 通知 agent（同步，controller 才能正確移除使用者）
        if self.agent_ip and self.agent_port != "0":
            try:
                r = requests.delete(
                    f"http://{self.agent_ip}:{self.agent_port}/subscribe",
                    timeout=3)
                logging.info(f"[CONTROL] uid={self.uid} stop DELETE "
                             f"{self.agent_ip}:{self.agent_port} -> HTTP {r.status_code}")
            except Exception as e:
                logging.error(f"[CONTROL] uid={self.uid} stop DELETE "
                              f"{self.agent_ip}:{self.agent_port} FAILED (agent 可能未收到終止通知，"
                              f"容器將殘留): {type(e).__name__}: {e}")

        # Step 2: 關閉 WebSocket，讓 asyncio 任務自然退出
        self._stop.set()
        if self._loop and self._ws:
            try:
                asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
            except Exception:
                pass

        # Step 3: 等待 thread
        if self._thread:
            self._thread.join(timeout=6)

    def wait_for_connection(self, timeout=None) -> bool:
        """等待 AgentManager 完成 handoff。回傳 True 表示 admission 成功
        （admitted is True 且拿到 agent_ip）。不再等待任何資料面訊息，
        所以不會再出現「admission 早就成功、但因資料面卡住而被回溯性判定
        FAILED」的幽靈案例。"""
        deadline = time.time() + timeout if timeout is not None else None
        while deadline is None or time.time() < deadline:
            if self._stop.is_set():
                return False
            if self._main_done.is_set():
                return bool(self.agent_ip) and self.admitted is True
            time.sleep(0.1)
        return False

    # ── Internal: thread entry ────────────────────────────────────────────

    def _run(self):
        asyncio.run(self._main())

    async def _main(self):
        self._loop = asyncio.get_running_loop()
        try:
            await self._connect_agent_manager()
        except Exception as e:
            logging.error(f"[DEBUG uid={self.uid}] _connect_agent_manager() raised: "
                          f"{type(e).__name__}: {e}")
        finally:
            logging.warning(f"[DEBUG uid={self.uid}] after _connect_agent_manager: "
                            f"agent_ip={self.agent_ip!r} agent_ws_port={self.agent_ws_port!r} "
                            f"admitted={self.admitted!r} stop={self._stop.is_set()}")
            self._main_done.set()  # 通知 wait_for_connection：_main 已結束

    # ── WebSocket 連線 ────────────────────────────────────────────────────

    async def _connect_agent_manager(self):
        uri = f"ws://{self._am_ip}:{self._am_ws_port}/{self._services_arg}"
        async with websockets.connect(uri, ping_interval=None) as ws:
            self._ws = ws
            await self._recv_agent_manager(ws)
        self._ws = None

    async def _recv_agent_manager(self, ws):
        """接收 AgentManager 回傳的 agent 分配訊息：'<ip> <port> <ws_port> <admitted>'
        第 4 個欄位是 controller 的准入判斷結果（1/0），舊協定（3 個欄位）視為已接受。"""
        async for msg in ws:
            if isinstance(msg, bytes):
                msg = msg.decode()
            parts = msg.strip().split()
            if len(parts) >= 3 and len(parts[0]) > 3:
                self.agent_ip      = parts[0]
                self.agent_port    = parts[1]
                self.agent_ws_port = parts[2]
                self.admitted      = bool(int(parts[3])) if len(parts) >= 4 else True
                await ws.close()
                return


# ── EmulatorManager ────────────────────────────────────────────────────────────

class EmulatorManager:
    """管理所有 EmulatorInstance 的生命週期。"""

    def __init__(self,
                 agent_manager_ip=AGENT_MANAGER_IP,
                 agent_manager_ws_port=AGENT_MANAGER_WS_PORT):
        self._am_ip      = agent_manager_ip
        self._am_ws_port = agent_manager_ws_port
        self._lock       = Lock()
        self._active     = {}   # uid → EmulatorInstance
        self._metrics_cache = {}   # uid → 上一次成功輪詢到的 agent /metrics 回應（逾時降級用）
        self._poll_pool  = ThreadPoolExecutor(max_workers=10)

    def spawn(self, uid, services) -> "EmulatorInstance":
        inst = EmulatorInstance(
            uid=uid,
            services=services,
            agent_manager_ip=self._am_ip,
            agent_manager_ws_port=self._am_ws_port,
        )
        with self._lock:
            self._active[uid] = inst
        inst.start()
        return inst

    def wait_for_connection(self, uid, timeout=None) -> bool:
        with self._lock:
            inst = self._active.get(uid)
        return inst.wait_for_connection(timeout=timeout) if inst else False

    def terminate(self, uid):
        with self._lock:
            inst = self._active.pop(uid, None)
            self._metrics_cache.pop(uid, None)
        if inst:
            inst.stop()

    def terminate_all(self):
        with self._lock:
            instances = list(self._active.values())
            self._active.clear()
            self._metrics_cache.clear()
        threads = [Thread(target=inst.stop) for inst in instances]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

    def _poll_one(self, uid, inst) -> dict:
        """輪詢單一 agent 的 GET /metrics。短 timeout + 失敗時沿用上一次快照
        （優雅降級：這一秒沒更新到不代表使用者有問題，agent 可能只是當下比較忙），
        並記一筆 [METRICS] 警告 —— 這行 log 同時也是資源競爭的即時診斷訊號。"""
        data = None
        if inst.agent_ip and inst.agent_port != "0":
            try:
                r = requests.get(f"http://{inst.agent_ip}:{inst.agent_port}/metrics", timeout=2)
                r.raise_for_status()
                data = r.json()
                self._metrics_cache[uid] = data
            except Exception as e:
                logging.warning(f"[METRICS] uid={uid} agent busy/timeout: {type(e).__name__}: {e}")
        if data is None:
            data = self._metrics_cache.get(uid, {})

        entry = {"services": inst.services, "service_fps": {},
                 "service_eff_fps": {}, "service_avg_svclat_ms": {}}
        for svc in inst.services:
            m = data.get(svc, {})
            entry["service_fps"][svc]          = m.get("fps", 0)
            entry["service_eff_fps"][svc]       = m.get("eff_fps", 0)
            entry["service_avg_svclat_ms"][svc] = m.get("avg_svclat_ms", 0.0)
        return entry

    def read_metrics(self) -> dict:
        """
        對每個 active 使用者的 agent 平行輪詢 GET /metrics，取代舊版靠 client
        端逐幀累加的方式（那套機制正是「幽靈 FAILED」的根源）。
        回傳格式與舊版相容，供 Recorder 使用：
        { uid: { "services", "service_fps", "service_eff_fps",
                 "service_avg_svclat_ms" } }
        （不再有 service_avg_latency_ms / recv_fps / send_fps —— 這些原本量的是
        client↔agent 這段，agent 自產幀之後這段已經不存在。）
        """
        with self._lock:
            snapshot = dict(self._active)
        if not snapshot:
            return {}
        uids  = list(snapshot.keys())
        insts = list(snapshot.values())
        results = list(self._poll_pool.map(lambda i: self._poll_one(*i), zip(uids, insts)))
        return dict(zip(uids, results))

    def compute_nsats(self, metrics) -> int:
        nsats = 0
        for uid, m in metrics.items():
            eff_fps = m["service_eff_fps"]
            if all(eff_fps.get(s, 0) >= FPS_THRESHOLDS.get(s, 0)
                   for s in m["services"]):
                nsats += 1
        return nsats


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def get_status(url):
    try:
        return requests.get(f"{url}/status", timeout=5).json()
    except Exception:
        return {}


def node_failure(url, node_name):
    try:
        requests.post(f"{url}/alert",
                      json={"alertType": "workernode_failure",
                            "alertContent": {"nodeName": node_name}},
                      timeout=5)
    except Exception:
        pass


def node_recovery(url, node_name):
    try:
        requests.post(f"{url}/noderecovery",
                      json={"nodeName": node_name}, timeout=5)
    except Exception:
        pass


# ── Recorder ───────────────────────────────────────────────────────────────────

class Recorder:
    def __init__(self, csv_path, url, emulator_mgr, interval=1, enable_status_poll=True):
        self.csv_path           = csv_path
        self.url                = url
        self.mgr                = emulator_mgr
        self.interval           = interval
        self.enable_status_poll = enable_status_poll
        self.stop_evt           = Event()
        self.t0                 = time.time()
        self._rows              = []
        self._cumulative_pod_changes = 0
        self._last_reconcile_ts      = 0

    def start(self):
        self._thread = Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.stop_evt.set()
        self._thread.join()

    def log_event(self, event_name, detail=""):
        elapsed = round(time.time() - self.t0, 1)
        print(f"  [{elapsed:>7.1f}s] {event_name:40s} {detail}")

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
                "real_pose_fps", "real_gesture_fps", "real_object_fps",
                "eff_pose_fps",  "eff_gesture_fps",  "eff_object_fps",
                "avg_pose_latency_ms", "avg_gesture_latency_ms", "avg_object_latency_ms",
                "avg_pose_svclat_ms", "avg_gesture_svclat_ms", "avg_object_svclat_ms",
                "real_total_recv_fps",
                "node_status", "trigger_type",
                "solver_time_ms", "q_score",
                "phase1_triggered", "phase1_alloc_count",
                "node_topology",
                "per_user_detail",
            ])
            f.flush()

            while not self.stop_evt.wait(self.interval):
                st      = get_status(self.url) if self.enable_status_poll else {}
                metrics = self.mgr.read_metrics()

                elapsed  = round(time.time() - self.t0, 1)
                n_users  = st.get("n_users", 0) if st else len(metrics)
                # 優先採用 controller /status 回傳的權威 nsats（來自 solver 自己的
                # reconcile history，伺服器端算出，不受 client 端輪詢/資料面狀況影響）。
                # 只有在沒開 status 輪詢時才 fallback 回舊的、靠 client 端 eff_fps 算的版本。
                if st and "nsats" in st:
                    nsats = st["nsats"]
                else:
                    nsats = self.mgr.compute_nsats(metrics)
                sat_rate = round(nsats / n_users, 3) if n_users else 0

                nodes   = st.get("node_status", {}) if st else {}
                trigger = st.get("last_trigger", {}).get("type", "") if st else ""

                cur_ts = st.get("last_ts", 0) if st else 0
                if cur_ts != self._last_reconcile_ts:
                    added   = len(st.get("pods_added",   [])) if st else 0
                    deleted = len(st.get("pods_deleted", [])) if st else 0
                    self._cumulative_pod_changes += added + deleted
                    self._last_reconcile_ts = cur_ts
                else:
                    added = deleted = 0

                fps_buck    = {"pose": [], "gesture": [], "object": []}
                eff_buck    = {"pose": [], "gesture": [], "object": []}
                lat_buck    = {"pose": [], "gesture": [], "object": []}
                svclat_buck = {"pose": [], "gesture": [], "object": []}
                total_recv = 0
                per_user   = {}

                for uid, m in metrics.items():
                    sfps   = m.get("service_fps",            {})
                    seff   = m.get("service_eff_fps",        {})
                    # agent 自產幀之後，client↔agent 這段 e2e 延遲已經不存在了
                    # （沒有 client 送出時間可比對），這欄位固定回傳空字典，
                    # 對應的 avg_*_latency_ms CSV 欄位會是 0，保留欄位只是維持 schema 相容。
                    slat   = {}
                    sslat  = m.get("service_avg_svclat_ms",  {})
                    total_recv += sum(sfps.values())
                    per_user[str(uid)] = {
                        "services":   m["services"],
                        "fps":        {s: sfps.get(s, 0) for s in m["services"]},
                        "eff_fps":    {s: seff.get(s, 0) for s in m["services"]},
                        "latency_ms": {s: slat.get(s, 0) for s in m["services"]},
                        "svclat_ms":  {s: sslat.get(s, 0) for s in m["services"]},
                    }
                    for svc in SERVICES:
                        if svc in m["services"]:
                            if sfps.get(svc, 0) > 0:
                                fps_buck[svc].append(sfps[svc])
                            if seff.get(svc, 0) > 0:
                                eff_buck[svc].append(seff[svc])
                            if slat.get(svc, 0) > 0:
                                lat_buck[svc].append(slat[svc])
                            if sslat.get(svc, 0) > 0:
                                svclat_buck[svc].append(sslat[svc])

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
                    mean(svclat_buck["pose"]),
                    mean(svclat_buck["gesture"]),
                    mean(svclat_buck["object"]),
                    total_recv,
                    json.dumps(nodes), trigger,
                    st.get("solver_time_ms", 0) if st else 0,
                    st.get("q_score",        0) if st else 0,
                    int(st.get("phase1_triggered", False)) if st else 0,
                    st.get("phase1_alloc_count",   0) if st else 0,
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

        elapsed_col = col(0)
        nsats_col   = col(2)
        sat_time    = nsats_col[0] * elapsed_col[0]
        for i in range(1, len(elapsed_col)):
            sat_time += nsats_col[i] * (elapsed_col[i] - elapsed_col[i - 1])
        sat_time   = round(sat_time, 1)
        total_time = elapsed_col[-1]
        avg_nsats  = round(sat_time / total_time, 3) if total_time > 0 else 0

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
        if LATENCY_MODE == "service":
            lat_title, lat_cols = "Agent→Service 延遲", (25, 26, 27)
        else:
            lat_title, lat_cols = "端到端延遲", (22, 23, 24)
        print(f"  ── {lat_title} ────────────────────────────────")
        print(f"  平均回應時間 (controller 量) : {avg(col(15))} ms")
        print(f"  pose    avg latency  : {avg(col(lat_cols[0]))} ms")
        print(f"  gesture avg latency  : {avg(col(lat_cols[1]))} ms")
        print(f"  object  avg latency  : {avg(col(lat_cols[2]))} ms")
        print()
        print(f"  ── 真實量測 FPS（各服務，各使用者平均）────────")
        print(f"  pose    FPS total/eff : {avg(col(16))} / {avg(col(19))}")
        print(f"  gesture FPS total/eff : {avg(col(17))} / {avg(col(20))}")
        print(f"  object  FPS total/eff : {avg(col(18))} / {avg(col(21))}")
        print(f"  total recv            : {avg(col(28))} fps")
        print()
        print(f"  資料輸出             : {self.csv_path}")


# ── 情境 ───────────────────────────────────────────────────────────────────────

def _pick_services(uid, user_map):
    if uid in user_map:
        return list(user_map[uid])
    k = random.choices([1, 2, 3], weights=[4, 3, 1])[0]
    return random.sample(SERVICES, k)


def _spawn_and_watch(uid, svcs, mgr, rec):
    """啟動 emulator 並在背景 thread 中等待連線結果，不阻塞情境時序。"""
    rec.log_event(f"user-{uid} 訂閱服務", f"services={svcs}")
    inst = mgr.spawn(uid, svcs)

    def _watch():
        if inst.wait_for_connection(timeout=None):
            rec.log_event(f"user-{uid} connected",
                          f"agent={inst.agent_ip}:{inst.agent_ws_port}")
        else:
            rec.log_event(f"user-{uid} FAILED",
                          "no agent assigned (admission rejected, or stopped while waiting)")

    Thread(target=_watch, daemon=True).start()
    return inst


def scenario_normal(url, rec, mgr, user_map=None, arrival_order=None):
    ARRIVAL_INTERVAL = 5
    STAY_DURATION    = 60
    MAX              = 100  # 最多進場人數，超過的部分直接截斷

    user_map         = user_map or {}

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
                inst = _spawn_and_watch(uid, svcs, mgr, rec)
                if inst.wait_for_connection(timeout=None):  # 等到真正收到第一個服務結果
                    time.sleep(STAY_DURATION)
                    rec.log_event(f"unsubscribe user-{uid}", "")
                mgr.terminate(uid)  # 不論是否連線成功都要清理

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
    MAX              = 30  # 最多進場人數，超過的部分直接截斷
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
    time.sleep(30)

    rec.log_event("=== 統一退訂所有使用者（並行）===")
    def _unsub(uid):
        mgr.terminate(uid)
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

    time.sleep(max(0, FAIL_AT - (time.time() - t_start)))
    rec.log_event(f">>> 節點故障：{fail_node}", "alertType=workernode_failure")
    node_failure(url, fail_node)

    time.sleep(RECOVER_AT - FAIL_AT)
    rec.log_event(f">>> 節點恢復：{fail_node}", "noderecovery")
    node_recovery(url, fail_node)

    time.sleep(END_AT - RECOVER_AT)

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


# ── 主程式 ─────────────────────────────────────────────────────────────────────

def load_user_profiles(profile_path, scenario=2):
    """回傳 (user_map, arrival_order)。

    user_map      : {uid: services} 供 _pick_services() 查詢
    arrival_order : 依設定檔原始順序排列的到達序列，每個元素是
                    uid（int 或 str）或 None。None 代表設定檔中的佔位項
                    （例如 "[]": []），不會產生使用者，純粹讓抵達
                    序列多等一個 ARRIVAL_INTERVAL。
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
    parser = argparse.ArgumentParser(
        description="AR Emulator 實驗控制器 v2（in-process emulator）")
    parser.add_argument("--scenario", type=int, choices=[1, 2, 3], required=True)
    parser.add_argument("--url",       default="http://localhost:5000")
    parser.add_argument("--fail-node", default="pdclab")
    parser.add_argument("--seed",      type=int, default=42)
    parser.add_argument("--profile",   default=None,
                        help="user_profiles.json 路徑")
    parser.add_argument("--no-status", action="store_true",
                        help="停用 /status API 輪詢（減少 controller 負擔；關閉後 node_topology / q_score / supply 等欄位為空）")
    args = parser.parse_args()

    random.seed(args.seed)

    # 載入用戶訂閱設定檔
    profile_path = args.profile
    if profile_path is None:
        for name in ("user_profiles.json", "user_subscriptions.json"):
            candidate = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
            if os.path.exists(candidate):
                profile_path = candidate
                break
    user_map, arrival_order = load_user_profiles(profile_path, scenario=args.scenario)

    st = get_status(args.url)
    if not st:
        print(f"無法連線到 {args.url}，請確認 controller 是否運行")
        sys.exit(1)

    solver_mode = st.get("solver_mode", "unknown")
    ts          = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "results", solver_mode)
    os.makedirs(results_dir, exist_ok=True)
    csv_out = os.path.join(results_dir, f"s{args.scenario}_{ts}.csv")
    log_out = os.path.join(results_dir, f"s{args.scenario}_{ts}.log")

    # 監控每個 agent 的控制狀況（准入 handshake、DELETE 終止是否真的送達）。
    # 之前 logging 從未呼叫 basicConfig，[CONTROL]/[METRICS] 等 info/warning log
    # 只會在預設 root logger 下被吃掉或散落 stderr，事後無法查證；現在統一寫檔，
    # 才能在事後比對「agent 容器有沒有殘留」跟「DELETE 到底有沒有打到 agent」。
    logging.basicConfig(filename=log_out,
                        format='%(asctime)s %(levelname)s: %(message)s',
                        level=logging.INFO)

    print(f"\n{'='*60}")
    print(f" 實驗情境 {args.scenario}  |  controller: {args.url}")
    print(f" solver_mode={solver_mode}")
    print(f" 輸出 CSV   : {csv_out}")
    print(f" 控制紀錄   : {log_out}")
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

    mgr = EmulatorManager()
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
