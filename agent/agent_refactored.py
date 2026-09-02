import asyncio
import json
import logging
import os
import queue
import struct
import sys
import threading
import time
from typing import Annotated, Dict
from fastapi import FastAPI, Form, Request, BackgroundTasks
import grpc
import requests
import uvicorn
import websockets
import argparse
import base64

# Protobuf modules
import gesture_pb2
import gesture_pb2_grpc
import pose_pb2
import pose_pb2_grpc
import inference_pb2
import inference_pb2_grpc

# ---------------------------------------------------------
# Configuration & Globals
# ---------------------------------------------------------
parser = argparse.ArgumentParser(description="Agent Startup")
parser.add_argument("--ip", type=str, default='10.52.52.50', help="Agent IP outside")
parser.add_argument("--port", type=int, default=8888, help="Agent Port")
parser.add_argument("--ws-port", type=int, default=8889, help="Agent Websocket Port")
parser.add_argument("--service", action="append", nargs=4, metavar=('NAME', 'IP', 'PORT', 'FREQ'), default=[])
parser.add_argument("--controller-ip", type=str, default='10.52.52.126', help="Controller IP")
parser.add_argument("--controller-port", type=int, default=30004, help="Controller Port")



try:
    args, unknown = parser.parse_known_args()
    AgentIP_outside = args.ip
    AgentIP = '0.0.0.0'
    AgentPort = args.port
    AgentWebsocketPort = args.ws_port
    initial_services = args.service
    
    ControllerIP = args.controller_ip
    ControllerPort = args.controller_port
except Exception as e:
    AgentIP_outside = '10.52.52.50'
    AgentIP = '0.0.0.0'
    AgentPort = 8888
    AgentWebsocketPort = 8889
    initial_services = []
    ControllerIP = '10.52.52.126'
    ControllerPort = 30004


# gRPC 呼叫逾時（秒）：純粹當作「下游服務被打爆時避免 worker thread 永久卡死」的安全網，
# 跟 QoS 判定門檻（100ms）是兩件事，故意設得寬鬆一些，不用來評斷延遲是否合格。
GRPC_CALL_TIMEOUT_S = 0.5

# QoS 判定門檻（ms）：跟 experiment_ctrl_v2.py / rebuild_metrics_from_agent_logs.py 的
# LATENCY_THRESHOLD_MS 保持一致，這樣 /metrics 端點回傳的 eff_fps 才跟事後用 log 重建出來
# 的數字對得上。
LATENCY_THRESHOLD_MS = 100.0

# 自產幀模式：不再依賴 emulator 透過 websocket 送幀，agent 收到 /subscribe 讓某個 service
# 變成 ready 之後，自己對這個 service 直接發 gRPC 請求，頻率跟著 controller 分配的
# svc.freq 走（不是寫死的固定值）。
_TEST_IMAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "1280hand.jpg")

log_dir = "logs"
os.makedirs(log_dir, exist_ok=True)
logging.basicConfig(
    filename=os.path.join(log_dir, f"Agent_Refactored_{AgentIP_outside}_{AgentPort}.log"),
    format='%(asctime)s %(levelname)s: %(message)s',
    level=logging.INFO
)

app = FastAPI()

# Global variables for cross-thread communication
global_responses = []
recvFPS = 0
returnFPS = 0

