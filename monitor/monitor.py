import time
import requests
from kubernetes import client, config
import httpx
import asyncio
import logging
import json

LOG_FILE = './logdir/monitor.log'
IN_CLUSTER = True
PROMETHEUS_URL = 'http://prometheus-stack-kube-prom-prometheus.prometheus.svc.cluster.local:9090/api/v1/query'
SERVICESPEC_FILE = './information/serviceSpec_mul.json'

if IN_CLUSTER:
    controller_alert_url = 'http://controller-service:80/alert'
else:
    controller_alert_url = "http://10.52.52.126:30004/alert"

# 設定 logging，將所有日誌寫入到 LOG_FILE
logging.basicConfig(
    filename= LOG_FILE,
    format='%(asctime)s %(levelname)s: %(message)s',
    level=logging.INFO
)

async def query_prometheus(query):
    """發送 PromQL 查詢至 Prometheus"""
    response = requests.get(f"{PROMETHEUS_URL}", params={"query": query})
    if response.status_code == 200:
        data = response.json()
        return data
    else:
        print(f"Error querying Prometheus: {response.status_code}")
        return []
    
async def get_computing_nodes():
    try:
        config.load_incluster_config() if IN_CLUSTER else config.load_kube_config()
    except Exception as e:
        print(f"Error loading kubeconfig: {e}")
        logging.error(f"Error loading kubeconfig: {e}")
        raise

    core_api = client.CoreV1Api()
    # 用來儲存符合條件的節點名稱
    computing_nodes = set()
    
    # 獲取所有節點的標籤
    nodes = core_api.list_node().items
    for node in nodes:
        labels = node.metadata.labels
        # 檢查標籤是否符合條件
        if labels.get('arha-node-type') == 'computing-node':
            computing_nodes.add(node.metadata.name)
    
    return computing_nodes

async def isServiceCrashLoopBackOff(containerName, nodeName):
    """
    查詢 kube_pod_container_status_restarts_total
    若重啟次數超過一次便將Pod視為crashloopbackoff並且回傳Pod名稱
    反之回傳None
    """

    query_restarts_total = f"""
        kube_pod_container_status_restarts_total{{container="{containerName}"}}
    * on (pod, namespace) group_left(node) 
    kube_pod_info{{node="{nodeName}"}}
    """

    query_restarts_total_result =  await query_prometheus(query_restarts_total)
    query_restarts_total_result = query_restarts_total_result['data']['result']
    if query_restarts_total_result and int(query_restarts_total_result[0]['value'][1]) >= 2:
        return (query_restarts_total_result[0]['metric']['pod'], query_restarts_total_result[0]['metric']['uid'])
    else:
        return None
    
async def handle(body):
    async with httpx.AsyncClient(timeout=60) as client:
        await client.post(controller_alert_url, json=body)

async def handle_url(url, body):
    async with httpx.AsyncClient(timeout=60) as client:
        await client.post(url, json=body)

async def check_node_status():
    uid_temp = ''
    computing_nodes_set = await get_computing_nodes()
    computing_nodes_status = {node: 'Ready' for node in computing_nodes_set}

    while True:
        query = 'kube_node_status_condition{condition="Ready",status="true"}'
        try:
            response = requests.get(PROMETHEUS_URL, params={'query': query}, timeout=3)
            if response.status_code == 200:
                data = response.json()
                results = data['data']['result']

                if results:
                    for result in results:
                        node_name = result['metric']['node']
                        status = result['value'][1]
                        if status != '1' and node_name in computing_nodes_set and computing_nodes_status[node_name] != 'NotReady':
                            computing_nodes_status[node_name] = 'NotReady'
                            logging.info(f"Node {node_name} becomes Not Ready")
                            request_to_controller = {
                                "alertType" : "workernode_failure",
                                "alertContent" : {
                                    "nodeName" : node_name
                                }
                            }
                            asyncio.create_task(handle(request_to_controller))
                            logging.info("Send node failure alert to Controller")
                        elif status == '1' and node_name in computing_nodes_set and computing_nodes_status[node_name] != 'Ready':
                            logging.info(f"Node {node_name} becomes Ready")
                            computing_nodes_status[node_name] = 'Ready'
                            request_to_controller = {
                                "nodeName": node_name
                            }
                            controller_recovery_url = controller_alert_url.replace('/alert', '/noderecovery')
                            asyncio.create_task(handle_url(controller_recovery_url, request_to_controller))
                            logging.info("Send node recovery to Controller")
                        elif status == '1' and node_name in computing_nodes_set and computing_nodes_status[node_name] == 'Ready':
                            serviceType_list = []
                            # 查看我們需要檢查哪些serviceType
                            try:
                                with open(SERVICESPEC_FILE, 'r') as serviceSpec_jsonFile:
                                    try:
                                        serviceSpec_data = json.load(serviceSpec_jsonFile)
                                        # 新格式: {"services": {"pose": {...}, ...}}
                                        serviceType_list = list(serviceSpec_data.get('services', {}).keys())
                                    except json.decoder.JSONDecodeError:
                                        logging.info("ServiceSpec file is empty")
                                        continue
                            except FileNotFoundError:
                                logging.info("ServiceSpec file not found")
                                continue

                            for serviceType in serviceType_list:
                                failedPodName, uid = await isServiceCrashLoopBackOff(serviceType, node_name) or ("Unknown", "Unknown")
                                if failedPodName != "Unknown" and uid != uid_temp:
                                    request_to_controller = {
                                        "alertType" : "pod_failure",
                                        "alertContent" : {
                                            "podName" : failedPodName
                                        }
                                    }
                                    uid_temp = uid
                                    asyncio.create_task(handle(request_to_controller))
                                    logging.info("Send pod failure alert to Controller")
                else:
                    print("No nodes found with the specified condition.")
            else:
                print("Failed to fetch data from Prometheus.")
        except requests.exceptions.Timeout:
            print("Query kube_node_status_condition timeout")

        print(computing_nodes_status)
        await asyncio.sleep(5)

async def main():
    await asyncio.gather(
        check_node_status()
    )

if __name__ == '__main__':
    asyncio.run(main())