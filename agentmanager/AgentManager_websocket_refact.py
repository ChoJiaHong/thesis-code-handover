import asyncio
import logging
import os
import threading
import time
from fastapi import FastAPI, Form, Request
import requests
import uvicorn
import paramiko
import json
import sys
import websockets

# python AgentManager_websocket.py number_of_agenthost agenthost1_ip agenthost1_account agenthost1_passward agenthost2_ip genthost2_account agenthost2_passward
# e.g. python AgentManager_websocket.py 2 10.52.52.58 user58 user 10.52.52.59 user59 user

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
with open(CONFIG_PATH, "r") as _f:
    _config = json.load(_f)

AGENT_CONTROLLER_IP = _config["agent_controller_ip"]     # Agent 看得到的外部 NodePort IP
AGENT_CONTROLLER_PORT = _config["agent_controller_port"]
AGENT_READY_TIMEOUT = _config["agent_ready_timeout"]     # wait_for_agent_ready 預設等待秒數
AGENT_IMAGE = _config["agent_image"]


Agent_Host_Number = int(sys.argv[1])

current_agent_host = -1

Agent_Host = []
Agent_Host_ACCOUNT = []
Agent_Host_PASSWORD = []

for i in range(Agent_Host_Number):
    Agent_Host.append(sys.argv[3 * i + 2])
    Agent_Host_ACCOUNT.append(sys.argv[3 * i + 3])
    Agent_Host_PASSWORD.append(sys.argv[3 * i + 4])

port = 8888
# 注意：websocket_port 這個變數啟動時的值，同時被 line ~515 拿去綁定 AgentManager
# 自己對外服務 client 的 websocket listen port（K8s Service 的 NodePort 30008 -> 這個
# targetPort），不能亂改，否則外部連不進來。
websocket_port = 50051

# 這個是「配發給新建 agent container」用的獨立計數器，起始值刻意設在 61000，
# 避開 Linux 預設的臨時 port 範圍（/proc/sys/net/ipv4/ip_local_port_range 通常是
# 32768-60999）。之前 agent 的 port 池也共用 websocket_port 那個變數（起始 50051），
# 整段可用範圍都落在臨時 port 區間內，導致偶爾跟其他行程（例如 Prometheus 對外連線）
# 用到的臨時來源 port 撞號，docker run -p 綁定失敗（"address already in use"），
# 而且這個錯誤變體沒被下面的 retry 邏輯攔到，會讓整個 websocket handler 未捕捉例外
# 崩潰（client 端看到 1011 internal error）。跟 AgentManager 自己的 listen port 完全
# 分開，才不會互相干擾。
agent_ws_port_pool = 61000

Service = ["pose", "gesture", "object"]

incluster = True

if incluster:
    ControllerIP = "controller-service"
    ControllerPort = 80
else:
    ControllerIP = "10.52.52.12"
    ControllerPort = 30004

log_dir = "logs"
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

logging.basicConfig(filename=os.path.join(log_dir, "AgentManager.log"),
                    format='%(asctime)s %(levelname)s: %(message)s',
                    level=logging.INFO)

app = FastAPI()

# 保護全域狀態（port/host 分配 + JSON 檔案讀寫）的鎖
_subscribe_lock = asyncio.Lock()
# 限制同時啟動的 agent 容器數量，避免 Docker host 資源耗盡
_create_semaphore = asyncio.Semaphore(3)

@app.middleware("http")
async def log_requests(request: Request, call_next):
    log_data = {
        "client_host": request.client.host,
        "client_port": request.client.port,
        "method": request.method,
        "url": str(request.url),
    }

    logging.info(f"HTTP Request: {log_data}")

    response = await call_next(request)
    return response

@app.post("/subscribe")
async def agent(request: Request):
    client_host = request.client.host
    client_port = request.client.port

    # 用鎖序列化 port 分配，避免多個請求拿到相同 port
    async with _subscribe_lock:
        host, newport, newwebsocketport = generate_agent_information()

    # 將阻塞的 SSH + docker run 移到 executor，不阻塞 event loop
    loop = asyncio.get_event_loop()
    ip, port, websocket_port, container_id = await loop.run_in_executor(
        None, lambda: create_agent(Host=host, services_config=None, newport=newport, newwebsocketport=newwebsocketport)
    )

    # 用鎖序列化 JSON 讀寫，避免檔案損毀
    async with _subscribe_lock:
        store_information(client_host, ip, port, websocket_port, container_id)

    print(ip)
    print(port)
    print(websocket_port)

    #return the ip, port of the agent that just created
    return {"IP": ip, "Port": port, "WebsocketPort": websocket_port}