# ---------------------------------------------------------
# Object-Oriented Framework for Services
# ---------------------------------------------------------
class ServiceWorker:
    """Base class to encapsulate common logic for different AI services."""
    def __init__(self, name: str):
        self.name = name
        self.ip = ""
        self.port = 0
        self.freq = 0.0
        self.is_ready = False
        
        self.req_queue = queue.Queue()
        self.channel = None
        self.stub = None

        self.send_fps = 0
        self.result_fps = 0
        self.thread = None

        # 供 GET /metrics 查詢用的每秒滾動窗口（跟上面 send_fps/result_fps 平行，
        # 那組是給 counting_FPS() 印 log 用、每秒被它自己歸零，兩邊互不干擾）
        self._metrics_lock = threading.Lock()
        self._window_fps = 0
        self._window_eff_fps = 0
        self._window_svclat_sum = 0.0
        self._window_svclat_count = 0
        self.last_metrics = {"fps": 0, "eff_fps": 0, "avg_svclat_ms": 0.0}

    def update_config(self, ip: str, port: int, freq: float):
        """Update connection parameters and restart loop if newly ready."""
        endpoint_changed = (ip != self.ip or port != self.port)
        self.ip = ip
        self.port = port
        self.freq = freq

        if self.ip and self.ip != "null" and self.port > 0:
            if endpoint_changed or not self.is_ready:
                self.connect()
            if not self.is_ready:
                self.is_ready = True
                self.thread = threading.Thread(target=self._run_loop, daemon=True)
                self.thread.start()
                logging.info(f"{self.name} service is now ready to run and adjust frequency!")
                _ensure_self_driving_started(self)
            else:
                logging.info(f"{self.name} frequency updated to {freq} fps (no reconnect)")

    def connect(self):
        """Establish or re-establish gRPC connection (make-before-break)."""
        new_channel = grpc.insecure_channel(f"{self.ip}:{self.port}")
        old_channel = self.channel
        self.channel = new_channel
        self._create_stub()
        if old_channel:
            old_channel.close()
        logging.info(f"Connected to {self.name} service, {self.ip}:{self.port}")

    def reset(self):
        """Clear the configuration and halt processing."""
        self.ip = ""
        self.port = 0
        self.freq = 0.0
        self.is_ready = False
        if self.channel:
            self.channel.close()
            self.channel = None

    def _create_stub(self):
        """Override in subclasses to initialize specific gRPC stub."""
        raise NotImplementedError

    def invoke_grpc(self, request_bytes: bytes) -> str:
        """Override in subclasses to format the request and execute the call."""
        raise NotImplementedError

    def build_self_driving_request(self, image_bytes: bytes):
        """Override：自產幀模式專用，用靜態測試圖預先建好這個 service 專屬格式的
        gRPC request 物件（例如 gesture 需要 base64 編碼，pose/object 用原始
        bytes）。內容從頭到尾不變，只在 worker 啟動時建一次，之後重複使用，
        不用每次呼叫都重新編碼。"""
        raise NotImplementedError

    def invoke_grpc_request(self, request) -> str:
        """Override：直接送出已經建好的 request 物件（自產幀模式用），
        跟 invoke_grpc(request_bytes) 不同，這裡不用每次從 raw bytes 重建。"""
        raise NotImplementedError

    def _run_loop(self):
        """Main loop fetching from queue, applying frequency limit, and submitting tasks."""
        while True:
            if not self.is_ready:
                time.sleep(1)
                continue

            try:
                # freq<=0 時 0.1s 輪詢間隔（同 _self_driving_worker），讓 freq 恢復後
                # 的反應延遲壓在 100ms 內
                timeout = 1 / self.freq if self.freq > 0 else 0.1
                req = self.req_queue.get(timeout=timeout)
                while not self.req_queue.empty():
                    req = self.req_queue.get_nowait()
            except queue.Empty:
                continue

            if self.freq <= 0:
                # 暫停轉發（controller 判定此 service 不再服務）：清空佇列但不送出，
                # 持續輪詢以便之後 freq 恢復時能立即接手，不需要重新 reset/reconnect
                continue

            t = time.time()
            try:
                self._process_and_forward(req)
            except Exception as e:
                logging.error(f"{self.name} processing error: {e}")

            sleeptime = (1 / self.freq) - (time.time() - t)
            if sleeptime > 0:
                time.sleep(sleeptime)

    def _process_and_forward(self, request_bytes: bytes):
        """Wrapper around invoke_grpc that handles profiling and response appending."""
        self.send_fps += 1
        try:
            t = time.time()
            ret_str = self.invoke_grpc(request_bytes)
            grpc_lat_ms = (time.time() - t) * 1000
            logging.info(f"{self.name} detection inference time = {grpc_lat_ms / 1000:.4f}")
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
                # 服務卡住超過 GRPC_CALL_TIMEOUT_S：不讓這幀無聲消失，改記成逾時上限值，
                # 讓延遲統計看得出「這裡發生過打爆/卡住」，而不是誤以為單純沒送出。
                ret_str = ""
                grpc_lat_ms = GRPC_CALL_TIMEOUT_S * 1000
                logging.warning(f"{self.name} gRPC call timed out after {GRPC_CALL_TIMEOUT_S}s")
            else:
                logging.error(f"{self.name} gRPC transmission failed: {e}")
                raise
        except Exception as e:
            logging.error(f"{self.name} gRPC transmission failed: {e}")
            raise

        self.result_fps += 1

        with self._metrics_lock:
            self._window_fps += 1
            if grpc_lat_ms < LATENCY_THRESHOLD_MS:
                self._window_eff_fps += 1
            self._window_svclat_sum += grpc_lat_ms
            self._window_svclat_count += 1

        # Attach service name tag, 4-byte client token (ID) and agent→service latency (ms)
        idx = struct.unpack('i', request_bytes[-4:])[0]
        final_res = f"{self.name}|{ret_str}{idx:04} {grpc_lat_ms:.2f}"
        global_responses.append(final_res)

    def _self_driving_process(self, request):
        """跟 _process_and_forward() 做一樣的事（送 gRPC、量延遲、更新
        /metrics 用的計數器、寫 detection inference time log），但用在自產幀
        模式：request 是預先建好的物件，不用每次從 raw bytes 重建；也不寫
        global_responses——那個 list 只有 client 真的連上 agent 的 websocket
        才會有人消費（send_messages()），自產幀模式下永遠不會有 client 連
        上來，寫了就是隻進不出，整個 container 生命週期無限累積、洩漏記憶體。"""
        self.send_fps += 1
        try:
            t = time.time()
            self.invoke_grpc_request(request)
            grpc_lat_ms = (time.time() - t) * 1000
            logging.info(f"{self.name} detection inference time = {grpc_lat_ms / 1000:.4f}")
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
                grpc_lat_ms = GRPC_CALL_TIMEOUT_S * 1000
                logging.warning(f"{self.name} gRPC call timed out after {GRPC_CALL_TIMEOUT_S}s")
            else:
                logging.error(f"{self.name} gRPC transmission failed: {e}")
                return
        except Exception as e:
            logging.error(f"{self.name} gRPC transmission failed: {e}")
            return

        self.result_fps += 1

        with self._metrics_lock:
            self._window_fps += 1
            if grpc_lat_ms < LATENCY_THRESHOLD_MS:
                self._window_eff_fps += 1
            self._window_svclat_sum += grpc_lat_ms
            self._window_svclat_count += 1


