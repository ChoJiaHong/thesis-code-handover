import asyncio
import json
import logging
import os
import copy
import time
from datetime import datetime
import requests
import yaml
import concurrent.futures
from typing import List, Dict, Any

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from pydantic import BaseModel
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from starlette.responses import JSONResponse

GPU_MEMORY_LABEL = "nvidia.com/gpu.memory"
IN_CLUSTER = True
CONTROLLER_CONFIG_FILE = './information/controller_config.json'
_VALID_MODES = ('ga', 'ems', 'dl3', 'ems_dl3', 'ems_rr', 'lsr', 'minlp', 'ga_ff', 'ga_bf', 'ga_bf_prune',
                 'ems_v2', 'lsr_v2', 'hudson', 'hudson_combo', 'lai_eua')
print("28")
_ctrl_cfg: dict = {}

def _load_ctrl_config() -> dict:
    try:
        with open(CONTROLLER_CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_ctrl_config(cfg: dict) -> None:
    with open(CONTROLLER_CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=4)

def cfg_solver_mode() -> str:
    return _ctrl_cfg.get('solver_mode') or 'ga'

def cfg_keep_pods_on_empty() -> bool:
    return bool(_ctrl_cfg.get('keep_pods_on_empty', False))

def cfg_ga_pop_size() -> int:
    return int(_ctrl_cfg.get('ga_pop_size', 20))

def cfg_ga_generations() -> int:
    return int(_ctrl_cfg.get('ga_generations', 10))

def cfg_ga_n_restarts() -> int:
    """GA每次事件觸發時的獨立重跑次數：每次重跑各自初始化族群、獨立演化
    cfg_ga_generations() 代，取全部重跑中 (nsat, Q) 字典式排序最佳者為最終解。
    預設1，即維持現況行為（單次求解，不重跑）。"""
    return int(_ctrl_cfg.get('ga_n_restarts', 1))

def cfg_ga_time_limit() -> float:
    """GA本次事件觸發之整體時間預算（秒），涵蓋所有重跑加總。0表示不設限
    （維持現況行為，僅以世代數為終止條件）。時間到時：(a) 不再開始下一次重跑；
    (b) 當前重跑之世代迴圈提前中止，回傳目前已演化出的最佳解。"""
    return float(_ctrl_cfg.get('ga_time_limit', 0))

def cfg_ga_routing() -> str:
    """ga_bf routing allocator: 'optimize' (f_h-first, default) or 'nsats' (f_l-first)."""
    return _ctrl_cfg.get('ga_routing', 'optimize')

def cfg_phase1_enabled() -> bool:
    """Enable two-phase migration: notify agents with existing pods while new pods are building."""
    return bool(_ctrl_cfg.get('phase1_enabled', True))

def cfg_deploy_switch_policy() -> str:
    """
    subscribe/node_recovery 事件下，部署（gene）是否切換的判準：
      'nsat_only'  (預設) - 只有 nsat 嚴格提升才切換，維持現況行為
      'q_superset' - nsat 打平但 Q 提升，且新部署未刪除任何現有節點的既有
                     服務時，也切換
      'q_any'      - nsat 打平但 Q 提升即切換，不檢查是否刪除現有服務
    """
    return _ctrl_cfg.get('deploy_switch_policy', 'nsat_only')

def cfg_q_switch_min_gain() -> float:
    """q_any / q_superset 政策下，nsat 打平時 Q 至少要改善多少才允許切換部署
    （Q 是 0~1 的正規化平均，見 bench_ga_v2_CUR.py score_from_x /
    allocate_x_nsats_plus / allocate_x_ff_bf_opt 的 2026-07-18 修正）。
    預設 0.01（1%），避免為了浮點雜訊等級的假進步付出真實的部署切換成本
    （新建 pod、等 wait_pod_ready，實測平均一次要 10+ 秒）。"""
    return float(_ctrl_cfg.get('q_switch_min_gain', 0.01))

def cfg_notify_wave_gap_s() -> float:
    """Phase 4-B（C4）down/up 兩波通知之間的等待秒數。down 波的 HTTP response
    只代表 agent 端 self.freq 已被設定，不代表 agent 的 _run_loop 已經套用新頻率
    送出下一幀（那邊最壞要等到 1/舊頻率 秒後才會重算 timeout），這裡加一個
    可調的緩衝，讓 down 波實際生效後再送 up 波，預設 0 等同於原行為（不等待）。"""
    return float(_ctrl_cfg.get('notify_wave_gap_s', 0.0))

def cfg_pre_delete_gap_s() -> float:
    """C4（Phase 2 最終通知）完成到 C5（實際刪除舊 pod）之間的等待秒數。
    C4 的 HTTP response 只代表 agent 收到新設定，agent 的 _run_loop 最壞要等到
    1/舊頻率 秒後才真的停止/改送對象；且 pod 端目前沒有攔截 SIGTERM，一收到
    K8s 刪除信號就立即終止、不 drain in-flight 請求。這裡加一個可調的緩衝，
    讓已發出的降頻/停止通知有機會真正生效，再真的刪 pod，預設 0 等同於原行為
    （不等待）。"""
    return float(_ctrl_cfg.get('pre_delete_gap_s', 0.0))

def cfg_minlp_time_limit() -> float:
    return float(_ctrl_cfg.get('minlp_time_limit', 30))

def cfg_minlp_rel_gap() -> float:
    return float(_ctrl_cfg.get('minlp_rel_gap', 0.1))

def cfg_minlp_max_nodes() -> int:
    return int(_ctrl_cfg.get('minlp_max_nodes', 10000))


_ctrl_cfg.update(_load_ctrl_config())
if _ctrl_cfg.get('solver_mode') and _ctrl_cfg['solver_mode'] not in _VALID_MODES:
    raise ValueError(
        f"controller_config.json 的 solver_mode={_ctrl_cfg['solver_mode']!r} 不是合法值，"
        f"必須是 {_VALID_MODES} 其中之一（或留空以使用預設值 'ga'）——"
        f"若不修正，dispatch 會靜默 fallback 到 'ga'，不會有任何錯誤訊息。"
    )
SERVICE_FILE = './information/service.json'
SERVICESPEC_FILE = './information/serviceSpec_mul.json'
SUBSCRIPTION_FILE = './information/subscription.json'
NODE_STATUS_FILE = './information/nodestatus.json'
RECONCILE_HISTORY_FILE = './information/reconcile_history.json'
NODESTATUS_EVENTS_FILE = './information/nodestatus_events.json'
LOG_FILE = './logdir/controller.log'

# 每次啟動時以 timestamp 命名，記錄各事件後的部署與連線快照
_EVENT_LOG_FILE: str = ""


def _init_event_log():
    global _EVENT_LOG_FILE
    os.makedirs('./logdir', exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    _EVENT_LOG_FILE = f"./logdir/events_{ts}.log"
    with open(_EVENT_LOG_FILE, 'w') as f:
        f.write(f"# Controller Event Log — {ts}\n")
        f.write(f"# mode: {cfg_solver_mode()}\n\n")
    logging.info(f"Event log initialized: {_EVENT_LOG_FILE}")


def _write_event_snapshot(trigger_info, svcs, subs, target_allocation, nsats, duration_s,
                          ga_debug: dict = None):
    """每次 reconcile 結束後，把部署狀況與連線流量寫入事件 log。"""
    if not _EVENT_LOG_FILE:
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    trig = trigger_info or {}
    event_type = trig.get("type", "unknown")
    agent_info = trig.get("agent", "")
    services   = trig.get("services", trig.get("removed_services", []))

    W = 72
    SEP  = "=" * W
    SEP2 = "-" * W

    lines = []
    lines.append(SEP)
    lines.append(f"[{now}] EVENT: {event_type:<12}  agent: {agent_info}")
    lines.append(f"  services: {services}   mode: {cfg_solver_mode()}   "
                 f"duration: {duration_s:.1f}s   nsats: {nsats}/{len(subs)}")
    lines.append(SEP2)

    # ── GA 剪枝資訊（僅 GA 模式）────────────────────────────────────────
    if ga_debug and ga_debug.get("pruned"):
        lines.append(f"  GA PRUNE  (剪枝發生！)")
        lines.append(f"    剪枝前: {ga_debug.get('pre_prune', {})}")
        lines.append(f"    剪枝後: {ga_debug.get('post_prune', {})}")
        lines.append(f"    有連線: {ga_debug.get('used_node_svcs', [])}")
        lines.append(SEP2)
    elif ga_debug and cfg_solver_mode() == 'ga':
        lines.append(f"  GA PRUNE  無變化  post={ga_debug.get('post_prune', {})}")
        lines.append(SEP2)

    # ── 部署狀況 ────────────────────────────────────────────────────────
    by_node: dict = {}
    for s in svcs:
        n = s.get('nodeName', '?')
        by_node.setdefault(n, []).append(s)

    lines.append(f"  DEPLOYMENT  ({len(svcs)} pods)")
    if not svcs:
        lines.append("    (none)")
    for node, pod_list in sorted(by_node.items()):
        pods_str = "  ".join(
            f"{p['serviceType']}:{p['hostPort']}({p.get('podIP','?')})"
            for p in sorted(pod_list, key=lambda x: x['serviceType'])
        )
        conns = [c for p in pod_list for c in p.get('currentConnection', [])]
        lines.append(f"    {node:<12} {pods_str}  [{len(conns)} conn]")

    lines.append(SEP2)

    # ── 連線流量 ────────────────────────────────────────────────────────
    # 整理 allocation: agent_id → {svc → (node, port, freq)}
    alloc_by_agent: dict = {}
    for a in target_allocation:
        aid = f"{a['agentIP']}:{a['agentPort']}"
        alloc_by_agent.setdefault(aid, []).append(a)

    lines.append(f"  CONNECTIONS  ({len(subs)} agents,  {len(target_allocation)} alloc entries)")
    for sub in sorted(subs, key=lambda x: (x['agentIP'], x['agentPort'])):
        aid = f"{sub['agentIP']}:{sub['agentPort']}"
        svcs_sub = [s['serviceType'] for s in sub.get('subscriptions', [])]
        lines.append(f"    {aid}  sub={svcs_sub}")
        for a in sorted(alloc_by_agent.get(aid, []), key=lambda x: x['serviceType']):
            lines.append(f"        {a['serviceType']:<8} → {a['targetNode']}:{a['hostPort']}  "
                         f"@ {a['frequency']:.0f} fps")
        if aid not in alloc_by_agent:
            lines.append("        (no allocation)")

    lines.append(SEP)
    lines.append("")

    try:
        with open(_EVENT_LOG_FILE, 'a') as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        logging.error(f"_write_event_snapshot failed: {e}")

logging.basicConfig(
    filename=LOG_FILE,
    format='%(asctime)s %(levelname)s: %(message)s',
    level=logging.INFO
)

state_lock = asyncio.Lock()
# 持有 /subscribe 觸發的 reconcile task 參考，避免被 GC 提早回收
_bg_tasks_keepalive: set = set()

class SubscriptionRequest(BaseModel):
    ip: str
    port: int
    serviceTypes: List[str]

# ==========================================
# 狀態讀取/寫入封裝
# ==========================================

def load_json(filepath, default=[]):
    if not os.path.exists(filepath):
        return default
    try:
        with open(filepath, 'r') as f:
            return json.load(f)
    except:
        return default

def save_json(filepath, data):
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=4)

def append_json_record(filepath, record):
    data = load_json(filepath, [])
    if not isinstance(data, list):
        data = []
    data.append(record)
    save_json(filepath, data)

def lifespan(app: FastAPI):
    config.load_incluster_config() if IN_CLUSTER else config.load_kube_config()
    core_api = client.CoreV1Api()
    node_status_list = []
    try:
        nodes = core_api.list_node().items
        for node in nodes:
            labels = node.metadata.labels
            if labels.get('arha-node-type') == 'computing-node':
                node_status_list.append(node.metadata.name)
    except Exception as e:
        logging.error(f"Error list nodes: {e}")

    node_health_status = {}
    for node in node_status_list:
        ip = get_node_ip(node)
        if ip != "Error":
            try:
                res = curl_health_check(ip)
                if isinstance(res, str) and res.strip() == "ok":
                    node_health_status[node] = "healthy"
                else:
                    node_health_status[node] = "unhealthy"
            except Exception:
                node_health_status[node] = "unhealthy"
        else:
            node_health_status[node] = "unhealthy"

    os.makedirs('./information', exist_ok=True)
    with open(NODE_STATUS_FILE, 'w') as f:
        json.dump(node_health_status, f, indent=4)

    _init_event_log()

    yield

app = FastAPI(lifespan=lifespan)

@app.middleware("http")
async def log_requests(request: Request, call_next):
    api_name = request.url.path
    client_ip = request.client.host
    
    # 讀取 Request Body (注意：這可能會影響大型檔案上傳，建議小資料才讀)
    request_body = await request.body()
    request_log = request_body.decode("utf-8") if request_body else "Empty Body"
    logging.info(f"{api_name} | From: {client_ip} | Req: {request_log}")

    # 執行後續路徑
    response = await call_next(request)

    # 修正重點：只針對 JSON 類型的 Response 進行解析與紀錄，其餘直接回傳
    content_type = response.headers.get("Content-Type", "")
    
    if "application/json" in content_type:
        # 如果是 JSON，我們可以嘗試讀取並 Log
        # 注意：為了不破壞原始 Response，簡單的做法是直接回傳 response
        # 若真的要看內容，建議只在開發環境做，或是使用更複雜的 StreamingResponse 封裝
        logging.info(f"{api_name} | Status: {response.status_code} | Type: JSON")
    else:
        logging.info(f"{api_name} | Status: {response.status_code} | Type: {content_type}")

    return response

# ==========================================
# 底層 K8s 與通訊操作 (已保留並微調)
# ==========================================

def get_node_ip(node_name: str) -> str:
    try:
        config.load_incluster_config() if IN_CLUSTER else config.load_kube_config()
        core_api = client.CoreV1Api()
        node = core_api.read_node(name=node_name)
        for address in node.status.addresses:
            if address.type == "InternalIP":
                return address.address
        return "Error"
    except Exception as e:
        logging.error(f"Error getting node ip: {e}")
        return "Error"

def curl_health_check(ip: str):
    url = f"http://{ip}:10248/healthz"
    try:
        response = requests.get(url, timeout=1)
        if response.status_code == 200:
            return response.text
        return f"failed"
    except Exception as e:
        return f"failed: {e}"

def is_pod_terminating(core_api, pod_name, namespace="default"):
    try:
        resp = core_api.read_namespaced_pod(name=pod_name, namespace=namespace)
        if resp.metadata.deletion_timestamp:
            return True
    except ApiException as e:
        pass
    return False

def deploy_pod_sync(service_type, hostPort, node_name):
    # 僅負責發送建立請求，不阻塞等待 Ready
    try:
        config.load_incluster_config() if IN_CLUSTER else config.load_kube_config()
        core_api = client.CoreV1Api()
        with open(f"service_yaml/{service_type}.yaml") as f:
            dep = yaml.safe_load(f)

        # 修正：Pod 名稱不允許包含 '.'，將其替換為 '-'
        safe_node_name = str(node_name).replace('.', '-')
        unique_name = f"{service_type}-{safe_node_name}-{hostPort}"
        dep['metadata']['name'] = unique_name

        # 確保刪除 YAML 中可能存在的實體 nodeName 欄位，改用 nodeSelector
        if 'nodeName' in dep['spec']:
            del dep['spec']['nodeName']

        dep['spec']['containers'][0]['ports'][0]['hostPort'] = hostPort
        dep['spec']['nodeSelector'] = {'kubernetes.io/hostname': node_name}

        if is_pod_terminating(core_api, unique_name):
            logging.warning(f"Pod {unique_name} is terminating, needs new port.")
            return None

        resp = core_api.create_namespaced_pod(body=dep, namespace='default')
        logging.info(f"Created Pod {resp.metadata.name}.")
        return resp.metadata.name # 回傳 PodName 以便後續非同步等待
    except ApiException as e:
        logging.error(f"K8s API Error for {service_type} on {node_name}: {e.body}")
        return None
    except Exception as e:
        logging.error(f"deploy_pod_sync error: {e}", exc_info=True)
        return None

async def wait_pod_ready(pod_name: str, timeout: int = 60) -> str:
    # 非同步等待 Pod IP 分配與 Ready 狀態
    config.load_incluster_config() if IN_CLUSTER else config.load_kube_config()
    core_api = client.CoreV1Api()
    
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            resp = core_api.read_namespaced_pod(name=pod_name, namespace='default')
            is_ready = False
            if resp.status.conditions:
                for cond in resp.status.conditions:
                    if cond.type == 'Ready' and cond.status == 'True':
                        is_ready = True
            
            if is_ready and resp.status.pod_ip:
                logging.info(f"Pod {pod_name} is Ready with IP {resp.status.pod_ip}")
                return resp.status.pod_ip
        except ApiException:
            pass
        await asyncio.sleep(2)
    
    logging.warning(f"Pod {pod_name} timeout after {timeout}s")
    return ""

def delete_pod(pod_name, namespace='default'):
    try:
        config.load_incluster_config() if IN_CLUSTER else config.load_kube_config()
        core_api = client.CoreV1Api()
        core_api.delete_namespaced_pod(name=pod_name, namespace=namespace)
        logging.info(f"Deleted Pod {pod_name}")
    except Exception as e:
        logging.error(f"Error deleting pod {pod_name}: {e}")

def communicate_with_agent(data: dict, agent_ip: str, agent_port: int):
    url = f"http://{agent_ip}:{agent_port}/servicechange"
    try:
        response = requests.post(url, json=data, timeout=2)
        logging.info(f"Notify Agent {agent_ip}:{agent_port} success, body={data}")
        return response.status_code
    except Exception as e:
        logging.error(f"Notify Agent {agent_ip}:{agent_port} failed: {e}")
        return None


async def notify_agents_two_wave(jobs: list, cur_freq: dict, label: str) -> list:
    """對 jobs（[(agent_ip, agent_port, body), ...]，body 含 'frequency'/'servicename'）
    依 cur_freq 記錄的舊頻率拆成 down（降頻或持平）/ up（升頻，含從 0 開始的新加入）
    兩波：先送 down 波，等 cfg_notify_wave_gap_s() 秒讓 agent 端真的把頻率降下來
    （HTTP response 只代表 agent 收到新設定，不代表 _run_loop 已經套用），再送 up
    波，避免升頻的 agent 先開始送高頻流量時，該降頻的 agent 還在用舊頻率打，造成
    pod 瞬間超載。Phase 1（C1，暫時分配）與 Phase 2（C4，最終分配）共用同一套邏輯。
    回傳 [((ip, port, body), result), ...] 供呼叫端自行統計成功/失敗。"""
    if not jobs:
        return []
    loop = asyncio.get_event_loop()
    down_jobs = [(ip, port, body) for ip, port, body in jobs
                 if body['frequency'] <= cur_freq.get((f"{ip}:{port}", body['servicename']), 0)]
    up_jobs   = [(ip, port, body) for ip, port, body in jobs
                 if body['frequency'] > cur_freq.get((f"{ip}:{port}", body['servicename']), 0)]
    logging.info(f"{label}: down={len(down_jobs)} up={len(up_jobs)}")

    wave_gap_s = cfg_notify_wave_gap_s()
    all_results = []
    for wave_name, wave in [("down", down_jobs), ("up", up_jobs)]:
        if not wave:
            continue
        if wave_name == "up" and down_jobs and wave_gap_s > 0:
            logging.info(f"{label}: waiting {wave_gap_s}s for down wave to take effect before up wave")
            await asyncio.sleep(wave_gap_s)
        wave_results = await asyncio.gather(
            *[loop.run_in_executor(None, communicate_with_agent, body, ip, port)
              for ip, port, body in wave],
            return_exceptions=True
        )
        all_results.extend(zip(wave, wave_results))
        failed_wave = [f"{ip}:{port}" for (ip, port, _), r in zip(wave, wave_results)
                       if r is None or isinstance(r, Exception)]
        if failed_wave:
            logging.warning(f"{label} {wave_name}: failed={failed_wave}")
    return all_results


# ==========================================
# 狀態讀取/寫入封裝
# ==========================================

def load_json(filepath, default=[]):
    if not os.path.exists(filepath):
        return default
    try:
        with open(filepath, 'r') as f:
            return json.load(f)
    except:
        return default

def save_json(filepath, data):
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=4)