@app.post("/agentfail")
async def agentfail(request: Request):
    #todo
    #find the corresponding agent
    failed_agent, failed_agentport, failed_agentwebsocketport = find_pair_information(request.client.host)
    if failed_agent == None or failed_agentport == None or failed_agentwebsocketport == None:
        logging.error(f"Agent not found for client: {request.client.host}")
        return {"status": "500", "message": "agent not found"}


    #get a new agent information, need to tell controller who's the successor
    new_host, new_agent_port, new_agent_websocketport = generate_agent_information()
    #call controller to get agent information
    body = {
        "old_ip": failed_agent,
        "old_port": failed_agentport,
        "new_ip": Agent_Host[new_host],
        "new_port": new_agent_port
    }
    response = requests.post(f'http://{ControllerIP}:{ControllerPort}/agentfail', json.dumps(body))
    if response.status_code != 200:
        logging.error("Failed to get result from controller")
        return{"status": "500", "message": "fail getting result from controller"}
    response = response.json()
    logging.info(f"got old information from controller: {response}")

    try:
        pose_ip = "0"
        pose_port = 0
        pose_freq = 0
        ges_ip = "0"
        ges_port = 0
        ges_freq = 0

        for service in response:
            servicetype = service['ServiceType']
            if servicetype == 'pose':
                pose_ip = service['IP']
                pose_port = service['Port']
                pose_freq = service['Frequency']
            elif servicetype == 'gesture':
                ges_ip = service['IP']
                ges_port = service['Port']
                ges_freq = service['Frequency']
        # pose_ip = response[0].get("IP")
        # pose_port = response[0].get("Port")
        # pose_freq = response[0].get("Frequency")
        # ges_ip = response[1].get("IP")
        # ges_port = response[1].get("Port")
        # ges_freq = response[1].get("Frequency")
    except Exception:
        logging.error(f"Error parsing response: {response}")
        print(response)

    services_config = {
        "pose": {"ip": pose_ip, "port": pose_port, "freq": pose_freq},
        "gesture": {"ip": ges_ip, "port": ges_port, "freq": ges_freq}
    }

    #call a function to create a agent on agent host
    ip, port, websocket_port, container_id = create_agent(Host=new_host, services_config=services_config, newport=new_agent_port, newwebsocketport=new_agent_websocketport)

    #store the bind information of the new agent and client
    store_information(request.client.host, ip, port, websocket_port, container_id)

    return {"status": "200", "message": "OK"}

@app.get("/newagent")
async def newagent(request: Request):
    #find the pair relationship of client and agent
    ip , port, websocketport = find_pair_information(request.client.host)
    if ip == None or port == None or websocketport == None:
        ip = ""
        port = 0
        websocketport = 0

    logging.info(f"New agent info: IP={ip}, Port={port}, WebsocketPort={websocketport}")
    #return the corresponding agent ip and port
    body = {
        "IP": ip,
        "Port": port,
        "WebsocketPort": websocketport
    }
    return {"IP": ip, "Port": port, "WebsocketPort": websocketport}

def run_server():
    #the IP and Port to run Agent Manager
    #needs to modify
    logging.info("HTTP server started on 0.0.0.0:" + str(port))
    uvicorn.run(app, host="0.0.0.0", port= port)