# ---------------------------------------------------------
# Subclasses: Concrete implementations for each service
# ---------------------------------------------------------
class PoseService(ServiceWorker):
    def __init__(self):
        super().__init__("pose")

    def _create_stub(self):
        self.stub = pose_pb2_grpc.MirrorStub(self.channel)

    def invoke_grpc(self, request_bytes: bytes) -> str:
        req = pose_pb2.FrameRequest(image_data=request_bytes[:-4])
        response = self.stub.SkeletonFrame(req, timeout=GRPC_CALL_TIMEOUT_S)
        return response.skeletons + " "

    def build_self_driving_request(self, image_bytes: bytes):
        return pose_pb2.FrameRequest(image_data=image_bytes)

    def invoke_grpc_request(self, request) -> str:
        response = self.stub.SkeletonFrame(request, timeout=GRPC_CALL_TIMEOUT_S)
        return response.skeletons + " "


class GestureService(ServiceWorker):
    def __init__(self):
        super().__init__("gesture")

    def _create_stub(self):
        self.stub = gesture_pb2_grpc.GestureRecognitionStub(self.channel)

    def invoke_grpc(self, request_bytes: bytes) -> str:
        req = gesture_pb2.RecognitionRequest(image=base64.b64encode(request_bytes[:-4]))
        response = self.stub.Recognition(req, timeout=GRPC_CALL_TIMEOUT_S)
        res_dict = json.loads(response.action)
        return f"{res_dict['Left']} {res_dict['Right']} "

    def build_self_driving_request(self, image_bytes: bytes):
        # base64 編碼只做這一次；自產幀模式下圖片內容不變，沒必要每次呼叫都重編碼。
        return gesture_pb2.RecognitionRequest(image=base64.b64encode(image_bytes))

    def invoke_grpc_request(self, request) -> str:
        response = self.stub.Recognition(request, timeout=GRPC_CALL_TIMEOUT_S)
        res_dict = json.loads(response.action)
        return f"{res_dict['Left']} {res_dict['Right']} "


