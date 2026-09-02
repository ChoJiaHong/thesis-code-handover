import argparse
import asyncio
import csv
import json
import logging
import os
import struct
import sys
import threading
import time
import requests
import websockets
#import cv2

# Per-frame send timestamps for latency calculation (idx -> send_time)
# Response format from agent: "svcname|<ret_str> <idx>" (ret_str ends with space)
_pending_send: dict = {}          # idx -> send_time (float)
_PENDING_MAX  = 1000              # keep at most this many pending entries

# Per-service effective FPS (latency < 100ms) and latency tracking
_svc_eff_fps    : dict = {}       # populated in __main__ac
_svc_lat_sum    : dict = {}       # sum of latency_ms per service in current second
_svc_lat_count  : dict = {}       # count of samples per service in current second
_lat_lock = threading.Lock()

LATENCY_THRESHOLD_MS = 100        # effective frame = latency < this value
FIRST_MSG_TIMEOUT = 120.0         # 對齊 experiment_ctrl_v2.py：等 agent 第一筆回傳的上限秒數

# True once the first tagged service result has ever been received (對齊
# experiment_ctrl_v2.py 的 EmulatorInstance.connected)；提供給
# experiment_ctrl.py 的 EmulatorManager.wait_for_connection() 透過 metrics_file 輪詢。
_connected_ever = False

# Per-frame latency CSV writer (initialised in __main__)
_frame_csv_writer = None
_frame_csv_lock   = threading.Lock()

#send photo to server
async def send_messages(websocket):
    global SendFPS

    # 每次建立新連線時清空舊的 pending，避免重連後 idx 重置造成延遲計算錯誤
    _pending_send.clear()

    idx = 0
    next_send_time = time.time()
    while True:
        try:
            webcamimg = image_byte
            t_send = time.time()
            message = webcamimg + struct.pack('I', idx)
            await websocket.send(message)

            # Record send timestamp; trim oldest entries to bound memory
            _pending_send[idx] = t_send
            if len(_pending_send) > _PENDING_MAX:
                oldest = min(_pending_send)
                del _pending_send[oldest]

            idx += 1
            SendFPS += 1

            next_send_time += 1 / sending_freq
            sleep_time = next_send_time - time.time()
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

        except websockets.exceptions.ConnectionClosed as e:
            print(f"send_messages: Connection closed by server: {str(e)}")
            break
        except Exception as e:
            print(f"send_messages error: {type(e).__name__}: {str(e)}")
            logging.error(f"send_messages error: {type(e).__name__}: {str(e)}")
            break