'''
services_config: a dict containing configurations for multiple services
newport : a agent port that is already created, no need to create again
newwebsocketport : a agent websocket port that is already created, no need to create again
'''
def create_agent(Host: int, services_config: dict = None, newport=0, newwebsocketport=0, _retry=0):
    #optional args old informations

    if services_config is None:
        services_config = {}

    #get a unique agent information
    if newport == 0 or newwebsocketport == 0:
        Host, newport, newwebsocketport = generate_agent_information()

    #connect to agent host and create agent by command
    ssh = paramiko.SSHClient()

    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    ssh.connect(hostname=Agent_Host[Host], username=str(Agent_Host_ACCOUNT[Host]), password=str(Agent_Host_PASSWORD[Host]))

    logging.info(f"connected to Agent Host {Agent_Host[Host]}")

    print("before command")
    command = f"docker run -d --rm -v /home/logs:/app/logs -p {newport}:{newport} -p {newwebsocketport}:{newwebsocketport} {AGENT_IMAGE} \
        --ip {Agent_Host[Host]} --port {newport} --ws-port {newwebsocketport} \
        --controller-ip {AGENT_CONTROLLER_IP} --controller-port {AGENT_CONTROLLER_PORT}"
    
    for srv_name, srv_info in services_config.items():
        if srv_info.get("ip") and str(srv_info.get("ip")) != "0":
            command += f" --service {srv_name} {srv_info['ip']} {srv_info['port']} {srv_info['freq']}"
            
    print(f"Executing command: {command}")
    
    try:
        stdin, stdout, stderr = ssh.exec_command(command, timeout=30)
        
        # 等待指令執行完成
        exit_status = stdout.channel.recv_exit_status()
        out = stdout.read().decode().strip()
        err = stderr.read().decode().strip()
        
        print(f"after command, exit_status={exit_status}")
        print(f"stdout: {out}")
        if err:
            print(f"stderr: {err}")
        
        if exit_status != 0:
            # 兩種措辭都要接：Docker 自己記錄的 port 已分配用 "port is already allocated"；
            # OS 層級 bind 失敗（例如撞上其他行程當下用掉的臨時 port）則是
            # "address already in use"，之前只接第一種，第二種會直接 raise 到
            # handle_client() 沒人接，讓整個 websocket handler crash。
            if ("port is already allocated" in err or "address already in use" in err) and _retry < 30:
                ssh.close()
                logging.warning(f"Port {newport}/{newwebsocketport} already allocated/in use, retrying with {newport+1}/{newwebsocketport+1}")
                return create_agent(Host=Host, services_config=services_config,
                                    newport=newport + 1, newwebsocketport=newwebsocketport + 1,
                                    _retry=_retry + 1)
            logging.error(f"Docker run failed (exit {exit_status}): {err}")
            raise RuntimeError(f"Docker run failed: {err}")

        container_id = out  # docker run -d 輸出 container ID
        logging.info(f"Container started successfully, ID: {container_id}")

    except Exception as e:
        print(f"SSH exec_command error: {type(e).__name__}: {str(e)}")
        logging.error(f"SSH exec_command error: {type(e).__name__}: {str(e)}")
        ssh.close()
        raise

    time.sleep(2)  # 等待容器啟動（在 executor 中執行，不阻塞 event loop）
    ssh.close()

    return Agent_Host[Host], newport, newwebsocketport, container_id

#generate a agent host, a new agent port and websocket port
def generate_agent_information():
    global current_agent_host
    global Agent_Host_Number
    global port
    global agent_ws_port_pool

    current_agent_host = (current_agent_host + 1) % Agent_Host_Number
    port += 1
    agent_ws_port_pool += 1
    return current_agent_host, port - 1, agent_ws_port_pool - 1

def store_information(ar: str, agent: str, agentport: int, agentwebsocketport: int, container_id: str = ""):
    logging.info(f"store info for AR: {ar} and agent: {agent} {agentport} {agentwebsocketport} container={container_id}")
    if os.path.exists('AR_Agent.json'):
        with open('AR_Agent.json', 'r') as json_file:
            data_list = json.load(json_file)
    else:
        data_list = []

    for data in data_list:
        if data["AR"] == ar:
            data_list.remove(data)

    newpair = {
        "AR": ar,
        "Agent": agent,
        "AgentPort": agentport,
        "AgentWebsocketPort": agentwebsocketport,
        "ContainerID": container_id,
    }

    data_list.append(newpair)

    with open('AR_Agent.json', 'w') as json_file:
        json.dump(data_list, json_file)

def find_pair_information(ar: str):
    logging.info(f"find the agent of AR ({ar})")
    if os.path.exists('AR_Agent.json'):
        with open('AR_Agent.json', 'r') as json_file:
            data_list = json.load(json_file)
    else:
        logging.error(f"Agent not found for {ar}")
        return None, None, None

    for data in data_list:
        if data["AR"] == ar:
            logging.info(f"Agent found, ip: {data['Agent']}, port: {data['AgentPort']}, websocketport: {data['AgentWebsocketPort']}")
            return data["Agent"], data["AgentPort"], data["AgentWebsocketPort"]
    logging.error(f"Agent not found for {ar}")
    return None, None, None

def find_full_pair_information(ar: str):
    if os.path.exists('AR_Agent.json'):
        with open('AR_Agent.json', 'r') as json_file:
            data_list = json.load(json_file)
    else:
        return None
    for data in data_list:
        if data["AR"] == ar:
            return data
    return None

def remove_pair_information(ar: str):
    if not os.path.exists('AR_Agent.json'):
        return
    with open('AR_Agent.json', 'r') as json_file:
        data_list = json.load(json_file)
    data_list = [d for d in data_list if d["AR"] != ar]
    with open('AR_Agent.json', 'w') as json_file:
        json.dump(data_list, json_file)