class ObjectService(ServiceWorker):
    def __init__(self):
        super().__init__("object")

    def _create_stub(self):
        self.stub = pose_pb2_grpc.MirrorStub(self.channel)

    def invoke_grpc(self, request_bytes: bytes) -> str:
        req = pose_pb2.FrameRequest(image_data=request_bytes[:-4])
        response = self.stub.SkeletonFrame(req, timeout=GRPC_CALL_TIMEOUT_S)
        return response.skeletons + " "

    def build_self_driving_request(self, image_bytes: bytes):
        return pose_pb2.FrameRequest(image_data=image_bytes)

    def invoke_grpc_request(self, request) -> str:
        response = self.stub.SkeletonFrame(request, timeout=GRPC_CALL_TIMEOUT_S)
        return response.skeletons + " "


# Initialize all services
services: Dict[str, ServiceWorker] = {
    "pose": PoseService(),
    "gesture": GestureService(),
    "object": ObjectService()
}

# ---------------------------------------------------------
# Self-driving（取代 emulator 經 websocket 送幀）
# ---------------------------------------------------------
# 每個 service 各自一條直接呼叫 gRPC 的 worker thread，完全不經過
# req_queue/_run_loop()（那條路徑保留給 v3/舊版 client 端送幀相容用，
# 自產幀模式下沒有人塞資料進去，讓它閒置即可，成本可忽略）。
# request 物件只在 worker 啟動時建一次（見 build_self_driving_request()），
# 之後重複使用——內容從頭到尾不變，不用每次呼叫都重新組 bytes/重新 base64
# 編碼，也不用 queue 做 producer/consumer 交接。
_self_driving_lock          = threading.Lock()
_self_driving_started_names = set()
_self_driving_image_lock    = threading.Lock()
_self_driving_image_bytes   = None


def _load_self_driving_image() -> bytes:
    global _self_driving_image_bytes
    with _self_driving_image_lock:
        if _self_driving_image_bytes is None:
            with open(_TEST_IMAGE_PATH, "rb") as f:
                _self_driving_image_bytes = f.read()
        return _self_driving_image_bytes


def _self_driving_worker(svc: "ServiceWorker", image_bytes: bytes):
    request = svc.build_self_driving_request(image_bytes)
    next_t = time.time()
    while svc.is_ready:
        if svc.freq <= 0:
            # 暫停送出（controller 判定此 service 不再服務）：不呼叫
            # _self_driving_process，定期檢查 freq 是否恢復。is_ready 仍為 True，
            # 只有 reset()（整個 agent unsubscribe）才會真正跳出這個迴圈。
            # 0.1s 輪詢間隔：讓 freq 恢復後的反應延遲壓在 100ms 內，換來暫停期間
            # 稍微多一點的空轉檢查次數（可忽略的 CPU 成本）。
            time.sleep(0.1)
            next_t = time.time()
            continue

        t = next_t
        svc._self_driving_process(request)
        freq   = svc.freq
        next_t = max(time.time(), t + 1.0 / freq)
        sleep  = next_t - time.time()
        if sleep > 0:
            time.sleep(sleep)
    with _self_driving_lock:
        _self_driving_started_names.discard(svc.name)
    logging.info(f"Self-driving worker for {svc.name} stopped (unsubscribed)")