# ==========================================
# 求解器 (Solver Adapter) - 抽象封裝
# ==========================================

def get_used_node_ports() -> set:
    """查詢所有 K8s Service 已佔用的 NodePort，避免 port 分配衝突"""
    try:
        config.load_incluster_config() if IN_CLUSTER else config.load_kube_config()
        v1 = client.CoreV1Api()
        used = set()
        for svc in v1.list_service_for_all_namespaces().items:
            if svc.spec.ports:
                for p in svc.spec.ports:
                    if p.node_port:
                        used.add(p.node_port)
        logging.info(f"K8s occupied NodePorts: {sorted(used)}")
        return used
    except Exception as e:
        logging.error(f"Failed to query NodePorts: {e}")
        return set()


def _build_model_dict(subscriptions: List[dict], nodes_status: dict, specs: dict) -> dict:
    """
    將 subscriptions / nodes_status / specs 轉換成 GAEnv 所需的 model_dict。
    GA 與 EMS 路徑共用，確保兩者在相同的問題定義下比較。
    """
    agents = []
    requirements = {}
    for agent_data in subscriptions:
        agent_id = f"{agent_data['agentIP']}:{agent_data['agentPort']}"
        agents.append(agent_id)
        requirements[agent_id] = [s['serviceType'] for s in agent_data.get('subscriptions', [])]

    nodes = [n for n, status in nodes_status.items() if status == 'healthy']
    services = list(specs.get('services', {}).keys())
    f_l = {s: specs['services'][s]['frequencyLimit'][1] for s in services}
    f_h = {s: specs['services'][s]['frequencyLimit'][0] for s in services}

    legal_masks = {node: {} for node in nodes}
    for node in nodes:
        node_ability = specs.get('workAbility', {}).get(node, {})
        for combo_str, cap_dict in node_ability.items():
            mask = 0
            for s in combo_str.split(','):
                if s in services:
                    mask |= (1 << services.index(s))
            legal_masks[node][str(mask)] = cap_dict

    return {
        "services": services, "nodes": nodes, "agents": agents,
        "requirements": requirements, "legal_masks": legal_masks,
        "f_l": f_l, "f_h": f_h,
    }


def _topology_to_gene(env, topology: dict) -> list:
    """
    將 USC-TS 回傳的部署拓撲 {node: [service_list]} 轉換成 GA chromosome。
    若某節點的組合不在 legal_masks 內（理論上不應發生），回退為空部署（mask=0）。
    """
    gene = []
    for node in env.nodes:
        services_on_node = topology.get(node, [])
        mask = 0
        for s in services_on_node:
            if s in env.svc2idx:
                mask |= (1 << env.svc2idx[s])
        if mask != 0 and not env.is_legal_mask(node, mask):
            logging.warning(f"USC-TS produced illegal mask {mask} for node {node}, falling back to 0")
            mask = 0
        gene.append(mask)
    return gene