def delete_agent(agent_ip: str, container_id: str):
    if not container_id:
        logging.warning(f"delete_agent: no container_id for {agent_ip}, skipping")
        return
    # 找對應的 host index 以取得 SSH 帳密
    host_idx = next((i for i, h in enumerate(Agent_Host) if h == agent_ip), None)
    if host_idx is None:
        logging.error(f"delete_agent: agent_ip {agent_ip} not in Agent_Host list")
        return
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(hostname=agent_ip,
                    username=Agent_Host_ACCOUNT[host_idx],
                    password=Agent_Host_PASSWORD[host_idx])
        cmd = f"docker stop {container_id}"
        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=15)
        stdout.channel.recv_exit_status()
        ssh.close()
        logging.info(f"delete_agent: stopped container {container_id} on {agent_ip}")
        print(f"Stopped agent container {container_id} on {agent_ip}")
    except Exception as e:
        logging.error(f"delete_agent error: {type(e).__name__}: {e}")

def unsubscribe_from_controller(agent_ip: str, agent_port: int):
    try:
        body = {"ip": agent_ip, "port": agent_port}
        r = requests.post(f"http://{ControllerIP}:{ControllerPort}/unsubscribe", json=body, timeout=10)
        logging.info(f"unsubscribe_from_controller: {agent_ip}:{agent_port} → {r.status_code}")
    except Exception as e:
        logging.error(f"unsubscribe_from_controller error: {e}")

def subscribe_services(host:int, port: int, servicenames: list) -> bool:
    """回傳 controller 的准入判斷結果（True=已接受，False=容量不足被拒絕）。"""
    body = {
        "ip" : str(Agent_Host[host]),
        "port" : port,
        "serviceTypes" : servicenames
    }
    response = requests.post(f'http://{ControllerIP}:{ControllerPort}/subscribe', json=body)
    response = response.json()
    admitted = response.get("admitted", True)
    logging.info(f"subscribed {servicenames} service for new agent: {response} (admitted={admitted})")
    return admitted

async def wait_for_agent_ready(ip, port, timeout=AGENT_READY_TIMEOUT):
    url = f"http://{ip}:{port}/healthz"
    loop = asyncio.get_event_loop()
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            # Run in executor so concurrent waiters don't block each other on the event loop
            status = await loop.run_in_executor(
                None, lambda: requests.get(url, timeout=1).status_code)
            if status == 200:
                logging.info(f"Agent {ip}:{port} is ready.")
                return True
        except Exception:
            pass
        await asyncio.sleep(0.5)
    logging.warning(f"Timeout waiting for Agent {ip}:{port} to be ready.")
    return False