#receive result from server
async def receive_messages(websocket, first_msg_timeout=None):
    """first_msg_timeout：僅套用在第一筆訊息上（對齊 experiment_ctrl_v2.py 的
    FIRST_MSG_TIMEOUT——agent 連上但一直沒有任何回傳時，主動放棄而不是無限等待）。"""
    global RecvFPS, Agent_IP, Agent_Port, Agent_Websocket_Port, reconnect, service_recv_fps, _connected_ever

    first_received = False
    _debug_raw_logged = 0
    _debug_mismatch_logged = False
    _debug_wait_start = time.time()
    while True:
        try:
            if not first_received and first_msg_timeout is not None:
                response = await asyncio.wait_for(websocket.recv(), timeout=first_msg_timeout)
            else:
                response = await websocket.recv()

            try:
                if type(response) != str:
                    response = response.decode('utf-8')
            except Exception as e:
                logging.error(f"[DEBUG id={number}] response.decode() raised after "
                              f"{time.time() - _debug_wait_start:.1f}s: {type(e).__name__}: {e}")
                continue

            RecvFPS += 1
            first_received = True

            if _debug_raw_logged < 3:
                logging.warning(f"[DEBUG id={number}] raw response #{_debug_raw_logged}: {response!r}")
                _debug_raw_logged += 1

            # Service-tagged response: "svcname|<ret_str> <idx>"
            # ret_str always ends with a space, so the last token is the frame idx
            if '|' in response:
                svc_name, content = response.split('|', 1)
                if svc_name not in service_recv_fps and not _debug_mismatch_logged:
                    logging.warning(f"[DEBUG id={number}] svc_name={svc_name!r} not in "
                                    f"expected {list(service_recv_fps.keys())}, skipped: {response!r}")
                    _debug_mismatch_logged = True
                if svc_name in service_recv_fps:
                    service_recv_fps[svc_name] += 1
                    _connected_ever = True

                    # Compute end-to-end latency using the echoed frame idx
                    try:
                        frame_idx = int(content.split()[-1])
                        send_t = _pending_send.get(frame_idx, None)
                        if send_t is not None:
                            latency_ms = (time.time() - send_t) * 1000
                            if _frame_csv_writer is not None:
                                with _frame_csv_lock:
                                    _frame_csv_writer.writerow([time.time(), svc_name, frame_idx, round(latency_ms, 2)])
                            with _lat_lock:
                                _svc_lat_sum[svc_name]   = _svc_lat_sum.get(svc_name, 0.0) + latency_ms
                                _svc_lat_count[svc_name] = _svc_lat_count.get(svc_name, 0) + 1
                                if latency_ms < LATENCY_THRESHOLD_MS:
                                    _svc_eff_fps[svc_name] = _svc_eff_fps.get(svc_name, 0) + 1
                    except Exception:
                        pass
                continue

            result = response.split(' ')
            if len(result) == 3 and len(result[0]) > 3:
                print(f"Received agent assignment: {response}")
                Agent_IP = result[0]
                Agent_Port = result[1]
                Agent_Websocket_Port = result[2]
                logging.info(f"Receive new Agent, IP: {Agent_IP}, Port: {Agent_Port}, WebsocketPort: {Agent_Websocket_Port}")
                reconnect = True
                await websocket.close()
                break

        except asyncio.TimeoutError:
            print(f"receive_messages: no data within {first_msg_timeout}s, giving up")
            logging.warning(f"[DEBUG id={number}] FIRST_MSG_TIMEOUT fired after "
                            f"{time.time() - _debug_wait_start:.1f}s (limit={first_msg_timeout}s)")
            break
        except websockets.exceptions.ConnectionClosed as e:
            print(f"receive_messages: Connection closed by server: {str(e)}")
            break
        except Exception as e:
            print(f"receive_messages error: {type(e).__name__}: {str(e)}")
            logging.error(f"receive_messages error: {type(e).__name__}: {str(e)}")
            break

#start connection
async def websocket_client(ip, port, path="", first_msg_timeout=None):
    uri = f"ws://{ip}:{port}/{path}"
    try:
        print(f"Attempting to connect to {uri}...")
        async with websockets.connect(uri, ping_interval=None) as websocket:
            print(f"Connected to {uri}")
            logging.info(f"Connected to {uri}")

            send_task = asyncio.create_task(send_messages(websocket))
            receive_task = asyncio.create_task(receive_messages(websocket, first_msg_timeout=first_msg_timeout))

            done, pending = await asyncio.wait(
                [send_task, receive_task], 
                return_when=asyncio.FIRST_COMPLETED
            )
            
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                    
    except ConnectionRefusedError as e:
        error_msg = f"Connection refused to {uri}: {str(e)} (errno: {e.errno})"
        print(error_msg)
        logging.error(error_msg)
        raise  # 重新拋出異常讓main()處理
    except OSError as e:
        error_msg = f"Network error to {uri}: {str(e)} (errno: {e.errno})"
        print(error_msg)
        logging.error(error_msg)
        raise
    except websockets.exceptions.ConnectionClosed as e:
        error_msg = f"WebSocket connection closed to {uri}: {str(e)}"
        print(error_msg)
        logging.error(error_msg)
        raise
    except websockets.exceptions.InvalidURI as e:
        error_msg = f"Invalid URI {uri}: {str(e)}"
        print(error_msg)
        logging.error(error_msg)
        raise
    except Exception as e:
        error_msg = f"Unexpected connection error to {uri}: {type(e).__name__}: {str(e)}"
        print(error_msg)
        logging.error(error_msg)
        import traceback
        traceback.print_exc()  # 打印完整的堆疊追蹤
        raise