def _delta_allocation(subs: List[dict], svcs: List[dict], specs: dict) -> List[dict]:
    """
    Unsubscribe 且拓撲不變時的增量分配。
    維持所有 agent 的目標 pod，只重算 fps = min(f_h, 節點容量 / 該節點剩餘人數)。
    """
    f_h_map     = {s: v['frequencyLimit'][0] for s, v in specs.get('services', {}).items()}
    work_ability = specs.get('workAbility', {})

    # 每個節點目前部署的服務列表（用於查 workAbility key）
    node_svcs: Dict[str, List[str]] = {}
    for entry in svcs:
        node_svcs.setdefault(entry['nodeName'], []).append(entry['serviceType'])

    def _cap(node: str, svc: str) -> float:
        deployed = sorted(node_svcs.get(node, []))
        for key, cap_dict in work_ability.get(node, {}).items():
            if sorted(key.split(',')) == deployed:
                return float(cap_dict.get(svc, 0.0))
        return 0.0

    # 統計每個 (node, svc) 的剩餘 agent 數
    count: Dict[Tuple[str, str], int] = {}
    for agent_data in subs:
        for sub in agent_data.get('subscriptions', []):
            node = sub.get('targetNode', '')
            if node:
                count[(node, sub['serviceType'])] = count.get((node, sub['serviceType']), 0) + 1

    alloc = []
    for agent_data in subs:
        for sub in agent_data.get('subscriptions', []):
            node = sub.get('targetNode', '')
            svc  = sub['serviceType']
            port = sub.get('hostPort', 0)
            if not node or not port:
                continue
            k   = max(count.get((node, svc), 1), 1)
            cap = _cap(node, svc)
            fps = int(min(f_h_map.get(svc, 30), cap // k)) if cap > 0 else f_h_map.get(svc, 30)
            alloc.append({
                'agentIP':    agent_data['agentIP'],
                'agentPort':  agent_data['agentPort'],
                'serviceType': svc,
                'targetNode': node,
                'hostPort':   port,
                'frequency':  fps,
            })
    return alloc


def _pack_result(env, optimal_x: dict, svcs: List[dict], gene: list = None) -> dict:
    """
    將分配結果轉換成 controller 標準格式，並分配 port。
    GA 與 EMS 路徑共用。
    gene 不為 None 時（ga_bf 模式）：topology 從 gene 建出（預部署所有 gene 包含的服務），
    routing 仍從 optimal_x 取。
    """
    used_ports = set()
    existing_mapping = {}
    for s in svcs:
        used_ports.add(int(s['hostPort']))
        existing_mapping[(s['nodeName'], s['serviceType'])] = int(s['hostPort'])
    used_ports |= get_used_node_ports()

    current_port_search = 31000

    def get_or_assign_port(nodeName, serviceType):
        nonlocal current_port_search
        key = (nodeName, serviceType)
        if key in existing_mapping:
            return existing_mapping[key]
        while current_port_search in used_ports:
            current_port_search += 1
        p = current_port_search
        used_ports.add(p)
        existing_mapping[key] = p
        current_port_search += 1
        return p

    target_topology = []
    allocation = []
    assigned_topology = set()

    if gene is not None:
        # ga_bf：topology 從 gene 建出，部署所有 gene 包含的服務
        for ni, node in enumerate(env.nodes):
            mask = gene[ni]
            for si, svc in enumerate(env.services):
                if (mask & (1 << si)) and (node, svc) not in assigned_topology:
                    hostPort = get_or_assign_port(node, svc)
                    target_topology.append({"nodeName": node, "serviceType": svc, "hostPort": hostPort})
                    assigned_topology.add((node, svc))
        for (agent_id, node_name, service_type), freq in optimal_x.items():
            if freq == 0:
                continue
            ip, port = agent_id.split(':')
            hostPort = get_or_assign_port(node_name, service_type)
            allocation.append({
                "agentIP": ip, "agentPort": int(port),
                "targetNode": node_name, "hostPort": hostPort,
                "frequency": freq, "serviceType": service_type,
            })
    else:
        # 預設：topology 與 routing 都從 optimal_x 建出
        for (agent_id, node_name, service_type), freq in optimal_x.items():
            if freq == 0:
                continue
            ip, port = agent_id.split(':')
            hostPort = get_or_assign_port(node_name, service_type)
            if (node_name, service_type) not in assigned_topology:
                target_topology.append({"nodeName": node_name, "serviceType": service_type, "hostPort": hostPort})
                assigned_topology.add((node_name, service_type))
            allocation.append({
                "agentIP": ip, "agentPort": int(port),
                "targetNode": node_name, "hostPort": hostPort,
                "frequency": freq, "serviceType": service_type,
            })

    return {"target_topology": target_topology, "allocation": allocation}


def _round_robin_allocate(env, gene: list, subscriptions: List[dict], specs: dict) -> dict:
    """
    Round Robin 路由：對每個 service，依序輪流分配到部署了該服務的節點。
    分配 f_h 頻率（不感知 QoS 門檻），與 P2C 同層級的 naive baseline。
    """
    from usc_ts_solver import _get_node_combo_capacity

    services = env.services
    nodes    = env.nodes
    f_h = {s: specs['services'][s]['frequencyLimit'][0] for s in services}

    deployment: Dict[str, List[str]] = {}
    for ni, node in enumerate(nodes):
        mask = gene[ni]
        combo = sorted([s for si, s in enumerate(services) if mask & (1 << si)])
        if combo:
            deployment[node] = combo

    combo_cap = _get_node_combo_capacity(specs, deployment)
    remaining = dict(combo_cap)

    # 每個服務獨立的輪轉指標
    rr_idx: Dict[str, int] = {s: 0 for s in services}

    optimal_x: Dict = {}
    for sub in subscriptions:
        agent_id = f"{sub['agentIP']}:{sub['agentPort']}"
        reqs = [e['serviceType'] for e in sub.get('subscriptions', [])]

        for svc in reqs:
            candidates = sorted([k for k in combo_cap if k[1] == svc])
            if not candidates:
                continue

            n = len(candidates)
            for attempt in range(n):
                node_key = candidates[(rr_idx[svc] + attempt) % n]
                avail = remaining.get(node_key, 0)
                if avail > 0:
                    freq = min(f_h.get(svc, 0), avail)
                    if freq > 0:
                        optimal_x[(agent_id, node_key[0], svc)] = freq
                        remaining[node_key] = avail - freq
                        rr_idx[svc] = (rr_idx[svc] + attempt + 1) % n
                        break

    return optimal_x


def _static_max_deployment(nodes: List[str], services: List[str], specs: dict) -> Dict[str, List[str]]:
    """
    Static Max 部署：每個節點各自部署能跑的最大服務組合，不考慮使用者需求。
    上限由 specs['nodeCapacity'] 決定（default=3）；全組合有離線量測才有效容量。
    """
    cap_cfg = specs.get("nodeCapacity", {})
    default_cap = int(cap_cfg.get("default", len(services)))
    all_svcs = sorted(services)
    deployment: Dict[str, List[str]] = {}
    for node in nodes:
        cap = int(cap_cfg.get(node, default_cap))
        deployment[node] = all_svcs[:cap]
    return deployment


def _dl3_p2c_allocate(env, gene: list, subscriptions: List[dict], specs: dict) -> dict:
    """
    DL3 P2C (Power of Two Choices) 路由。
    對每個 (agent, service)：隨機挑 2 個有該服務的 pod，
    選剩餘容量較高（負載較低）的那個，分配 f_h 頻率（無 QoS 門檻保護）。
    與 Oracle 的差異：
      Oracle 分配 f_l（保留容量最大化 nsats）；
      DL3   分配 f_h（不感知門檻，貪心使用當前最空閒的 pod）。
    """
    from usc_ts_solver import _get_node_combo_capacity

    services = env.services
    nodes    = env.nodes
    f_h = {s: specs['services'][s]['frequencyLimit'][0] for s in services}

    # 從 gene 重建部署拓撲
    deployment: Dict[str, List[str]] = {}
    for ni, node in enumerate(nodes):
        mask = gene[ni]
        combo = sorted([s for si, s in enumerate(services) if mask & (1 << si)])
        if combo:
            deployment[node] = combo

    combo_cap = _get_node_combo_capacity(specs, deployment)  # {(node, svc): cap}
    remaining = dict(combo_cap)

    # 隨機化 agent 順序（模擬並發到達，避免順序偏差）
    import random as _rnd
    agents = list(subscriptions)
    _rnd.shuffle(agents)

    optimal_x: Dict = {}
    for sub in agents:
        agent_id = f"{sub['agentIP']}:{sub['agentPort']}"
        reqs = [e['serviceType'] for e in sub.get('subscriptions', [])]

        for svc in reqs:
            # 找所有有此服務且剩餘容量 > 0 的候選 pod
            candidates = [k for k in combo_cap if k[1] == svc and remaining.get(k, 0) > 0]
            if not candidates:
                continue

            # P2C：隨機挑最多 2 個，選剩餘容量較多（負載較低）的
            picked = _rnd.sample(candidates, min(2, len(candidates)))
            best   = max(picked, key=lambda k: remaining.get(k, 0))
            best_node = best[0]

            avail = remaining.get((best_node, svc), 0)
            freq  = min(f_h.get(svc, 0), avail)
            if freq <= 0:
                continue

            optimal_x[(agent_id, best_node, svc)] = freq
            remaining[(best_node, svc)] = avail - freq

    return optimal_x


def _svcs_to_deployment(svcs: List[dict], nodes: List[str]) -> Dict[str, List[str]]:
    """從 service.json 的 pod 列表重建 {node: [service_list]} 部署字典。"""
    deployment: Dict[str, List[str]] = {n: [] for n in nodes}
    for svc_entry in svcs:
        n = svc_entry.get('nodeName', '')
        s = svc_entry.get('serviceType', '')
        if n in deployment and s and s not in deployment[n]:
            deployment[n].append(s)
    return deployment


def trigger_solver(subscriptions: List[dict], svcs: List[dict], nodes_status: dict, specs: dict,
                   dry_run: bool = False, event_type: str = "subscribe",
                   phase1_only: bool = False) -> dict:
    """
    統一求解器入口。透過 SOLVER_MODE 環境變數切換：
      'ga'  (預設) – 遺傳演算法聯合優化部署與頻率分配
      'ems'         – USC-TS 決定部署，再以 Oracle nsats_first 分配頻率（EMS 比較基準）
    兩條路徑共用 model_dict 建構、allocate_x_packed_dict、port 分配，
    差異僅在部署染色體（gene）的來源。

    EMS 模式下的事件分流（方案 A）：
      subscribe / node_recovery → SUD episode（Q-learning ADD）
      unsubscribe / node_failure / pod_failure → USC-TS 全量重解（scale-down）

    dry_run=True 時（准入控制模擬）：EMS 路徑跳過 Q-table 更新，
    直接使用當前部署或 USC-TS 冷啟動結果，避免模擬污染訓練資料。
    """
    import sys
    import random

    ga_path = os.path.abspath(os.path.dirname(__file__))
    if ga_path not in sys.path:
        sys.path.append(ga_path)

    try:
        from bench_ga_v2_CUR import GAEnv, init_population, tournament_select, crossover, mutate_hybrid, evaluate, allocate_x_packed_dict, allocate_x_ff_bf_dict, allocate_x_nsats_dict, allocate_x_optimize_dict
    except ImportError as e:
        logging.error(f"Cannot load GA modules: {e}")
        return {"target_topology": [], "allocation": []}

    # ── 共用：建立模型 ────────────────────────────────────────────────────
    model_dict = _build_model_dict(subscriptions, nodes_status, specs)
    if not model_dict["agents"] or not model_dict["nodes"]:
        # 無使用者時保留現有 Pod，避免下次使用者重新訂閱時須重建；但要濾掉
        # 不健康節點上的殘留紀錄——node_failure 只會把節點標成 unhealthy，
        # 不會馬上清 service.json，若這裡原樣回傳整包 svcs，死節點的 pod
        # 會一直留在 target_topology 裡，pods_to_delete 永遠算不到它們，
        # 直到之後有 subscriber 走到 GA/MINLP 那條路徑（那裡透過
        # _svcs_to_deployment 已經會排除不健康節點）才會被清掉。這裡改成
        # 跟那條路徑一致的過濾方式，避免零 subscriber 時的殘留視窗。
        healthy_nodes = set(model_dict["nodes"])
        existing_topology = [
            {"nodeName": s["nodeName"], "serviceType": s["serviceType"], "hostPort": s["hostPort"]}
            for s in svcs
            if s.get("nodeName") in healthy_nodes
        ]
        logging.info("[SOLVER] No agents: retaining %d pods (skip solver)", len(existing_topology))
        return {"target_topology": existing_topology, "allocation": []}

    env = GAEnv(model_dict)

    # ── LSR 模式：兩階段啟發式（部署 + 分配一體，早期返回）────────────────────
    if cfg_solver_mode() == 'lsr':
        from lsr_solver import lsr_solve
        gene, optimal_x = lsr_solve(env)
        logging.info(f"[LSR] gene: {gene}")
        result = _pack_result(env, optimal_x, svcs)
        result["_ga_debug"] = {}
        logging.info(f"[LSR] Solver completed. Agents assigned: {len(result['allocation'])}")
        return result

    # ── MINLP 模式：精確 MILP 求解（聯合部署+分配），作為 GA 的對照 Oracle ──────
    if cfg_solver_mode() == 'minlp':
        if phase1_only:
            # Phase 1 快捷路徑：跳過完整 MILP 重新求解，直接用目前部署 + packed 分配器
            # （若讓 MILP 自由重選 z，可能選到尚未真正部署的拓樸，導致算出的 allocation
            #  對應不到現有 Pod，在後面 podIP 檢查時被悄悄濾掉，Phase 1 形同沒生效）
            current_deployment = _svcs_to_deployment(svcs, model_dict["nodes"])
            if not any(current_deployment.values()):
                logging.info("[MINLP][phase1] 無當前部署，Phase 1 快捷路徑跳過")
                return {"target_topology": [], "allocation": []}
            gene = _topology_to_gene(env, current_deployment)
            for ni in range(len(gene)):
                if gene[ni] == 0 or gene[ni] not in env.all_caps[ni]:
                    gene[ni] = min(env.all_caps[ni].keys())
            logging.info(f"[MINLP][phase1] gene（當前部署）: {gene}")
            optimal_x = allocate_x_packed_dict(env, gene)
            result = _pack_result(env, optimal_x, svcs)
            result["_ga_debug"] = {}
            logging.info(f"[MINLP][phase1] Solver completed. Agents assigned: {len(result['allocation'])}")
            return result

        from minlp_solver import minlp_solve
        gene, optimal_x = minlp_solve(
            env,
            time_limit=cfg_minlp_time_limit(),
            rel_gap=cfg_minlp_rel_gap(),
            max_nodes=cfg_minlp_max_nodes(),
        )
        logging.info(f"[MINLP] gene: {gene}")
        result = _pack_result(env, optimal_x, svcs, gene=gene)
        result["_ga_debug"] = {}
        logging.info(f"[MINLP] Solver completed. Agents assigned: {len(result['allocation'])}")
        return result

    # ── 部署決策：GA / EMS 系列 / DL3 ───────────────────────────────────────
    _ga_debug: dict = {}   # 供 event log 顯示剪枝前後拓撲
    _EMS_MODES = {'ems', 'ems_dl3', 'ems_rr'}
    if cfg_solver_mode() in _EMS_MODES:
        from usc_ts_solver import get_usc_ts_deployment
        from svc_updating_decision import SUDAgent

        current_deployment = _svcs_to_deployment(svcs, model_dict["nodes"])
        sud_agent = SUDAgent.get_instance()

        # ── nodeAliases：真實節點名稱 ↔ Q-table 通用名稱 ─────────────────
        # specs 中設定 "nodeAliases": {"real-node-X": "node-1", ...}
        # 讓 Q-table 始終使用通用名稱，與生產環境節點命名解耦
        node_aliases: Dict[str, str] = specs.get("nodeAliases", {})
        reverse_aliases: Dict[str, str] = {v: k for k, v in node_aliases.items()}

        def to_generic(deployment: Dict[str, list]) -> Dict[str, list]:
            if not node_aliases:
                return deployment
            return {node_aliases.get(n, n): svcs for n, svcs in deployment.items()}

        def to_real(deployment: Dict[str, list]) -> Dict[str, list]:
            if not reverse_aliases:
                return deployment
            return {reverse_aliases.get(n, n): svcs for n, svcs in deployment.items()}

        generic_nodes = [node_aliases.get(n, n) for n in model_dict["nodes"]] if node_aliases else model_dict["nodes"]

        # SUD（svc_updating_decision.py）內部一律用 generic 節點名查 specs["workAbility"]，
        # 但 workAbility 的 key 是真實節點名 → 直接查會全部落空，valid_nodes 永遠是空的，
        # 導致 PSS 判定「有缺口」也找不到合法節點，SUD 實質上無法新增任何 replica。
        # 這裡連同 workAbility 一併轉成 generic key，讓傳給 SUD 的 specs 跟
        # current_deployment/nodes 使用同一套命名空間。
        specs_generic = specs
        if node_aliases:
            specs_generic = dict(specs)
            specs_generic["workAbility"] = {
                node_aliases.get(n, n): v for n, v in specs.get("workAbility", {}).items()
            }

        # 需要 USC-TS 全量重解的事件類型（scale-down 或拓撲變動）
        _USC_TS_EVENTS = {"unsubscribe", "node_failure", "pod_failure"}

        if dry_run:
            # 准入控制模擬：不訓練 Q-table，使用當前部署（或冷啟動時用 USC-TS）
            if sud_agent.q_table:
                topology = current_deployment
                logging.info("[EMS][dry_run] Using current deployment for admission simulation")
            else:
                topology = get_usc_ts_deployment(
                    nodes=model_dict["nodes"], services=model_dict["services"],
                    subscriptions=subscriptions, specs=specs,
                )
                logging.info("[EMS][dry_run] USC-TS cold-start topology for admission simulation")

        elif event_type in _USC_TS_EVENTS:
            # scale-down / 拓撲變動 → USC-TS 全量重解，再讓 SUD 補充遺漏服務
            topology = get_usc_ts_deployment(
                nodes=model_dict["nodes"], services=model_dict["services"],
                subscriptions=subscriptions, specs=specs,
            )
            logging.info("[EMS] USC-TS re-solve (event=%s): %s", event_type, topology)
            # SUD ADD：補充 USC-TS 遺漏、但需求存在的服務（防止 cascade 刪光）
            topology_generic = sud_agent.decide(
                current_deployment=to_generic(topology),
                subscriptions=subscriptions,
                nodes=generic_nodes,
                services=model_dict["services"],
                specs=specs_generic,
            )
            topology = to_real(topology_generic)
            logging.info("[EMS] After SUD (event=%s): %s", event_type, topology)

        elif not sud_agent.initialized:
            # 冷啟動：USC-TS 提供初始部署，再讓 SUD 跑第一個 episode 建立 Q-table
            usc_ts_topology = get_usc_ts_deployment(
                nodes=model_dict["nodes"], services=model_dict["services"],
                subscriptions=subscriptions, specs=specs,
            )
            logging.info("[EMS] Cold start USC-TS topology: %s", usc_ts_topology)
            topology_generic = sud_agent.decide(
                current_deployment=to_generic(usc_ts_topology),
                subscriptions=subscriptions,
                nodes=generic_nodes,
                services=model_dict["services"],
                specs=specs_generic,
            )
            topology = to_real(topology_generic)
            sud_agent.initialized = True  # 無論 SUD 有無動作，標記冷啟動完成

        else:
            # 暖啟動：subscribe / node_recovery → SUD episode（Q-learning ADD）
            # 若 current_deployment 全空（Pod 全死或重啟），先用 USC-TS 重建基線
            if not any(current_deployment.values()):
                current_deployment = get_usc_ts_deployment(
                    nodes=model_dict["nodes"], services=model_dict["services"],
                    subscriptions=subscriptions, specs=specs,
                )
                logging.info("[EMS] Warm-start with empty deployment, USC-TS baseline applied")
            topology_generic = sud_agent.decide(
                current_deployment=to_generic(current_deployment),
                subscriptions=subscriptions,
                nodes=generic_nodes,
                services=model_dict["services"],
                specs=specs_generic,
            )
            topology = to_real(topology_generic)

        gene = _topology_to_gene(env, topology)
        # mask=0（空節點）不在 all_caps，用該節點最小合法 mask 替代，避免 allocator KeyError
        for ni in range(len(gene)):
            if gene[ni] == 0 or gene[ni] not in env.all_caps[ni]:
                gene[ni] = min(env.all_caps[ni].keys())
        logging.info(f"[{cfg_solver_mode().upper()}] Deployment gene: {gene} (dry_run={dry_run})")

    elif cfg_solver_mode() == 'dl3':
        # DL3：Static Max 部署（每節點最大服務組合，不考慮需求）+ P2C 路由
        topology = _static_max_deployment(model_dict["nodes"], model_dict["services"], specs)
        logging.info("[DL3] Static Max topology: %s", topology)
        gene = _topology_to_gene(env, topology)
        for ni in range(len(gene)):
            if gene[ni] == 0 or gene[ni] not in env.all_caps[ni]:
                gene[ni] = min(env.all_caps[ni].keys())
        logging.info(f"[DL3] Deployment gene: {gene}")

    elif cfg_solver_mode() == 'hudson':
        from hudson_solver import hudson_greedy_deploy
        topology = hudson_greedy_deploy(model_dict["nodes"], model_dict["services"], subscriptions, specs)
        logging.info("[HUDSON] Greedy marginal-gain topology: %s", topology)
        gene = _topology_to_gene(env, topology)
        for ni in range(len(gene)):
            if gene[ni] == 0 or gene[ni] not in env.all_caps[ni]:
                gene[ni] = min(env.all_caps[ni].keys())
        logging.info(f"[HUDSON] Deployment gene: {gene}")

    elif cfg_solver_mode() == 'hudson_combo':
        # 共置感知對照版，僅供跟 hudson（單服務獨立容量）比較用，見
        # hudson_combo_solver.py 說明。
        from hudson_combo_solver import hudson_combo_greedy_deploy
        topology = hudson_combo_greedy_deploy(model_dict["nodes"], model_dict["services"], subscriptions, specs)
        logging.info("[HUDSON_COMBO] Greedy marginal-gain topology: %s", topology)
        gene = _topology_to_gene(env, topology)
        for ni in range(len(gene)):
            if gene[ni] == 0 or gene[ni] not in env.all_caps[ni]:
                gene[ni] = min(env.all_caps[ni].keys())
        logging.info(f"[HUDSON_COMBO] Deployment gene: {gene}")

    elif cfg_solver_mode() == 'lai_eua':
        from lai_eua_solver import lai_eua_bin_pack_deploy
        topology = lai_eua_bin_pack_deploy(model_dict["nodes"], model_dict["services"], subscriptions, specs)
        logging.info("[LAI_EUA] Per-service FFD bin-packing topology: %s", topology)
        gene = _topology_to_gene(env, topology)
        for ni in range(len(gene)):
            if gene[ni] == 0 or gene[ni] not in env.all_caps[ni]:
                gene[ni] = min(env.all_caps[ni].keys())
        logging.info(f"[LAI_EUA] Deployment gene: {gene}")

    elif cfg_solver_mode() in ('lsr_v2', 'ems_v2'):
        # 簡化版 LSR/EMS：有需求就增加對應服務，只增不減，新 replica 放負載最輕的合法節點
        from simple_baseline_solver import simple_add_on_demand
        current_deployment = _svcs_to_deployment(svcs, model_dict["nodes"])
        topology = simple_add_on_demand(
            current_deployment, subscriptions, model_dict["nodes"], model_dict["services"], specs,
        )
        logging.info(f"[{cfg_solver_mode().upper()}] Deployment topology: {topology}")
        # 注意：這裡刻意不做「mask=0 → 補最小合法 mask」的正規化（GA 系路徑會做，
        # 因為 all_caps 從不含空組合，補下去會讓「本該保持空」的節點被塞一個 phantom
        # 部署）。lsr_v2/ems_v2 的路由是直接用 bitmask 分解 gene，mask=0 自然產生空
        # combo、不會出錯，所以不需要、也不能套用這個正規化，否則會破壞只增不減的語意。
        gene = _topology_to_gene(env, topology)
        logging.info(f"[{cfg_solver_mode().upper()}] Deployment gene: {gene}")

    else:
        # ── Step 1：Warm Start — 以當前部署為演化基準 ─────────────────────
        # 空節點（mask=0）不在 all_caps，需替換為隨機有效 mask，否則 evaluate 會 KeyError
        current_deployment = _svcs_to_deployment(svcs, model_dict["nodes"])
        has_current = any(current_deployment.values())

        def _safe_gene(topology: dict) -> list:
            gene = []
            for ni, node in enumerate(env.nodes):
                mask = _topology_to_gene(env, topology)[ni]
                if mask == 0 or mask not in env.all_caps[ni]:
                    mask = random.choice(list(env.all_caps[ni].keys()))
                gene.append(mask)
            return gene

        def _is_superset_gene(old_gene: list, new_gene: list) -> bool:
            """逐節點檢查 new_gene 是否完整保留 old_gene 既有部署的服務位元
            （只可能新增、不可能刪除任一節點上原本就有的服務）。"""
            return all((new_gene[ni] & old_gene[ni]) == old_gene[ni]
                       for ni in range(len(old_gene)))

        if phase1_only:
            # Phase 1 快捷路徑：跳過 GA 演化，直接使用當前部署 gene 避免耗時
            # 注意：dry_run=True 是 admission control 用途，GA 仍需跑演化（不在此處理）
            if not has_current:
                logging.info("[GA][phase1] 無當前部署，Phase 1 快捷路徑跳過")
                return {"target_topology": [], "allocation": []}
            gene = _safe_gene(current_deployment)
            logging.info(f"[GA][phase1] Phase 1 gene（當前部署）: {gene}")
        else:
            _ga_eval = "packed" if cfg_solver_mode() == "ga_ff" else "nsats_plus"
            current_gene = _safe_gene(current_deployment) if has_current else None
            current_fit = evaluate(env, current_gene, allocator=_ga_eval) if has_current else (-1, -1.0)

            # 重跑機制：預設ga_n_restarts=1與現況行為完全一致（單一族群、單次演化）。
            # 大於1時，每次重跑各自獨立初始化族群、獨立演化，重跑之間不共用族群
            # 或菁英，僅在全部重跑結束後比較各自的最佳解，取(nsat, Q)字典式排序
            # 最佳者——避免正式環境「單次求解」與離線複雜度實驗「多組種子取最佳」
            # 兩者的表現落差被誤讀為演算法本身的差異（詳見4-3-5節之討論）。
            n_restarts = cfg_ga_n_restarts()
            _ga_time_limit = cfg_ga_time_limit()
            _ga_deadline = (time.perf_counter() + _ga_time_limit) if _ga_time_limit > 0 else None
            best_gene, best_fit = None, (-1, -1.0)
            for restart_idx in range(n_restarts):
                if _ga_deadline is not None and time.perf_counter() >= _ga_deadline:
                    logging.info(f"[GA] 時間預算({_ga_time_limit}s)已耗盡，停止於第 {restart_idx}/{n_restarts} 次重跑前")
                    break
                pop = init_population(env, pop_size=cfg_ga_pop_size(), initial_gene=current_gene if has_current else None)
                run_best_gene, run_best_fit = None, (-1, -1.0)
                for gen_idx in range(cfg_ga_generations()):
                    if _ga_deadline is not None and time.perf_counter() >= _ga_deadline:
                        logging.info(f"[GA] Restart {restart_idx + 1}: 時間預算已耗盡，停止於第 {gen_idx}/{cfg_ga_generations()} 代")
                        break
                    fits = {i: evaluate(env, g, allocator=_ga_eval) for i, g in enumerate(pop)}
                    for i, fit in fits.items():
                        if fit[0] > run_best_fit[0] or (fit[0] == run_best_fit[0] and fit[1] > run_best_fit[1]):
                            run_best_fit = fit
                            run_best_gene = pop[i]
                    new_pop = [run_best_gene[:]]
                    while len(new_pop) < 20:
                        p1 = tournament_select(env, pop, fits)
                        p2 = tournament_select(env, pop, fits)
                        c1, _ = crossover(p1, p2, pc=0.9)
                        c1 = mutate_hybrid(env, c1, pm=0.3)
                        new_pop.append(c1)
                    pop = new_pop
                if run_best_gene is None:
                    # 時間預算在第一代評分前就已耗盡：退回使用族群裡的第一個基因，
                    # 確保任何情況下都有一個可回傳的解（同run_ga()的安全回退寫法）。
                    run_best_gene = pop[0]
                    run_best_fit = evaluate(env, run_best_gene, allocator=_ga_eval)
                is_new_best = run_best_fit[0] > best_fit[0] or (run_best_fit[0] == best_fit[0] and run_best_fit[1] > best_fit[1])
                if is_new_best:
                    best_fit = run_best_fit
                    best_gene = run_best_gene
                if n_restarts > 1:
                    logging.info(f"[GA] Restart {restart_idx + 1}/{n_restarts}: nsat={run_best_fit[0]}, Q={run_best_fit[1]:.3f}"
                                 f"{'  ← 目前最佳' if is_new_best else ''}")

            if best_gene is None:
                # 時間預算在第一次重跑開始前就已耗盡：退回目前部署（若有）或建立
                # 一個全新族群取第一個基因，確保任何情況下都有解可回傳、不會crash。
                if has_current:
                    best_gene, best_fit = current_gene, current_fit
                else:
                    _fallback_pop = init_population(env, pop_size=cfg_ga_pop_size(), initial_gene=None)
                    best_gene = _fallback_pop[0]
                    best_fit = evaluate(env, best_gene, allocator=_ga_eval)
                logging.warning("[GA] 時間預算過短，未完成任何一次重跑，已退回安全預設解")

            # ── Step 2：剪枝 — 移除沒有 agent 連線的閒置服務 ─────────────────
            pre_prune_gene = list(best_gene)   # 記錄剪枝前
            _cur_mode = cfg_solver_mode()
            if _cur_mode == "ga_bf_prune":
                # ga_bf_prune：用 nsats_dict 確認實際連線（與最終分配器一致）
                x_for_prune = allocate_x_nsats_dict(env, best_gene)
                used_node_svcs = {(node, s) for (_, node, s), freq in x_for_prune.items() if freq > 0}
                _do_prune = True
            elif _cur_mode != "ga_bf":
                x_for_prune = allocate_x_packed_dict(env, best_gene)
                used_node_svcs = {(node, s) for (_, node, s), freq in x_for_prune.items() if freq > 0}
                _do_prune = True
            else:
                used_node_svcs = set()
                _do_prune = False

            if _do_prune:
                pruned_gene = list(best_gene)
                for n_idx, node in enumerate(env.nodes):
                    mask = pruned_gene[n_idx]
                    new_mask = mask
                    for s_idx, s in enumerate(env.services):
                        if (mask & (1 << s_idx)) and (node, s) not in used_node_svcs:
                            new_mask &= ~(1 << s_idx)
                    # 剪枝後若全部服務被清除(mask=0)，all_caps 無此 key，用最小合法 mask 替代
                    if new_mask == 0 or new_mask not in env.all_caps[n_idx]:
                        new_mask = min(env.all_caps[n_idx].keys())
                    pruned_gene[n_idx] = new_mask
                best_gene = pruned_gene
                logging.info(f"[GA] Prune: {pre_prune_gene} → {best_gene}  used={used_node_svcs}")
            else:
                logging.info(f"[GA][ga_bf] Pruning skipped, gene: {best_gene}")

            def _gene_to_topo(g):
                """gene → {node: [svc,...]} 可讀格式"""
                t = {}
                for ni, node in enumerate(env.nodes):
                    mask = g[ni]
                    svcs_here = [s for si, s in enumerate(env.services) if mask & (1 << si)]
                    if svcs_here:
                        t[node] = svcs_here
                return t

            _ga_debug = {
                "pre_prune":  _gene_to_topo(pre_prune_gene),
                "post_prune": _gene_to_topo(best_gene),
                "pruned":     _gene_to_topo(pre_prune_gene) != _gene_to_topo(best_gene),
                "used_node_svcs": sorted(str(x) for x in used_node_svcs),
            }

            # ── Step 3：只升不換 — 依事件類型決定保守程度 ────────────────────
            # subscribe / node_recovery：nsats 提升就切換；是否額外允許「nsat
            # 打平但 Q 提升」也觸發切換，由 cfg_deploy_switch_policy() 決定
            # （nsat_only / q_superset / q_any，見該函式說明）
            # unsubscribe：有人離開，q 提升才重組拓樸（避免因一人離開而大規模 Pod churn）
            # node_failure / 其他：允許自由重組
            _CONSERVATIVE_NSATS_EVENTS = {"subscribe", "node_recovery"}
            _CONSERVATIVE_Q_EVENTS     = {"unsubscribe"}
            _DEPLOY_SWITCH_POLICY = cfg_deploy_switch_policy()
            _Q_MIN_GAIN = cfg_q_switch_min_gain()
            if event_type in _CONSERVATIVE_NSATS_EVENTS and has_current:
                _q_tied_improves = (best_fit[0] == current_fit[0]
                                     and (best_fit[1] - current_fit[1]) > _Q_MIN_GAIN)
                if best_fit[0] > current_fit[0]:
                    gene = best_gene
                    logging.info(f"[GA] Deployment upgraded: nsats {current_fit[0]} → {best_fit[0]}")
                elif (_DEPLOY_SWITCH_POLICY == "q_any" and _q_tied_improves):
                    gene = best_gene
                    logging.info(f"[GA] Deployment upgraded (q_any): nsats tied at {current_fit[0]}, "
                                 f"q {current_fit[1]:.4f} → {best_fit[1]:.4f}")
                elif (_DEPLOY_SWITCH_POLICY == "q_superset" and _q_tied_improves
                      and _is_superset_gene(current_gene, best_gene)):
                    gene = best_gene
                    logging.info(f"[GA] Deployment upgraded (q_superset): nsats tied at {current_fit[0]}, "
                                 f"q {current_fit[1]:.4f} → {best_fit[1]:.4f}, no existing service removed")
                else:
                    gene = current_gene
                    logging.info(f"[GA] Deployment unchanged: nsats {current_fit[0]} (new={best_fit[0]}, "
                                 f"q {current_fit[1]:.4f} → {best_fit[1]:.4f}, min_gain={_Q_MIN_GAIN}, "
                                 f"policy={_DEPLOY_SWITCH_POLICY}, no benefit)")
            elif event_type in _CONSERVATIVE_Q_EVENTS and has_current:
                if (best_fit[1] - current_fit[1]) > _Q_MIN_GAIN:
                    gene = best_gene
                    logging.info(f"[GA] Unsubscribe: topology updated (q {current_fit[1]:.4f} → {best_fit[1]:.4f})")
                else:
                    gene = current_gene
                    logging.info(f"[GA] Unsubscribe: topology unchanged (q={current_fit[1]:.4f}, ga_q={best_fit[1]:.4f}, "
                                 f"min_gain={_Q_MIN_GAIN}, no benefit)")
            else:
                gene = best_gene
                logging.info(f"[GA] Full re-solve (event={event_type}): nsats {current_fit[0]} → {best_fit[0]}")

            logging.info(f"[GA] Final gene: {gene}, fit: {best_fit}")

    # ── 路由分配（依模式選擇）────────────────────────────────────────────
    _mode = cfg_solver_mode()
    if _mode in ('dl3', 'ems_dl3'):
        optimal_x = _dl3_p2c_allocate(env, gene, subscriptions, specs)
    elif _mode == 'hudson':
        from hudson_solver import hudson_allocate
        optimal_x = hudson_allocate(env, gene, subscriptions, specs)
    elif _mode == 'hudson_combo':
        from hudson_combo_solver import hudson_combo_allocate
        optimal_x = hudson_combo_allocate(env, gene, subscriptions, specs)
    elif _mode == 'lai_eua':
        from lai_eua_solver import lai_eua_allocate
        optimal_x = lai_eua_allocate(env, gene, subscriptions, specs)
    elif _mode == 'ems_rr':
        optimal_x = _round_robin_allocate(env, gene, subscriptions, specs)
    elif _mode == 'ems':
        # 論文原始方法：round-robin 分配到 replica，頻率公式 freq = min(f_h, max(f_l, C_s/n_agents))
        from usc_ts_solver import round_robin_lb as _rr_lb
        _deployment = {}
        for ni, node in enumerate(env.nodes):
            mask = gene[ni]
            combo = sorted([s for si, s in enumerate(env.services) if mask & (1 << si)])
            if combo:
                _deployment[node] = combo
        _, _alloc_list = _rr_lb(_deployment, subscriptions, specs)
        optimal_x = {
            (f"{a['agentIP']}:{a['agentPort']}", a['targetNode'], a['serviceType']): a['frequency']
            for a in _alloc_list
        }
    elif _mode == 'ems_v2':
        # 簡化版 EMS：部署規則跟 lsr_v2 共用，頻率分配沿用原本的 round_robin_lb 公式
        from usc_ts_solver import round_robin_lb as _rr_lb
        _deployment = {}
        for ni, node in enumerate(env.nodes):
            mask = gene[ni]
            combo = sorted([s for si, s in enumerate(env.services) if mask & (1 << si)])
            if combo:
                _deployment[node] = combo
        _, _alloc_list = _rr_lb(_deployment, subscriptions, specs)
        optimal_x = {
            (f"{a['agentIP']}:{a['agentPort']}", a['targetNode'], a['serviceType']): a['frequency']
            for a in _alloc_list
        }
    elif _mode == 'lsr_v2':
        from simple_baseline_solver import lsr_v2_allocate
        _deployment = {}
        for ni, node in enumerate(env.nodes):
            mask = gene[ni]
            combo = sorted([s for si, s in enumerate(env.services) if mask & (1 << si)])
            if combo:
                _deployment[node] = combo
        _, _alloc_list = lsr_v2_allocate(_deployment, subscriptions, specs)
        optimal_x = {
            (f"{a['agentIP']}:{a['agentPort']}", a['targetNode'], a['serviceType']): a['frequency']
            for a in _alloc_list
        }
    elif _mode == 'ga_ff':
        optimal_x = allocate_x_ff_bf_dict(env, gene)
    elif _mode in ('ga_bf', 'ga_bf_prune'):
        if cfg_ga_routing() == 'nsats':
            optimal_x = allocate_x_nsats_dict(env, gene)
        else:
            optimal_x = allocate_x_optimize_dict(env, gene)
    else:  # ga
        optimal_x = allocate_x_packed_dict(env, gene)
    # lsr_v2/ems_v2：只增不減，target_topology 要從完整 gene 建出，不能只從 optimal_x
    # 反推（否則暫時沒人使用的已部署 replica 會被誤判成「不需要」而被刪除）
    _topo_from_gene_modes = ('ga_bf', 'ga_bf_prune', 'ems_v2', 'lsr_v2')
    result = _pack_result(env, optimal_x, svcs, gene=gene if _mode in _topo_from_gene_modes else None)
    result["_ga_debug"] = _ga_debug
    logging.info(f"[{cfg_solver_mode().upper()}] Solver completed. Agents assigned: {len(result['allocation'])}")
    return result


# ==========================================
# 核心協調引擎 (Reconciliation & Executor)
# ==========================================

_PROBLEM_P_MODES = {"ga", "ga_ff", "ga_bf", "ga_bf_prune", "minlp"}


def _problem_p_admission_check(trigger_info: dict, admission_future: "asyncio.Future",
                                subs_snap: list, specs_snap: dict, target_allocation: list) -> bool:
    """Problem-P（GA/MINLP）專屬的准入判斷。nsat 嚴格提升這個判準只套用於本研究
    之聯合優化模型（Problem P）本身的求解模式：這個判準是由 Problem P 的容量約束、
    達標門檻與 max A_sat 目標函數直接導出的性質（見論文 4-1-9 節），不是移植文獻
    baseline（EMS/LSR/DL3）本身具備、或應該具備的能力。EMS 原始論文只有快取層級
    的聚合決策，無個別使用者准入概念；LSR 原始論文對無法服務者是轉送雲端（z_u），
    不是拒絕/移除，而本系統無雲端層可對應。兩篇原始文獻皆無法對應到「新使用者
    自己沒達標就撤銷訂閱」這種個體准入關卡，故 baseline（呼叫端）一律不呼叫這個
    函式、不設准入判斷，來者不拒，對應論文 5-2-1/5-3 節之描述；分配結果不佳的
    Agent 會留在 subscription.json 中，不會被移除。

    回傳 True 表示准入成功；False 表示已拒絕並回滾 subscription.json，
    呼叫端應該讓整個 reconcile 提早結束（return）。"""
    new_agent_id = trigger_info.get("agent")

    _f_l_map_adm = {s: specs_snap['services'][s]['frequencyLimit'][1]
                    for s in specs_snap.get('services', {})}
    # nsat_before：直接由這次 reconcile 一開始讀入的 subs_snap
    # 算出（排除觸發本次事件的新 Agent），用各訂閱項目已存好
    # 的 frequency（上次成功 reconcile 寫入的值）判斷是否達
    # 標。不可依賴 RECONCILE_HISTORY_FILE 的最後一筆記錄——
    # 那是跨重啟持續累積的獨立 log，Controller 重啟、
    # subscription.json 清空後它不會跟著歸零，會讀到不相干
    # 的舊資料，導致全新系統的第一個使用者也被誤判拒絕。
    nsat_before = sum(
        1 for s in subs_snap
        if f"{s['agentIP']}:{s['agentPort']}" != new_agent_id
        and all(
            sub.get('frequency', 0) >= _f_l_map_adm.get(sub['serviceType'], 0)
            for sub in s.get('subscriptions', [])
        )
    )
    _freq_by_agent_svc_adm: Dict[str, float] = {}
    for a in target_allocation:
        _key = f"{a['agentIP']}:{a['agentPort']}|{a['serviceType']}"
        _freq_by_agent_svc_adm[_key] = (
            _freq_by_agent_svc_adm.get(_key, 0.0) + a.get('frequency', 0)
        )
    nsat_after = sum(
        1 for s in subs_snap
        if all(
            _freq_by_agent_svc_adm.get(
                f"{s['agentIP']}:{s['agentPort']}|{sub['serviceType']}", 0.0
            ) >= _f_l_map_adm.get(sub['serviceType'], 0)
            for sub in s.get('subscriptions', [])
        )
    )
    new_agent_served = nsat_after > nsat_before

    if admission_future and not admission_future.done():
        admission_future.set_result(new_agent_served)
    if not new_agent_served:
        logging.info(f"[ADMISSION] {new_agent_id} rejected: "
                     f"nsat would not strictly increase ({nsat_before} → {nsat_after}), "
                     f"reverting subscription.")
        subs_cur = load_json(SUBSCRIPTION_FILE, [])
        subs_cur = [s for s in subs_cur
                    if f"{s['agentIP']}:{s['agentPort']}" != new_agent_id]
        save_json(SUBSCRIPTION_FILE, subs_cur)
    return new_agent_served


def _problem_p_phase1_allocation(svcs_snap: list, subs_snap: list, nodes_snap: dict,
                                  specs_snap: dict, target_topology: list, etype: str,
                                  t0: float, start_time_ts: float, timing_fn) -> tuple:
    """Problem-P（GA/MINLP）專屬：用「目前已部署的 Pod」快速重解一次，讓受影響的
    訂閱者在 C2/C3 真正建好新 Pod 之前，先拿到一個暫時的降頻分配，避免整段等待
    期間完全沒有服務。這是本論文聯合優化模型的即時回應機制，baseline（EMS/LSR）
    沒有對應設計，不應呼叫這個函式——呼叫端會讓 phase1_alloc 維持空 list，
    下游的 C1 通知區塊本來就是 `if phase1_alloc:` 包起來，baseline 因此自然
    不會發送任何 Phase 1 通知，直接等到 Phase 2（C4）才收到第一次配置。

    timing_fn 是呼叫端 core_reconcile_loop() 內的 _t() 計時 closure，直接傳進來
    共用，避免另外複製一份計時邏輯。
    回傳 (phase1_alloc, t0)：phase1_alloc 可能是空 list（本次不需要/不適用/驗證失敗）。"""
    target_pod_names   = set(f"{t['serviceType']}-{t['nodeName']}-{t['hostPort']}" for t in target_topology)
    snap_pod_names     = set(f"{s['serviceType']}-{s['nodeName']}-{s['hostPort']}" for s in svcs_snap)
    pods_to_create_est = target_pod_names - snap_pod_names
    if not (svcs_snap and pods_to_create_est and cfg_phase1_enabled()):
        return [], t0

    logging.info(f"[Phase 1] 偵測到 {len(pods_to_create_est)} 個新 Pod 待建立，執行階段一臨時分配")
    phase1_result = trigger_solver(subs_snap, svcs_snap, nodes_snap, specs_snap,
                                   event_type=etype, phase1_only=True)
    phase1_alloc = phase1_result.get("allocation", [])
    if phase1_alloc:
        _f_l_map = {s: specs_snap['services'][s]['frequencyLimit'][1]
                    for s in specs_snap.get('services', {})}
        _p1_by_agent_svc = {}
        for _a in phase1_alloc:
            _ak = f"{_a['agentIP']}:{_a['agentPort']}"
            _p1_by_agent_svc.setdefault(_ak, {})[_a['serviceType']] = _a.get('frequency', 0)

        def _agent_ok(_ak: str, _sub: dict) -> bool:
            return all(
                _p1_by_agent_svc.get(_ak, {}).get(_svc_sub['serviceType'], 0)
                >= _f_l_map.get(_svc_sub['serviceType'], 0)
                for _svc_sub in _sub.get('subscriptions', [])
            )

        # node_failure/node_recovery：受影響的都是既有使用者，沒有「新人 vs 既有
        # 使用者」的取捨問題，單純想讓能救的人盡快先被救回來，故放寬為逐 agent
        # 過濾——只排除這次臨時分配裡沒達 f_l 的 agent，其餘合格者照樣提早受益，
        # 不合格者的 entries 直接從 phase1_alloc 移除（不通知、維持原狀，等 Phase 2），
        # 不會因此佔用容量或讓任何人被降到 f_l 以下。
        #
        # subscribe（及其他事件）維持原本全有全無：新人加入不應以任何既有使用者
        # 受干擾為代價，只要有一人不合格就整批放棄 Phase 1、讓大家均等待 Phase 2，
        # 呼應 _problem_p_admission_check() 對新人准入的保守設計，兩者哲學一致。
        _RELAXED_PHASE1_EVENTS = {"node_failure", "node_recovery"}
        if etype in _RELAXED_PHASE1_EVENTS:
            _valid_agent_ids = {
                f"{_sub['agentIP']}:{_sub['agentPort']}"
                for _sub in subs_snap
                if _agent_ok(f"{_sub['agentIP']}:{_sub['agentPort']}", _sub)
            }
            _before = len(phase1_alloc)
            phase1_alloc = [
                _a for _a in phase1_alloc
                if f"{_a['agentIP']}:{_a['agentPort']}" in _valid_agent_ids
            ]
            if len(phase1_alloc) < _before:
                logging.info(f"[Phase 1] ({etype}) 部分放行：{len(_valid_agent_ids)} 個 agent 達 f_l 提早受益，"
                             f"{_before - len(phase1_alloc)} 筆來自未達 f_l 之 agent 的分配被排除，"
                             f"該些 agent 維持原狀等待 Phase 2")
        else:
            _p1_valid = all(
                _agent_ok(f"{_sub['agentIP']}:{_sub['agentPort']}", _sub)
                for _sub in subs_snap
            )
            if not _p1_valid:
                logging.info("[Phase 1] Existing pods insufficient (some agent < f_l), skipping Phase 1")
                phase1_alloc = []
    t0 = timing_fn(f"B2: phase1 solver ({len(pods_to_create_est)} new pods est)", t0, start_time_ts)
    return phase1_alloc, t0


async def core_reconcile_loop(trigger_info=None, admission_future: "asyncio.Future" = None):
    """
    統一的處理工作流 (適用於 Subscribe, Unsubscribe, Node Fail, Node Recover)
    實作: 兩階段遷移 (兩階段分發)、先建後拆 (Make-before-break)、自動回收

    設計：整個流程（讀取快照 → GA 求解 → 執行）全程持同一個 state_lock，
    確保 solver 的決策永遠建立在「上一個 reconcile 完整跑完」的真實狀態上，
    避免 port 衝突與並發 admission 競態。代價是 reconcile 之間完全序列化。
    /subscribe、/unsubscribe 同樣持此鎖，整個流程完全序列化，無並行競態。

    admission_future: 若提供，會在准入判斷完成的瞬間 set_result(bool)，
    讓 /subscribe 可以同步等到准入結果就回應，不必等 Phase 1/Pod 建立跑完
    （那些仍在本 task 內、同一把鎖下繼續背景執行）。
    """
    def _t(label, t0, ref=None):
        """輸出階段耗時；ref 為本次 reconcile 起始時間，印出累計秒。"""
        elapsed = time.time() - t0
        total   = time.time() - ref if ref else elapsed
        logging.info(f"[TIMING] {label:<40} {elapsed:.3f}s  (total {total:.3f}s)")
        return time.time()

    try:
        start_time_ts = time.time()
        logging.info(f">>> Starting Reconcile Loop... Trigger: {trigger_info}")

        # ── 全程持鎖：從讀取快照到最終存檔，中間不放鎖 ──────────────────
        t_lock_wait = time.time()
        async with state_lock:
            queue_wait_s = time.time() - t_lock_wait
            if queue_wait_s > 5:
                logging.warning(f"[QUEUE] Reconcile waited {queue_wait_s:.1f}s for lock (trigger={trigger_info})")

            # ── 階段 A：讀取快照 ──────────────────────────────────────
            t0 = time.time()
            subs_snap  = load_json(SUBSCRIPTION_FILE, [])
            svcs_snap  = load_json(SERVICE_FILE, [])
            nodes_snap = load_json(NODE_STATUS_FILE, {})
            specs_snap = load_json(SERVICESPEC_FILE, {})
            t0 = _t("A: snapshot read", t0, start_time_ts)

            # ── 階段 B：求解 ──────────────────────────────────────────
            etype = trigger_info.get("type", "subscribe") if trigger_info else "subscribe"
            _t_solver = time.time()
            solver_result = trigger_solver(subs_snap, svcs_snap, nodes_snap, specs_snap, event_type=etype)
            solver_time_ms = round((time.time() - _t_solver) * 1000, 1)
            t0 = _t("B: solver", t0, start_time_ts)

            target_topology   = solver_result.get("target_topology", [])
            target_allocation = solver_result.get("allocation", [])

            # ── Unsubscribe 且拓撲不變 → Delta Allocation（維持目標，只改 fps）──
            # LSR/EMS 排除在外：必須永遠用自己 solver_mode 算出來的 target_allocation，
            # 不能被這個通用公式覆蓋，否則會汙染 baseline 對比（見 [[baseline_design]] 的教訓）。
            # hudson/lai_eua 同理排除：兩者的分配器皆對 f_l 門檻敏感（見各自檔案 docstring），
            # 不能被這個不檢查 f_l 的通用比例公式覆蓋。hudson_combo 是 hudson 的共置感知
            # 對照版（僅供新舊容量模型比較用，見 hudson_combo_solver.py），理由相同。
            _DELTA_EXCLUDED_MODES = {"ems", "lsr", "ems_v2", "lsr_v2", "hudson", "hudson_combo", "lai_eua"}
            if etype == "unsubscribe" and cfg_solver_mode() not in _DELTA_EXCLUDED_MODES:
                old_topo = {(s['nodeName'], s['serviceType']) for s in svcs_snap}
                new_topo = {(t['nodeName'], t['serviceType']) for t in target_topology}
                if old_topo == new_topo:
                    target_allocation = _delta_allocation(subs_snap, svcs_snap, specs_snap)
                    logging.info(f"[DELTA] Topology unchanged on unsubscribe ({cfg_solver_mode()}), "
                                 f"delta allocation applied ({len(target_allocation)} entries)")

            # ── 准入判斷（subscribe 觸發時）── Problem-P（GA/MINLP）專屬，
            # baseline（EMS/LSR/DL3）一律不呼叫、直接放行——見
            # _problem_p_admission_check() 的完整說明。
            _is_problem_p = cfg_solver_mode() in _PROBLEM_P_MODES
            if trigger_info and trigger_info.get("type") == "subscribe":
                if _is_problem_p:
                    new_agent_served = _problem_p_admission_check(
                        trigger_info, admission_future, subs_snap, specs_snap, target_allocation)
                else:
                    new_agent_served = True
                    if admission_future and not admission_future.done():
                        admission_future.set_result(new_agent_served)
                if not new_agent_served:
                    return
            target_pod_names  = set(f"{t['serviceType']}-{t['nodeName']}-{t['hostPort']}" for t in target_topology)
            logging.info(f"Target Allocation count: {len(target_allocation)}")

            # ── Phase 1 ── Problem-P（GA/MINLP）專屬，baseline 一律不呼叫，
            # phase1_alloc 維持空 list，下游 C1 通知區塊自然不會發送任何通知
            # ——見 _problem_p_phase1_allocation() 的完整說明。
            if _is_problem_p:
                phase1_alloc, t0 = _problem_p_phase1_allocation(
                    svcs_snap, subs_snap, nodes_snap, specs_snap, target_topology, etype,
                    t0, start_time_ts, _t)
            else:
                phase1_alloc = []
            phase1_triggered = bool(phase1_alloc)

            # ── 階段 C：執行 ──────────────────────────────────────────
            # 全程持有 state_lock，重讀以取得 Phase B 結束後最終的 svcs/subs 狀態。
            svcs = load_json(SERVICE_FILE, [])
            subs = load_json(SUBSCRIPTION_FILE, [])

            current_pod_names = set(f"{s['serviceType']}-{s['nodeName']}-{s['hostPort']}" for s in svcs)
            pods_to_create = target_pod_names - current_pod_names
            pods_to_delete = current_pod_names - target_pod_names

            # 1.5 重置所有連線計數
            for s in svcs:
                s['currentConnection'] = []

            # 記錄本次 reconcile 開始前的舊頻率，供 C1（Phase 1）與 C4（Phase 2）
            # 排序降頻/升頻共用；同時記錄每個 (agent,service) 上一輪實際連線的
            # 節點/port，供下面偵測「掉出 target_allocation」的孤兒訂閱時，
            # 知道要通知哪個舊連線停止。這裡讀 subs 時尚未被本輪任何通知邏輯
            # 修改過，是本輪唯一正確的「之前」基準。
            cur_freq = {}
            prev_active = {}   # (agent_id, serviceType) -> (targetNode, hostPort)，上一輪 freq>0 者
            for _sub in subs:
                _aid = f"{_sub['agentIP']}:{_sub['agentPort']}"
                for _s in _sub.get('subscriptions', []):
                    cur_freq[(_aid, _s['serviceType'])] = _s.get('frequency', 0)
                    if _s.get('frequency', 0) > 0 and _s.get('targetNode') and _s.get('hostPort'):
                        prev_active[(_aid, _s['serviceType'])] = (_s['targetNode'], _s['hostPort'])

            # 2. [兩階段遷移 - 階段一] 降頻加入（Phase 1 通知用快照結果，並行發送）
            if phase1_alloc:
                _c1_jobs = []
                for alloc in phase1_alloc:
                    if alloc.get('frequency', 0) <= 0:
                        continue
                    svc_entry = next(
                        (s for s in svcs
                         if s['nodeName'] == alloc['targetNode']
                         and s['serviceType'] == alloc['serviceType']
                         and s['hostPort'] == alloc['hostPort']),
                        None
                    )
                    if not svc_entry or not svc_entry.get('podIP'):
                        continue
                    body = {
                        'servicename': alloc['serviceType'],
                        'ip':          svc_entry.get('hostIP'),
                        'port':        alloc['hostPort'],
                        'frequency':   alloc['frequency'],
                    }
                    _c1_jobs.append((alloc['agentIP'], alloc['agentPort'], body))
                if _c1_jobs:
                    await notify_agents_two_wave(_c1_jobs, cur_freq, "C1")
                logging.info(f"[Phase 1] 臨時分配完成（{len(phase1_alloc)} 項），開始建立新 Pod")
                t0 = _t(f"C1: phase1 agent notify ({len(phase1_alloc)} allocs)", t0, start_time_ts)

            # 3. [先建後拆] - 建立需要的新 Pod
            pending_ready_tasks = []
            skipped_deployments = set()  # (serviceType, nodeName, hostPort) 連新 port 也建失敗的項目

            # 計算可用 port 範圍（現有 svcs + K8s NodePorts + 本次已分配的 port）
            used_ports_now = set(int(s['hostPort']) for s in svcs)
            used_ports_now |= get_used_node_ports()

            for pod_name in pods_to_create:
                parts    = pod_name.split('-')
                stype    = parts[0]
                port_str = parts[-1]
                nname    = '-'.join(parts[1:-1])
                p_name   = deploy_pod_sync(stype, int(port_str), nname)
                if p_name:
                    pending_ready_tasks.append((p_name, stype, nname, port_str))
                    used_ports_now.add(int(port_str))
                else:
                    # 原 port 還在 Terminating，找下一個可用 port 重試
                    new_port = 31000
                    while new_port in used_ports_now:
                        new_port += 1
                    p_name = deploy_pod_sync(stype, new_port, nname)
                    if p_name:
                        old_port = int(port_str)
                        logging.info(f"Retried {stype} on {nname} with new port {new_port} (old port {old_port} was terminating)")
                        # target_allocation 的 hostPort 同步更新為新 port，讓 C4 notify 找得到
                        for alloc in target_allocation:
                            if (alloc['serviceType'] == stype
                                    and alloc['targetNode'] == nname
                                    and alloc['hostPort'] == old_port):
                                alloc['hostPort'] = new_port
                        pending_ready_tasks.append((p_name, stype, nname, str(new_port)))
                        used_ports_now.add(new_port)
                    else:
                        skipped_deployments.add((stype, nname, int(port_str)))
                        logging.warning(f"deploy_pod_sync skipped {stype} on {nname}:{port_str} even with new port, will retry next reconcile")

            t0 = _t(f"C2: deploy_pod_sync ({len(pending_ready_tasks)}/{len(pods_to_create)} pods submitted)", t0, start_time_ts)

            # 等待所有新建立的 Pod Ready
            new_pod_ips = {}
            for p_name, stype, nname, port_str in pending_ready_tasks:
                t_pod = time.time()
                pod_ip = await wait_pod_ready(p_name)
                new_pod_ips[p_name] = pod_ip
                logging.info(f"[TIMING]   wait_pod_ready {p_name}: {time.time()-t_pod:.3f}s  ip={pod_ip or 'TIMEOUT'}")
                if pod_ip:
                    svcs.append({
                        "nodeName":         nname,
                        "hostIP":           get_node_ip(nname),
                        "serviceType":      stype,
                        "hostPort":         int(port_str),
                        "podIP":            pod_ip,
                        "currentConnection": [],
                        "createdAt":        time.time()
                    })
            if pending_ready_tasks:
                t0 = _t(f"C3: wait_pod_ready total ({len(pending_ready_tasks)} pods)", t0, start_time_ts)

            # 4. [兩階段遷移 - 階段二] 執行最終的 Agent 通知（並行）
            # Phase 4-A: 先完成所有 in-memory 狀態更新，收集 HTTP 任務
            # （cur_freq / prev_active 已在讀取 subs 後、C1 之前算好，這裡共用）

            notify_jobs = []  # (agent_ip, agent_port, body)
            new_active = set()  # (agent_id, serviceType) 這次仍在 target_allocation 裡的配對
            for alloc in target_allocation:
                new_active.add((f"{alloc['agentIP']}:{alloc['agentPort']}", alloc['serviceType']))

                svc_entry = next(
                    (s for s in svcs if s['nodeName'] == alloc['targetNode']
                     and s['serviceType'] == alloc['serviceType']
                     and s['hostPort'] == alloc['hostPort']),
                    None
                )

                if not svc_entry:
                    if (alloc['serviceType'], alloc['targetNode'], alloc['hostPort']) in skipped_deployments:
                        logging.warning(f"Skipping notify for {alloc['serviceType']} on {alloc['targetNode']}:{alloc['hostPort']}: pod terminating, will retry next reconcile")
                    else:
                        logging.error(f"CRITICAL: Resource assigned by solver but not found in svcs list: {alloc['serviceType']} on {alloc['targetNode']}")
                    continue

                target_pod_ip  = svc_entry.get('podIP')
                target_host_ip = svc_entry.get('hostIP')

                if not target_pod_ip:
                    logging.warning(f"Skipping agent notification: Pod IP not ready for {alloc['serviceType']}")
                    continue

                agent_id = f"{alloc['agentIP']}:{alloc['agentPort']}"
                for agent_data in subs:
                    if f"{agent_data['agentIP']}:{agent_data['agentPort']}" == agent_id:
                        for s_sub in agent_data.get('subscriptions', []):
                            if s_sub['serviceType'] == alloc['serviceType']:
                                s_sub['frequency']  = alloc['frequency']
                                s_sub['targetNode'] = alloc['targetNode']
                                s_sub['hostPort']   = alloc['hostPort']
                                s_sub['podIP']      = target_pod_ip

                if agent_id not in svc_entry['currentConnection']:
                    svc_entry['currentConnection'].append(agent_id)

                body = {
                    'servicename': alloc['serviceType'],
                    'ip':          target_host_ip,
                    'port':        alloc['hostPort'],
                    'frequency':   alloc['frequency']
                }
                logging.info(f"Attempting to notify Agent {alloc['agentIP']}:{alloc['agentPort']} with config: {body}")
                notify_jobs.append((alloc['agentIP'], alloc['agentPort'], body))

            # Phase 4-A-2：孤兒訂閱——上一輪 freq>0、這次卻沒有出現在 target_allocation
            # 裡的 (agent,service)，明確送 frequency=0 通知停止並把 subs 記錄歸零。
            # 沒有這一段的話，agent 會拿著上一輪的舊 freq 無限期繼續轉發，悄悄讓
            # solver 自己的容量估算（例如 LSR 的 remaining[ni][si]）失真、真實超額。
            # 依賴 agent_refactored.py _run_loop 的新語意：freq<=0 = 暫停轉發，不是
            # 拿掉節流（舊語意送 0 反而會變成無節流狂發，比不通知更糟）。
            for (agent_id, svc_type) in (prev_active.keys() - new_active):
                old_node, old_port = prev_active[(agent_id, svc_type)]
                svc_entry = next(
                    (s for s in svcs if s['nodeName'] == old_node
                     and s['serviceType'] == svc_type
                     and s['hostPort'] == old_port),
                    None
                )
                if not svc_entry or not svc_entry.get('hostIP'):
                    logging.warning(f"[C4][DROP] Cannot notify stop for {agent_id}/{svc_type} "
                                     f"on {old_node}:{old_port}: pod no longer tracked")
                    continue

                for agent_data in subs:
                    if f"{agent_data['agentIP']}:{agent_data['agentPort']}" == agent_id:
                        for s_sub in agent_data.get('subscriptions', []):
                            if s_sub['serviceType'] == svc_type:
                                s_sub['frequency'] = 0

                agent_ip, agent_port_str = agent_id.rsplit(':', 1)
                body = {
                    'servicename': svc_type,
                    'ip':          svc_entry['hostIP'],
                    'port':        old_port,
                    'frequency':   0
                }
                logging.info(f"[C4][DROP] Notifying {agent_id} to stop {svc_type} "
                             f"(no longer in target_allocation): {body}")
                notify_jobs.append((agent_ip, int(agent_port_str), body))

            # Phase 4-B: 先通知降頻（或持平）的 agent，再通知升頻（含新加入）的 agent
            # 避免升頻 agent 先收到通知時，降頻 agent 還在高頻發送，造成 pod 短暫超載
            if notify_jobs:
                t_notify_all = time.time()
                all_results = await notify_agents_two_wave(notify_jobs, cur_freq, "C4")
                notify_elapsed = time.time() - t_notify_all
                total_failed = [f"{ip}:{port}" for (ip, port, _), r in all_results
                                if r is None or isinstance(r, Exception)]
                logging.info(f"[TIMING] C4 notify {len(notify_jobs)} agents "
                             f"in {notify_elapsed:.3f}s, "
                             f"ok={len(notify_jobs)-len(total_failed)} failed={len(total_failed)}")
            t0 = _t(f"C4: phase2 agent notify ({len(notify_jobs)} agents)", t0, start_time_ts)

            # 5. [回收與先建後拆] - 刪除舊的且不需要的 Pod (Automatic Scale-down)
            if pods_to_delete:
                pre_delete_gap_s = cfg_pre_delete_gap_s()
                if pre_delete_gap_s > 0:
                    logging.info(f"C5: waiting {pre_delete_gap_s}s after notify before deleting "
                                 f"{len(pods_to_delete)} pods")
                    await asyncio.sleep(pre_delete_gap_s)
                for pod_name in pods_to_delete:
                    delete_pod(pod_name)
                    svcs = [s for s in svcs if f"{s['serviceType']}-{s['nodeName']}-{s['hostPort']}" != pod_name]
                t0 = _t(f"C5: delete_pod ({len(pods_to_delete)} pods)", t0, start_time_ts)

            # 6. 保存最終狀態
            save_json(SERVICE_FILE, svcs)
            save_json(SUBSCRIPTION_FILE, subs)
            t0 = _t("C6: save state", t0, start_time_ts)

            # 計算 nsats：所有訂閱服務皆達到頻率下限的 agent 數量
            specs_for_nsats = load_json(SERVICESPEC_FILE, {})
            f_l_map = {s: specs_for_nsats['services'][s]['frequencyLimit'][1]
                       for s in specs_for_nsats.get('services', {})}
            freq_by_agent_svc: Dict[str, float] = {}
            for alloc in target_allocation:
                key = f"{alloc['agentIP']}:{alloc['agentPort']}|{alloc['serviceType']}"
                freq_by_agent_svc[key] = freq_by_agent_svc.get(key, 0.0) + alloc['frequency']

            nsats = 0
            for sub in subs:
                agent_id = f"{sub['agentIP']}:{sub['agentPort']}"
                if all(
                    freq_by_agent_svc.get(f"{agent_id}|{s['serviceType']}", 0.0) >= f_l_map.get(s['serviceType'], 0)
                    for s in sub.get('subscriptions', [])
                ):
                    nsats += 1

            # 計算 Q（第二層目標函數，正規化至 0~1）：已分配頻率 / f_h 上限的比例，
            # 對所有訂閱中的 (agent, service) 配對取平均；未分配到的訂閱以 0 計入分母，
            # 避免漏算造成平均值被拉高。
            f_h_map = {s: specs_for_nsats['services'][s]['frequencyLimit'][0]
                       for s in specs_for_nsats.get('services', {})}
            total_sub_pairs = sum(len(s.get('subscriptions', [])) for s in subs)
            q_score = round(sum(
                alloc['frequency'] / f_h_map[alloc['serviceType']]
                for alloc in target_allocation
                if f_h_map.get(alloc['serviceType'], 0) > 0
            ) / total_sub_pairs, 4) if total_sub_pairs else 0.0

            # 記錄歷史
            total_duration = time.time() - start_time_ts
            history_record = {
                "timestamp":   time.time(),
                "duration":    total_duration,
                "trigger":     trigger_info,
                "solver_mode": cfg_solver_mode(),
                "solver_time_ms": solver_time_ms,
                "nsats":         nsats,
                "q_score":       q_score,
                "total_agents":  len(subs),
                "pruning": {
                    "count_before": len(current_pod_names),
                    "deleted_pods": list(pods_to_delete),
                    "added_pods":   list(pods_to_create),
                    "count_after":  len(target_pod_names)
                },
                "phase1_triggered":   phase1_triggered,
                "phase1_alloc_count": len(phase1_alloc),
                "final_allocation": target_allocation
            }
            append_json_record(RECONCILE_HISTORY_FILE, history_record)
            _write_event_snapshot(trigger_info, svcs, subs, target_allocation, nsats,
                                  total_duration,
                                  ga_debug=solver_result.get("_ga_debug"))
            logging.info(f">>> Reconcile Loop Completed in {total_duration:.3f}s. "
                         f"Pruned {len(pods_to_delete)} pods, created {len(pods_to_create)} pods.")

    except Exception as e:
        logging.error(f"Reconcile loop error: {e}", exc_info=True)
        if admission_future and not admission_future.done():
            admission_future.set_result(False)



# ==========================================
# Config API
# ==========================================

@app.get('/config')
async def get_config():
    return {
        "solver_mode":          cfg_solver_mode(),
        "keep_pods_on_empty":   cfg_keep_pods_on_empty(),
        "ga_pop_size":          cfg_ga_pop_size(),
        "ga_generations":       cfg_ga_generations(),
        "ga_n_restarts":        cfg_ga_n_restarts(),
        "ga_time_limit":        cfg_ga_time_limit(),
        "deploy_switch_policy": cfg_deploy_switch_policy(),
        "notify_wave_gap_s":    cfg_notify_wave_gap_s(),
        "pre_delete_gap_s":     cfg_pre_delete_gap_s(),
        "minlp_time_limit":     cfg_minlp_time_limit(),
        "minlp_rel_gap":        cfg_minlp_rel_gap(),
        "minlp_max_nodes":      cfg_minlp_max_nodes(),
        "raw": dict(_ctrl_cfg),
    }

@app.post('/config')
async def update_config(request: Request):
    body = await request.json()
    allowed = {
        'solver_mode', 'keep_pods_on_empty', 'ga_pop_size', 'ga_generations',
        'ga_n_restarts', 'ga_time_limit',
        'deploy_switch_policy', 'notify_wave_gap_s', 'pre_delete_gap_s',
        'minlp_time_limit', 'minlp_rel_gap', 'minlp_max_nodes',
    }
    unknown = set(body) - allowed
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown keys: {unknown}")
    if 'solver_mode' in body and body['solver_mode'] not in _VALID_MODES:
        raise HTTPException(status_code=400, detail=f"solver_mode must be one of {_VALID_MODES}")
    _VALID_SWITCH_POLICIES = ('nsat_only', 'q_superset', 'q_any')
    if 'deploy_switch_policy' in body and body['deploy_switch_policy'] not in _VALID_SWITCH_POLICIES:
        raise HTTPException(status_code=400,
                             detail=f"deploy_switch_policy must be one of {_VALID_SWITCH_POLICIES}")
    if 'ga_pop_size' in body and int(body['ga_pop_size']) < 1:
        raise HTTPException(status_code=400, detail="ga_pop_size must be >= 1")
    if 'ga_generations' in body and int(body['ga_generations']) < 1:
        raise HTTPException(status_code=400, detail="ga_generations must be >= 1")
    if 'ga_n_restarts' in body and int(body['ga_n_restarts']) < 1:
        raise HTTPException(status_code=400, detail="ga_n_restarts must be >= 1")
    if 'ga_time_limit' in body and float(body['ga_time_limit']) < 0:
        raise HTTPException(status_code=400, detail="ga_time_limit must be >= 0")
    if 'notify_wave_gap_s' in body and float(body['notify_wave_gap_s']) < 0:
        raise HTTPException(status_code=400, detail="notify_wave_gap_s must be >= 0")
    if 'pre_delete_gap_s' in body and float(body['pre_delete_gap_s']) < 0:
        raise HTTPException(status_code=400, detail="pre_delete_gap_s must be >= 0")
    if 'minlp_time_limit' in body and float(body['minlp_time_limit']) <= 0:
        raise HTTPException(status_code=400, detail="minlp_time_limit must be > 0")
    if 'minlp_rel_gap' in body and not (0 <= float(body['minlp_rel_gap']) < 1):
        raise HTTPException(status_code=400, detail="minlp_rel_gap must be in [0, 1)")
    if 'minlp_max_nodes' in body and int(body['minlp_max_nodes']) < 1:
        raise HTTPException(status_code=400, detail="minlp_max_nodes must be >= 1")
    _ctrl_cfg.update(body)
    _save_ctrl_config(_ctrl_cfg)
    logging.info(f"[CONFIG] Updated: {body}")
    return {"updated": body, "current": {
        "solver_mode":          cfg_solver_mode(),
        "keep_pods_on_empty":   cfg_keep_pods_on_empty(),
        "ga_pop_size":          cfg_ga_pop_size(),
        "ga_generations":       cfg_ga_generations(),
        "ga_n_restarts":        cfg_ga_n_restarts(),
        "ga_time_limit":        cfg_ga_time_limit(),
        "deploy_switch_policy": cfg_deploy_switch_policy(),
        "notify_wave_gap_s":    cfg_notify_wave_gap_s(),
        "pre_delete_gap_s":     cfg_pre_delete_gap_s(),
        "minlp_time_limit":     cfg_minlp_time_limit(),
        "minlp_rel_gap":        cfg_minlp_rel_gap(),
        "minlp_max_nodes":      cfg_minlp_max_nodes(),
    }}


# ==========================================
# Status API
# ==========================================

def _compute_extra_metrics(subs: list, svcs: list, specs: dict, allocation: list) -> dict:
    """計算聯合優化相關的精準部署指標。"""
    from usc_ts_solver import _get_node_combo_capacity

    # ── 部署容量：Σ W(i, d_i, j) ─────────────────────────────────────────
    deployment: Dict[str, List[str]] = {}
    for s in svcs:
        n, svc = s.get('nodeName', ''), s.get('serviceType', '')
        if n and svc:
            deployment.setdefault(n, [])
            if svc not in deployment[n]:
                deployment[n].append(svc)

    combo_cap = _get_node_combo_capacity(specs, {n: sorted(v) for n, v in deployment.items()})
    deployed_capacity_fps = sum(combo_cap.values())

    # ── 實際分配量：Σ x_{i,j,k} ──────────────────────────────────────────
    total_allocated_fps = sum(a.get('frequency', 0) for a in allocation)

    # ── 有效 Pod 率 ───────────────────────────────────────────────────────
    total_pods     = len(svcs)
    effective_pods = sum(1 for s in svcs if s.get('currentConnection'))
    effective_pod_ratio = round(effective_pods / total_pods, 3) if total_pods else 0.0

    # ── 無效投入 fps（未達 QoS 門檻 Agent 收到的 fps）───────────────────
    f_l = {s: specs['services'][s]['frequencyLimit'][1] for s in specs.get('services', {})}
    agent_fps: Dict[str, Dict[str, float]] = {}
    for a in allocation:
        aid = f"{a['agentIP']}:{a['agentPort']}"
        agent_fps.setdefault(aid, {})[a['serviceType']] = a.get('frequency', 0)

    wasted_fps = 0.0
    for sub in subs:
        aid  = f"{sub['agentIP']}:{sub['agentPort']}"
        reqs = [e['serviceType'] for e in sub.get('subscriptions', [])]
        fps  = agent_fps.get(aid, {})
        satisfied = reqs and all(fps.get(s, 0) >= f_l.get(s, 0) for s in reqs)
        if not satisfied:
            wasted_fps += sum(fps.values())

    # ── 每滿足人次 Pod 成本 ───────────────────────────────────────────────
    nsats = sum(
        1 for sub in subs
        if (lambda aid, reqs: reqs and all(
            agent_fps.get(aid, {}).get(s, 0) >= f_l.get(s, 0) for s in reqs
        ))(f"{sub['agentIP']}:{sub['agentPort']}",
           [e['serviceType'] for e in sub.get('subscriptions', [])])
    )
    pods_per_sat_user = round(total_pods / nsats, 2) if nsats else 0

    return {
        "total_pods":            total_pods,
        "effective_pods":        effective_pods,
        "effective_pod_ratio":   effective_pod_ratio,
        "deployed_capacity_fps": round(deployed_capacity_fps, 1),
        "total_allocated_fps":   round(total_allocated_fps, 1),
        "capacity_utilization":  round(total_allocated_fps / deployed_capacity_fps, 3)
                                 if deployed_capacity_fps else 0.0,
        "wasted_fps":            round(wasted_fps, 1),
        "pods_per_sat_user":     pods_per_sat_user,
    }


async def _scrape_pod_latency(svcs: list) -> float:
    """從各 Pod 的 admin API 並發抓取平均延遲，回傳加權平均 ms。"""
    seen = set()
    unique_ips = []
    for svc in svcs:
        pod_ip = svc.get("podIP")
        if pod_ip and pod_ip not in seen:
            seen.add(pod_ip)
            unique_ips.append(pod_ip)

    if not unique_ips:
        return 0.0

    def fetch_one(pod_ip):
        try:
            r = requests.get(f"http://{pod_ip}:8000/metrics/latency", timeout=1)
            if r.status_code == 200:
                d = r.json()
                cnt = d.get("count", 0)
                if cnt > 0:
                    return d.get("avg_ms", 0) * cnt, cnt
        except Exception:
            pass
        return 0.0, 0

    loop = asyncio.get_event_loop()
    results = await asyncio.gather(
        *[loop.run_in_executor(None, fetch_one, ip) for ip in unique_ips]
    )

    total_ms    = sum(r[0] for r in results)
    total_count = sum(r[1] for r in results)
    return round(total_ms / total_count, 2) if total_count > 0 else 0.0


@app.get('/status')
async def get_status():
    subs    = load_json(SUBSCRIPTION_FILE, [])
    svcs    = load_json(SERVICE_FILE, [])
    nodes   = load_json(NODE_STATUS_FILE, {})
    specs   = load_json(SERVICESPEC_FILE, {})
    history = load_json(RECONCILE_HISTORY_FILE, [])
    latest  = history[-1] if history else {}
    allocation = latest.get("final_allocation", [])

    extra = _compute_extra_metrics(subs, svcs, specs, allocation)
    avg_response_ms = await _scrape_pod_latency(svcs)

    node_topology = {}
    for svc_entry in svcs:
        node  = svc_entry.get("nodeName", "?")
        stype = svc_entry.get("serviceType", "?")
        node_topology.setdefault(node, {})
        node_topology[node][stype] = node_topology[node].get(stype, 0) + 1

    return {
        "n_users":             len(subs),
        "nsats":               latest.get("nsats", 0),
        "solver_mode":         cfg_solver_mode(),
        "keep_pods_on_empty":  cfg_keep_pods_on_empty(),
        "node_status":         nodes,
        "node_topology":       node_topology,
        "pods_added":          latest.get("pruning", {}).get("added_pods", []),
        "pods_deleted":        latest.get("pruning", {}).get("deleted_pods", []),
        "last_trigger":        latest.get("trigger", {}),
        "last_ts":             latest.get("timestamp", 0),
        "avg_response_ms":     avg_response_ms,
        "phase1_triggered":    latest.get("phase1_triggered",   False),
        "phase1_alloc_count":  latest.get("phase1_alloc_count", 0),
        "solver_time_ms":      latest.get("solver_time_ms",     0),
        "q_score":             latest.get("q_score",            0),
        **extra,
    }


# ==========================================
# 事件 API 端點 (四大事件)
# ==========================================

@app.post('/subscribe')
async def subscribe(request: Request):
    """
    訂閱服務 API - 僅支持新格式（一次訂閱多個服務）

    請求格式：
    {"ip": "...", "port": 1234, "serviceTypes": ["pose", "gesture"]}
    """
    data = await request.json()
    agent_ip = data.get('ip') or request.client.host
    agent_port = data.get('port')

    if 'serviceType' in data:
        raise HTTPException(
            status_code=400,
            detail="Field 'serviceType' is no longer supported. Use non-empty 'serviceTypes' list."
        )

    service_types = data.get('serviceTypes')
    if not isinstance(service_types, list) or len(service_types) == 0:
        raise HTTPException(status_code=400, detail="Must provide non-empty 'serviceTypes' list")

    specs = load_json(SERVICESPEC_FILE, {})
    valid_service_types = list(specs.get('services', {}).keys())

    invalid_types = [st for st in service_types if st not in valid_service_types]
    if invalid_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid service types: {invalid_types}. Valid types: {valid_service_types}"
        )

    added_count = 0
    updated_count = 0
    async with state_lock:
        subs = load_json(SUBSCRIPTION_FILE, [])

        existing_agents = {f"{a['agentIP']}:{a['agentPort']}" for a in subs}
        is_new_agent = f"{agent_ip}:{agent_port}" not in existing_agents

        # ==========================================
        # 正式更新訂閱狀態
        # ==========================================
        agent_entry = next(
            (a for a in subs if a['agentIP'] == agent_ip and a['agentPort'] == agent_port), None
        )
        if not agent_entry:
            agent_entry = {"agentIP": agent_ip, "agentPort": agent_port, "subscriptions": []}
            subs.append(agent_entry)

        for service_type in service_types:
            sub_exists = next(
                (s for s in agent_entry['subscriptions'] if s['serviceType'] == service_type), None
            )
            if not sub_exists:
                agent_entry['subscriptions'].append(
                    {"serviceType": service_type, "podIP": "", "timestamp": time.time()}
                )
                added_count += 1
                logging.info(f"Added subscription: {agent_ip}:{agent_port} -> {service_type}")
            else:
                sub_exists['timestamp'] = time.time()
                updated_count += 1
                logging.info(f"Updated subscription: {agent_ip}:{agent_port} -> {service_type}")

        save_json(SUBSCRIPTION_FILE, subs)
        trigger_info = {"type": "subscribe", "agent": f"{agent_ip}:{agent_port}", "services": service_types}

    # 同步等待准入判斷結果（GA solver 時間，通常 <1s）；
    # Phase 1 / Pod 建立等耗時操作仍在同一個 task、同一把鎖內繼續於背景執行。
    admission_future = asyncio.get_event_loop().create_future()
    _recon_task = asyncio.create_task(core_reconcile_loop(trigger_info, admission_future))
    _bg_tasks_keepalive.add(_recon_task)
    _recon_task.add_done_callback(_bg_tasks_keepalive.discard)
    admitted = await admission_future

    return {
        "message": "Subscription admitted, reconciliation in progress." if admitted
                    else "Subscription rejected: insufficient capacity (< f_l) for requested services.",
        "admitted": admitted,
        "subscribed_services": service_types,
        "total_subscriptions": len(service_types),
        "added": added_count,
        "updated": updated_count,
        "is_new_agent": is_new_agent
    }

@app.post('/unsubscribe')
async def unsubscribe(request: Request, bg_tasks: BackgroundTasks):
    """
    取消訂閱服務 API - 取消指定 agent 的所有訂閱
    
    請求格式：
    {
        "port": 1234
    }
    
    功能說明：
    - 自動識別調用的 agent IP（從 request.client.host 獲取）
    - 根據 IP + Port 組合，刪除該 agent 的所有訂閱記錄
    - 觸發協調引擎重新分配資源
    
    回應：
    {
        "message": "...",
        "agent_ip": "192.168.1.50",
        "agent_port": 1234,
        "unsubscribed_services": ["pose", "gesture", "object"],
        "total_removed": 3
    }
    """
    data = await request.json()
    agent_ip = data.get('ip') or request.client.host  # 優先使用 body 中的 IP，避免 K8s 網路 NAT 問題
    agent_port = data.get('port')
    
    if not agent_port:
        raise HTTPException(status_code=400, detail="Missing required field: port")

    removed_services = []
    async with state_lock:
        subs = load_json(SUBSCRIPTION_FILE, [])

        # 尋找該 agent 的物件
        agent_entry = next((a for a in subs if str(a['agentIP']) == str(agent_ip) and str(a['agentPort']) == str(agent_port)), None)

        if not agent_entry:
            # 已經被移除過（同一個 agent 可能透過不同路徑觸發兩次 unsubscribe），
            # 不需要再跑一次 reconcile
            logging.info(f"Unsubscribe no-op: agent {agent_ip}:{agent_port} not found (already removed)")
            return {
                "message": "Agent not found, no action taken.",
                "agent_ip": agent_ip,
                "agent_port": agent_port,
                "unsubscribed_services": [],
                "total_removed": 0
            }

        removed_services = [s['serviceType'] for s in agent_entry.get('subscriptions', [])]
        # 移除該 agent 的整個 Entry
        subs = [a for a in subs if not (str(a['agentIP']) == str(agent_ip) and str(a['agentPort']) == str(agent_port))]
        removed_count = len(removed_services)

        save_json(SUBSCRIPTION_FILE, subs)

        logging.info(
            f"Removed all subscriptions for agent {agent_ip}:{agent_port} | "
            f"Services: {removed_services} | Total removed: {removed_count}"
        )
        trigger_info = {"type": "unsubscribe", "agent": f"{agent_ip}:{agent_port}", "removed_services": removed_services}

    # 觸發核心協調引擎（非阻塞）
    bg_tasks.add_task(core_reconcile_loop, trigger_info)
    
    return {
        "message": "All subscriptions removed, reconciliation triggered.",
        "agent_ip": agent_ip,
        "agent_port": agent_port,
        "unsubscribed_services": removed_services,
        "total_removed": removed_count
    }

@app.post('/alert')
async def alert(request: Request, bg_tasks: BackgroundTasks):
    data = await request.json()
    alert_type = data.get('alertType')
    alert_content = data.get('alertContent')

    async with state_lock:
        nodes = load_json(NODE_STATUS_FILE, {})
        if alert_type == 'workernode_failure':
            node_name = alert_content.get('nodeName')
            nodes[node_name] = 'unhealthy'
            save_json(NODE_STATUS_FILE, nodes)
            logging.warning(f"Node {node_name} marked as unhealthy.")
            append_json_record(NODESTATUS_EVENTS_FILE, {"timestamp": time.time(), "nodeName": node_name, "status": "unhealthy", "source": "alert"})
            trigger_info = {"type": "node_failure", "nodeName": node_name}
        elif alert_type == 'pod_failure':
            # 針對 Pod failure，可立即從 service_json 移除
            pod_name = alert_content.get('podName')
            svcs = load_json(SERVICE_FILE, [])
            svcs = [s for s in svcs if f"{s['serviceType']}-{s['nodeName']}-{s['hostPort']}" != pod_name]
            save_json(SERVICE_FILE, svcs)
            logging.warning(f"Pod {pod_name} removed from state due to failure.")
            trigger_info = {"type": "pod_failure", "podName": pod_name}

    bg_tasks.add_task(core_reconcile_loop, trigger_info)
    return {"message": f"Alert {alert_type} received, reconciliation triggered."}

@app.post('/noderecovery')
async def noderecovery(request: Request, bg_tasks: BackgroundTasks):
    # 擴充：處理 Node 恢復的事件
    data = await request.json()
    node_name = data.get('nodeName')
    
    async with state_lock:
        nodes = load_json(NODE_STATUS_FILE, {})
        nodes[node_name] = 'healthy'
        save_json(NODE_STATUS_FILE, nodes)
        logging.info(f"Node {node_name} recovered and marked as healthy.")
        append_json_record(NODESTATUS_EVENTS_FILE, {"timestamp": time.time(), "nodeName": node_name, "status": "healthy", "source": "recovery"})
        trigger_info = {"type": "node_recovery", "nodeName": node_name}

    bg_tasks.add_task(core_reconcile_loop, trigger_info)
    return {"message": "Node recovery registered, reconciliation triggered."}