def _ensure_self_driving_started(svc: "ServiceWorker"):
    with _self_driving_lock:
        if svc.name in _self_driving_started_names:
            return
        _self_driving_started_names.add(svc.name)
    try:
        image_bytes = _load_self_driving_image()
    except Exception as e:
        logging.error(f"Self-driving worker for {svc.name} failed to load test image "
                      f"{_TEST_IMAGE_PATH}: {e}")
        with _self_driving_lock:
            _self_driving_started_names.discard(svc.name)
        return
    logging.info(f"Self-driving worker started for {svc.name} (freq={svc.freq}fps)")
    threading.Thread(target=_self_driving_worker, args=(svc, image_bytes),
                     daemon=True, name=f"self-driving-{svc.name}").start()


def _metrics_window_loop():
    """每秒把各 service 的滾動窗口計數快照到 last_metrics，供 GET /metrics 查詢。"""
    while True:
        time.sleep(1)
        for s in services.values():
            with s._metrics_lock:
                fps     = s._window_fps
                eff     = s._window_eff_fps
                lat_sum = s._window_svclat_sum
                lat_cnt = s._window_svclat_count
                s._window_fps = 0
                s._window_eff_fps = 0
                s._window_svclat_sum = 0.0
                s._window_svclat_count = 0
            s.last_metrics = {
                "fps":           fps,
                "eff_fps":       eff,
                "avg_svclat_ms": round(lat_sum / lat_cnt, 2) if lat_cnt else 0.0,
            }


# ---------------------------------------------------------
# FastAPI Endpoints
# ---------------------------------------------------------
_NO_LOG_PATHS = {"/metrics", "/healthz"}


@app.middleware("http")
async def log_requests(request: Request, call_next):
    # /metrics 被 experiment_ctrl_v2.py 每秒輪詢一次、整場實驗持續不斷，
    # 對診斷沒有價值，記下來只是白白增加磁碟 I/O（磁碟 I/O 競爭已經是
    # 之前查到 controller 卡住的成因之一，同一類風險，能省就省）。
    if request.url.path not in _NO_LOG_PATHS:
        log_data = {"client_host": request.client.host, "client_port": request.client.port, "method": request.method, "path": request.url.path}
        logging.info(f"HTTP Request: {log_data}")
    return await call_next(request)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    """輕量、非阻塞：只讀記憶體快照，不做任何 gRPC 呼叫，
    讓 experiment_ctrl_v2.py 可以短 timeout 高頻率輪詢而不必擔心拖慢 agent。"""
    return {s.name: s.last_metrics for s in services.values() if s.is_ready}


@app.post("/subscribe")
async def subscribe(servicename: Annotated[str, Form()]):
    msg = f"[Subscribe] service={servicename}"
    print(msg)
    logging.info(msg)

    response = requests.post(
        f'http://{ControllerIP}:{ControllerPort}/subscribe', 
        {"ip": AgentIP_outside, "port": AgentPort, "serviceType": servicename}
    ).json()

    if servicename in services:
        services[servicename].update_config(
            ip=response.get('IP'),
            port=int(response.get('Port')),
            freq=float(response.get('Frequency'))
        )
        msg2 = f"[Subscribe] {servicename} configured -> IP={response.get('IP')}, Port={response.get('Port')}, Freq={response.get('Frequency')}"
        print(msg2)
        logging.info(msg2)
        
    return response