# 客戶端連接後的處理
async def handle_client(websocket, path):
    print(f"Client connected from {path}")
    client_ip, client_port = websocket.remote_address
    # Use ip:port as the unique identifier — multiple emulators from the same
    # machine share the same IP but each TCP connection has a distinct port.
    client_id = f"{client_ip}:{client_port}"
    logging.info(f"WebSocket Client connected from {client_id} with path {path}")
    print(f"client ip = {client_ip}, port = {client_port}")

    # ── 清理舊 Agent（同一 client_id 重連時，先退訂舊容器再建新的）──────────
    # 在鎖內先取出並刪除記錄，避免並發連線重複清理同一個舊 Agent
    async with _subscribe_lock:
        existing = find_full_pair_information(client_id)
        if existing:
            remove_pair_information(client_id)
    if existing:
        old_ip  = existing["Agent"]
        old_port = existing["AgentPort"]
        old_cid  = existing.get("ContainerID", "")
        logging.info(f"Client {client_id} reconnecting; cleaning up stale agent {old_ip}:{old_port}")
        print(f"Cleaning up stale agent for {client_id}: {old_ip}:{old_port}")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: unsubscribe_from_controller(old_ip, old_port))
        await loop.run_in_executor(None, lambda: delete_agent(old_ip, old_cid))

    # parse requested services from path (e.g. /pose,gesture -> ["pose", "gesture"])
    requested_services = path.strip('/').split(',') if path.strip('/') else []
    if not requested_services:
        requested_services = ["pose", "gesture"] # fallback

    async with _subscribe_lock:
        host, new_agent_port, new_agent_websocketport = generate_agent_information()
    print(f"got new agent info with ip {Agent_Host[host]} port {new_agent_port} and websocketport {new_agent_websocketport}")
    logging.info(f"New agent generated for WebSocket client: IP={Agent_Host[host]} Port={new_agent_port}, WebsocketPort={new_agent_websocketport}")

    services_config = {}
    for svc in requested_services:
        services_config[svc] = {"ip": "", "port": 0, "freq": 0.0}

    agent_ip = Agent_Host[host]
    loop = asyncio.get_event_loop()

    # 最多 3 個容器同時啟動；Semaphore 持有到容器確認 ready 才釋放，
    # 避免 Docker host 資源耗盡（CPU/GPU/記憶體競爭）
    async with _create_semaphore:
        # create_agent may retry with a different port if the assigned one is occupied
        agent_ip, new_agent_port, new_agent_websocketport, container_id = await loop.run_in_executor(
            None, lambda: create_agent(Host=host, services_config=services_config,
                                       newport=new_agent_port, newwebsocketport=new_agent_websocketport)
        )
        print(f"created agent container on port {new_agent_port}/{new_agent_websocketport}")

        # Second: wait for agent to be ready (HTTP server started)
        agent_ready = await wait_for_agent_ready(agent_ip, new_agent_port)

    if agent_ready:
        # Third: subscribe services from controller only when agent is ready.
        # 序列化對 controller 的訂閱請求：避免多個 agent 同時送 /subscribe 給
        # controller，使 controller reconcile 的 Phase A snapshot 彼此干擾。
        try:
            async with _subscribe_lock:
                admitted = await loop.run_in_executor(None, lambda: subscribe_services(host, new_agent_port, requested_services))
            print(f"subscribed service to controller, admitted={admitted}")
            if not admitted:
                logging.warning(f"[ADMISSION] Controller rejected subscription for client {client_id} (services={requested_services})")
        except Exception as e:
            # subscribe_services() 打 controller 失敗（逾時/連線錯誤/非預期回應）：
            # 容器已經建立且 ready，但 handoff 訊息還沒送出去，client 端永遠拿不到
            # agent_ip，成為兩邊都不會清理的孤兒容器。比照下面「agent 未 ready」
            # 分支，直接在這裡清掉。
            logging.error(f"[ADMISSION] subscribe_services failed for {agent_ip}:{new_agent_port}, "
                          f"cleaning up: {type(e).__name__}: {e}")
            await loop.run_in_executor(None, lambda: delete_agent(agent_ip, container_id))
            await websocket.close()
            return
    else:
        logging.error(f"Agent {agent_ip}:{new_agent_port} failed to start within timeout, aborting.")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: delete_agent(agent_ip, container_id))
        await websocket.close()
        return

    # 記錄此次分配；正常 handoff 後保留，供下次重連時偵測舊 Agent
    async with _subscribe_lock:
        store_information(client_id, agent_ip, new_agent_port, new_agent_websocketport, container_id)

    handoff_sent = False
    try:
        # send 在 try 內：確保無論 emulator 是否已斷線，finally 都能清理
        # 第 4 個欄位回傳 controller 准入結果（1=接受, 0=拒絕），讓 client 端也能看到
        await websocket.send(f"{agent_ip} {new_agent_port} {new_agent_websocketport} {int(admitted)}")
        handoff_sent = True
        # 等待 emulator 關閉連線（正常交接後 emulator 會立刻 close）
        async for message in websocket:
            pass
    except (websockets.ConnectionClosed, Exception):
        pass
    finally:
        if handoff_sent:
            # 正常交接：保留 AR_Agent.json 記錄，供下次重連偵測舊 Agent
            logging.info(f"Handoff complete for client {client_id}, agent {agent_ip}:{new_agent_port} keeps running")
            print(f"Handoff complete for client {client_id}")
        else:
            # 交接前斷線（建立過程失敗）：清理資源並移除記錄
            async with _subscribe_lock:
                remove_pair_information(client_id)
            logging.info(f"Early disconnect for client {client_id}, cleaning up")
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: unsubscribe_from_controller(agent_ip, new_agent_port))
            await loop.run_in_executor(None, lambda: delete_agent(agent_ip, container_id))
            print(f"Cleanup done for client {client_id}")

# 啟動 WebSocket 伺服器
async def start_server():
    server = await websockets.serve(handle_client, "0.0.0.0", websocket_port, ping_interval=None)
    print("WebSocket server started on ws://0.0.0.0:" + str(websocket_port))
    logging.info("WebSocket server started on ws://0.0.0.0:" + str(websocket_port))
    await server.wait_closed()

if __name__ == "__main__":
    app.debug = False
    #run_server()
    threading.Thread(target = run_server).start()

    asyncio.get_event_loop().run_until_complete(start_server())
    asyncio.get_event_loop().run_forever()