def countFPS():
    global SendFPS, RecvFPS, service_recv_fps
    while True:
        s, r = SendFPS, RecvFPS
        svc_snapshot = {k: v for k, v in service_recv_fps.items()}
        SendFPS = 0
        RecvFPS = 0
        for k in service_recv_fps:
            service_recv_fps[k] = 0

        # Snapshot and reset latency / effective-FPS counters
        with _lat_lock:
            eff_snapshot = dict(_svc_eff_fps)
            lat_sum_snap = dict(_svc_lat_sum)
            lat_cnt_snap = dict(_svc_lat_count)
            _svc_eff_fps.clear()
            _svc_lat_sum.clear()
            _svc_lat_count.clear()

        # Evict _pending_send entries older than 2 seconds
        now = time.time()
        stale = [k for k, t in list(_pending_send.items()) if now - t > 2.0]
        for k in stale:
            _pending_send.pop(k, None)

        # Per-service average latency (ms)
        avg_lat = {}
        for svc in svc_snapshot:
            cnt = lat_cnt_snap.get(svc, 0)
            avg_lat[svc] = round(lat_sum_snap.get(svc, 0.0) / cnt, 2) if cnt > 0 else 0.0

        svc_str = ', '.join(f"{k}: {v}" for k, v in svc_snapshot.items())
        eff_str = ', '.join(f"{k}: {eff_snapshot.get(k, 0)}" for k in svc_snapshot)
        lat_str = ', '.join(f"{k}: {avg_lat[k]}ms" for k in svc_snapshot)
        print(f"Send={s} Recv={r} | fps[{svc_str}] | eff[{eff_str}] | lat[{lat_str}]")
        logging.info(f"Send={s} Recv={r} | fps={svc_snapshot} | eff={eff_snapshot} | lat={avg_lat}")

        if metrics_file:
            try:
                data = {
                    "id": number,
                    "services": services_arg,
                    "recv_fps": r,
                    "send_fps": s,
                    "service_fps": svc_snapshot,
                    "service_eff_fps": {k: eff_snapshot.get(k, 0) for k in svc_snapshot},
                    "service_avg_latency_ms": avg_lat,
                    "agent_ip": Agent_IP,
                    "agent_port": Agent_Port,
                    "connected": bool(Agent_IP),
                    "connected_ever": _connected_ever,
                    "start_time": start_time,
                    "last_update": time.time(),
                }
                with open(metrics_file, 'w') as _f:
                    json.dump(data, _f)
            except Exception:
                pass
        time.sleep(1)

async def main():
    global Agent_IP, Agent_Port, Agent_Websocket_Port

    t = threading.Thread(target=countFPS)
    t.daemon = True
    t.start()

    path = services_arg
    print(f"Subscribing services: {path.split(',')}")

    # Step 1: connect to AgentManager once to get agent assignment
    print(f"Connecting to Agent Manager: {Agent_Manager_IP}:{Agent_Manager_Websocket_port}/{path}")
    logging.info(f"Connecting to ws://{Agent_Manager_IP}:{Agent_Manager_Websocket_port}/{path}")
    try:
        await websocket_client(Agent_Manager_IP, Agent_Manager_Websocket_port, path)
    except KeyboardInterrupt:
        raise
    except Exception as e:
        print(f"AgentManager connection failed: {type(e).__name__}: {e}")
        logging.error(f"AgentManager connection failed: {type(e).__name__}: {e}")
        return

    if not Agent_IP:
        print("No agent assigned, exiting.")
        logging.warning("No agent assigned after AgentManager connection.")
        return

    # Step 2: connect to assigned agent, retry up to 3 times on failure.
    # Never fall back to AgentManager — retries are for the same agent only.
    print(f"Connecting to Agent: {Agent_IP}:{Agent_Websocket_Port}")
    logging.info(f"Connecting to ws://{Agent_IP}:{Agent_Websocket_Port}")
    for attempt in range(3):
        try:
            await websocket_client(Agent_IP, Agent_Websocket_Port, first_msg_timeout=FIRST_MSG_TIMEOUT)
            break
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"Agent connection attempt {attempt + 1}/3 failed: {type(e).__name__}: {e}")
            logging.error(f"Agent connection attempt {attempt + 1}/3 failed: {type(e).__name__}: {e}")
            if attempt < 2:
                await asyncio.sleep(2)