@app.post("/servicechange")
async def servicechange(request: Request):
    data = await request.json()
    servicename = data["servicename"]
    logging.info(f"change {servicename} service to IP: {data['ip']}, Port: {data['port']}, Frequency: {data['frequency']}")

    if servicename in services:
        services[servicename].update_config(
            ip=data["ip"],
            port=int(data["port"]),
            freq=float(data["frequency"])
        )
        
    return {"status": "200", "message": "OK"}


@app.delete("/subscribe")
async def unsubscribe(background_tasks: BackgroundTasks):
    msg = f"[Unsubscribe] resetting all services (AgentPort={AgentPort})"
    print(msg)
    logging.info(msg)

    for svc in services.values():
        svc.reset()

    body = {'ip': AgentIP_outside, 'port': AgentPort}
    requests.post(f'http://{ControllerIP}:{ControllerPort}/unsubscribe', json=body)

    msg2 = "[Unsubscribe] done, notified controller, container will exit"
    print(msg2)
    logging.info(msg2)

    # 回傳 response 後自我終止，停止容器（docker run --rm 會自動移除）
    background_tasks.add_task(_self_terminate)
    return {"status": "200", "message": "OK"}

def run_http_server():
    logging.info(f"Http server started on {AgentIP_outside}:{AgentPort}")
    uvicorn.run(app, host=AgentIP, port=AgentPort)

# ---------------------------------------------------------
# Global Performance Profiler
# ---------------------------------------------------------
def counting_FPS():
    global recvFPS, returnFPS
    while True:
        # Check if there was any activity to print
        total_activity = recvFPS + returnFPS + sum(s.send_fps + s.result_fps for s in services.values())
        if total_activity > 0:
            log_str = f"FPS: [receive AR: {recvFPS}, return AR: {returnFPS}]"
            for s in services.values():
                log_str += f" | {s.name}(send: {s.send_fps}, get: {s.result_fps})"
            logging.info(log_str)
            
        # Reset counters
        recvFPS = 0
        returnFPS = 0
        for s in services.values():
            s.send_fps = 0
            s.result_fps = 0
            
        time.sleep(1)

# ---------------------------------------------------------
# WebSocket Endpoints
# ---------------------------------------------------------
async def receive_messages(websocket):
    global recvFPS
    try:
        async for message in websocket:
            recvFPS += 1
            # Dispatch frame to all active services
            for svc in services.values():
                if svc.is_ready:
                    svc.req_queue.put(message)
    except Exception as e:
        logging.error(f"Exception while receiving websocket msg: {e}")

async def send_messages(websocket):
    global returnFPS
    try:
        while True:
            if global_responses:
                response_str = global_responses.pop(0)
                returnFPS += 1
                await websocket.send(response_str.encode('utf-8'))
            await asyncio.sleep(0.001)
    except Exception as e:
        logging.error(f"Exception while sending websocket msg: {e}")

async def handle_connection(websocket, path):
    global _watchdog_task
    if _watchdog_task and not _watchdog_task.done():
        _watchdog_task.cancel()

    client_ip, client_port = websocket.remote_address
    logging.info(f"WebSocket Client connected from {client_ip}:{client_port}")

    # Ensure profile thread runs once
    if not any(t.name == "fps_counter" for t in threading.enumerate()):
        threading.Thread(target=counting_FPS, name="fps_counter", daemon=True).start()

    receive_task = asyncio.create_task(receive_messages(websocket))
    send_task = asyncio.create_task(send_messages(websocket))
    await asyncio.gather(receive_task, send_task)

    # emulator 斷線：先通知 controller 退訂，再自我終止
    logging.info("WebSocket connection closed, notifying controller to unsubscribe")
    try:
        requests.post(
            f'http://{ControllerIP}:{ControllerPort}/unsubscribe',
            json={'ip': AgentIP_outside, 'port': AgentPort},
            timeout=5
        )
        logging.info("Unsubscribed from controller successfully")
    except Exception as e:
        logging.error(f"Failed to unsubscribe from controller: {e}")

    logging.info("Container self-terminating")
    _self_terminate()

_idle_timeout = 60  # 秒：啟動後無連線則自動退出（孤立容器保護）
_watchdog_task: asyncio.Task = None

async def _idle_watchdog():
    """啟動後若 _idle_timeout 秒內既無 WS 連線、也沒有任何 service 因 /subscribe 變成
    ready（自產幀模式下不會有 client 連上 agent 的 websocket，所以不能只看連線），
    才視為孤立容器，自我終止。"""
    try:
        await asyncio.sleep(_idle_timeout)
        if any(s.is_ready for s in services.values()):
            logging.info("Idle watchdog: service already active (self-driving), skip self-terminate")
            return
        logging.warning(f"No WebSocket connection or active service within {_idle_timeout}s, self-terminating (orphan guard)")
        _self_terminate()
    except asyncio.CancelledError:
        logging.info("Idle watchdog cancelled (WebSocket connection established)")

def _self_terminate():
    logging.info("Agent self-terminating")
    # os.kill(pid, SIGTERM) 不可靠：run_http_server() 跑在背景 thread，uvicorn
    # 嘗試安裝 signal handler 的行為在非主 thread 下不保證正常運作，實測會讓
    # SIGTERM 完全沒效果，agent 在「已退訂」之後仍持續送真實 gRPC 請求超過
    # 20-30 分鐘。os._exit() 是作業系統層級的強制結束，跳過所有 Python/
    # 第三方套件的 signal handler，保證真的終止。
    import os as _os
    _os._exit(0)

async def start_server():
    global _watchdog_task
    try:
        websocket_server = await websockets.serve(handle_connection, AgentIP, AgentWebsocketPort, ping_interval=None)
        print(f"WebSocket server started on ws://{AgentIP}:{AgentWebsocketPort}")
        logging.info(f"WebSocket server started on ws://{AgentIP}:{AgentWebsocketPort}")
        # 孤兒保護 watchdog 關閉：發現 controller 端某些 solver（例如 lsr）的
        # reconcile 沒有在 60 秒內把 /servicechange 推播給剛建立的 agent，會被
        # 這個 watchdog 誤判成孤立容器提前砍掉，導致明明 admission 成功、controller
        # 也還在處理中的 agent 平白死亡。不啟動這個 task 就不會再有這個誤殺。
        # 代價：真正的孤兒 container（controller 拒絕訂閱、之後也沒人清理）不會再
        # 自動消失，仍要靠 client 端在情境結束時送 DELETE /subscribe 清理。
        # _watchdog_task = asyncio.ensure_future(_idle_watchdog())
        await websocket_server.wait_closed()
    except Exception as e:
        logging.error(f"Failed to start WebSocket server: {e}")

# ---------------------------------------------------------
# Main Execution
# ---------------------------------------------------------
if __name__ == '__main__':
    print("Initializing Agent...")

    # Load initial service parameters if passed via terminal arguments
    try:
        for srv_name, srv_ip, srv_port, srv_freq in initial_services:
            if srv_name in services and srv_ip and str(srv_ip) != "0" and int(srv_port) != 0:
                services[srv_name].update_config(srv_ip, int(srv_port), float(srv_freq))
                logging.info(f"Initialized {srv_name} service with IP {srv_ip}, Port {srv_port}, Freq {srv_freq}")
            else:
                logging.warning(f"Failed to initialize service or unknown service: {srv_name}")
    except Exception as e:
        logging.error(f"Error parsing initial service arguments: {e}")

    app.debug = False
    threading.Thread(target=run_http_server, daemon=True).start()
    threading.Thread(target=_metrics_window_loop, daemon=True, name="metrics-window").start()

    # 使用更穩定的啟動方式
    loop = asyncio.get_event_loop()
    loop.run_until_complete(start_server())
    loop.run_forever()