if __name__ == '__main__':
    # Usage: python AR_emulator_nosub.py <id> [services] [--metrics-file PATH]
    # e.g. python AR_emulator_nosub.py 1 pose,gesture,object
    #      python AR_emulator_nosub.py 1 pose,gesture --metrics-file /tmp/ar_metrics_1.json
    _VALID = {"pose", "gesture", "object"}
    parser = argparse.ArgumentParser(description='AR Emulator')
    parser.add_argument('id', help='Emulator instance ID')
    parser.add_argument('services', nargs='?', default='pose,gesture,object',
                        help='Comma-separated services (default: pose,gesture,object)')
    parser.add_argument('--metrics-file', default='',
                        help='Path to write per-second metrics JSON (used by experiment_ctrl)')
    parser.add_argument('--agent-manager-ip', default='10.52.52.111',
                        help='AgentManager IP (default: 10.52.52.111)')
    parser.add_argument('--agent-manager-ws-port', type=int, default=30008,
                        help='AgentManager WebSocket port (default: 30008)')
    args = parser.parse_args()

    _requested = [s.strip() for s in args.services.split(',')]
    _invalid = [s for s in _requested if s not in _VALID]
    if _invalid:
        print(f"Error: unknown service types: {_invalid}")
        print(f"Available services: {sorted(_VALID)}")
        sys.exit(1)

    number = args.id
    services_arg = ','.join(_requested)
    metrics_file = args.metrics_file
    start_time = time.time()

    Agent_Manager_IP = args.agent_manager_ip
    Agent_Manager_Port = 30007
    Agent_Manager_Websocket_port = args.agent_manager_ws_port

    Agent_IP = ""
    Agent_Port = "0"
    Agent_Websocket_Port = "0"

    reconnect = False

    target_width = 1280
    target_height = 720

    if not os.path.exists("1280hand.jpg"):
        print("Error: 1280hand.jpg not found!")
        sys.exit(1)

    with open("1280hand.jpg", "rb") as image:
        f = image.read()
        image_byte = bytearray(f)
        print(f"Loaded image: {len(image_byte)} bytes")

    sending_freq = 40
    SendFPS = 0
    RecvFPS = 0
    service_recv_fps = {s: 0 for s in _requested}
    # initialise per-service latency/effective-fps counters for requested services only
    for _s in _requested:
        _svc_eff_fps[_s]   = 0
        _svc_lat_sum[_s]   = 0.0
        _svc_lat_count[_s] = 0

    log_dir = "logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    logging.basicConfig(filename=os.path.join(log_dir, f"AR_simulator_{number}.log"),
                        format='%(asctime)s %(levelname)s: %(message)s',
                        level=logging.INFO)

    _frame_csv_file = open(os.path.join(log_dir, f"frame_latency_{number}.csv"), 'w', newline='', buffering=1)
    _frame_csv_writer = csv.writer(_frame_csv_file)
    _frame_csv_writer.writerow(["timestamp", "service", "frame_idx", "latency_ms"])

    print(f"Starting AR Emulator {number}")
    print(f"Image size: {len(image_byte)} bytes")
    print(f"Target: {Agent_Manager_IP}:{Agent_Manager_Websocket_port}")
    print(f"Services: {services_arg}")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nProgram interrupted by user")
        if Agent_IP and Agent_Port != "0":
            try:
                print(f"Sending unsubscribe request to {Agent_IP}:{Agent_Port}...")
                requests.delete(f"http://{Agent_IP}:{Agent_Port}/subscribe", timeout=2)
                print("Unsubscribe successful.")
            except Exception as e:
                print(f"Failed to unsubscribe: {e}")
