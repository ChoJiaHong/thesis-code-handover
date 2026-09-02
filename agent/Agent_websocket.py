import asyncio
from concurrent import futures
import datetime
import json
import logging
import os
import queue
import struct
import sys
import threading
import time
from typing import Annotated
from fastapi import FastAPI, Form, Request
import grpc
import requests
import uvicorn
import websockets
import gesture_pb2
import gesture_pb2_grpc
import pose_pb2
import pose_pb2_grpc
import base64

try:
    #python Agent_websocket.py {IP} {Port} {websocket_port} {ip of obj det} {port of obj det} {sending freq of obj det} {ip of gesture det} {port of gesture det} {sending freq of gesture det}

    AgentIP_outside = sys.argv[1]
    AgentIP = '0.0.0.0'
    AgentPort = int(sys.argv[2])
    AgentWebsocketPort = int(sys.argv[3])
except:
    AgentIP = '10.52.52.50'
    AgentPort = 8888
    AgentWebsocketPort = 8889

log_dir = "logs"
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

logging.basicConfig(filename=os.path.join(log_dir, f"Agent_{AgentIP_outside}_{AgentPort}.log"),
                    format='%(asctime)s %(levelname)s: %(message)s',
                    level=logging.INFO)

app = FastAPI()

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
async def subscribe(servicename: Annotated[str, Form()]):
    response = requests.post(f'http://{ControllerIP}:{ControllerPort}/subscribe', {"ip": AgentIP_outside, "port": AgentPort, "serviceType": servicename})
    response = response.json()

    svcidx = Services[servicename]
    ServiceIP[svcidx] = response.get('IP')
    ServicePort[svcidx] = int(response.get('Port'))
    Servicefreq[svcidx] = float(response.get('Frequency'))

    connect_to_service(servicename)

    hasGotService[svcidx] = True

    #start_adjust_freq(svcidx)

    print(ServiceIP)
    print(ServicePort)
    print(Servicefreq)
    print(hasGotService)

    return response

@app.post("/servicechange")
async def service(request: Request):
    data = await request.json()
    servicename = data["servicename"]
    ip = data["ip"]
    port = data["port"]
    frequency = data["frequency"]

    logging.info(f"change {servicename} service to IP: {ip}, Port: {port}, Frequency: {frequency}")

    svcidx = Services[servicename]
    if ip != "null":
        ServiceIP[svcidx] = ip
        ServicePort[svcidx] = int(port)
        connect_to_service(servicename)
    Servicefreq[svcidx] = float(frequency)

    if not hasGotService[svcidx]:
        hasGotService[svcidx] = True
        start_adjust_freq(svcidx)

    print(ServiceIP)
    print(ServicePort)
    print(Servicefreq)
    print(hasGotService)
    return {"status": "200", "message": "OK"}

@app.delete("/subscribe")
async def unsubscribe():
    for service in Services:
        svcidx = Services[service]
        ServiceIP[svcidx] = ""
        ServicePort[svcidx] = 0
        Servicefreq[svcidx] = 0
        hasGotService[svcidx] = False
    print(ServiceIP)
    print(ServicePort)
    print(Servicefreq)
    print(hasGotService)

    body = {'port': AgentPort}

    response = requests.post(f'http://{ControllerIP}:{ControllerPort}/unsubscribe', json.dumps(body))
    return {"status": "200", "message": "OK"}

def run_http_server():
    logging.info(f"Http server started on {AgentIP_outside}:{AgentPort}")
    uvicorn.run(app, host = AgentIP, port = AgentPort)


def connect_to_service(servicename):
    if servicename == 'pose':
        global Pose_detection_channel
        global Pose_detection_stub

        if Pose_detection_channel:
            Pose_detection_channel.close()

        Pose_detection_channel = grpc.insecure_channel(ServiceIP[1] + ":" + str(ServicePort[1]))
        Pose_detection_stub = pose_pb2_grpc.MirrorStub(Pose_detection_channel)
        logging.info(f"connected to {servicename} service, {ServiceIP[1]}:{ServicePort[1]}")
    elif servicename == 'gesture':
        global Gesture_detection_channel
        global Gesture_detection_stub

        if Gesture_detection_channel:
            Gesture_detection_channel.close()

        Gesture_detection_channel = grpc.insecure_channel(ServiceIP[0] + ":" + str(ServicePort[0]))
        Gesture_detection_stub = gesture_pb2_grpc.GestureRecognitionStub(Gesture_detection_channel)
        logging.info(f"connected to {servicename} service, {ServiceIP[0]}:{ServicePort[0]}")

def start_adjust_freq(svcidx):
    if svcidx == 0:
        threading.Thread(target=gesture_det_freq).start()
    elif svcidx == 1:
        threading.Thread(target=pose_det_freq).start()

def pose_det_callback(future):
    try:
        result = future.result()
        print(result)
    except grpc.RpcError as e:
        logging.error(e)
        # 如果需要，可以在這裡重新連線或做其他錯誤處理
        #time.sleep(1)
        #connect_to_service("pose")
        #logging.info("Try to connect to Pose service again")

def pose_det_freq():
    global input_request_pose_queue
    while True:
        try:
            # blocking 取得一筆請求
            request = input_request_pose_queue.get(timeout=1/Servicefreq[1])
            # 如果 queue 中還有其他請求，取出最新的那一筆（丟棄前面的資料）
            while not input_request_pose_queue.empty():
                request = input_request_pose_queue.get_nowait()
        except queue.Empty:
            # 若超時沒拿到資料，直接進入下一輪迴圈
            continue

        t = time.time()
        future = posedet_executor.submit(forward_to_pose_detection, request)
        future.add_done_callback(pose_det_callback)
        # 根據目標頻率進行睡眠控制
        sleeptime = 1 / Servicefreq[1] - (time.time() - t)
        if sleeptime > 0:
            time.sleep(sleeptime)
        else:
            print(f"pose sleep time = {sleeptime}")
            logging.info(f"pose sleep time = {sleeptime}")


def forward_to_pose_detection(request):
        global pose_sendFPS
        global pose_resultFPS

        req = pose_pb2.FrameRequest(
            image_data=request[:-4]
        )
        #send[struct.unpack('i', request[-4:])[0]] = datetime.datetime.now().strftime("%H_%M_%S_%f")[:-3]
        pose_sendFPS += 1
        try:
            t = time.time()
            response = Pose_detection_stub.SkeletonFrame(req)
            logging.info(f"pose detection inference time = {time.time() - t}")
        except Exception as e:
            logging.error(e)
            raise grpc.RpcError("gRPC transmission failed")
        pose_resultFPS += 1
        #get[struct.unpack('i', request[-4:])[0]] = datetime.datetime.now().strftime("%H_%M_%S_%f")[:-3]

        retstr = response.skeletons
        retstr += f"{struct.unpack('i', request[-4:])[0]:04}" + " "
        responses.append(retstr[0:-1])

def gesture_det_callback(future):
    try:
        result = future.result()
        print(result)
    except grpc.RpcError as e:
        logging.error(e)
        # 若需要，可在此處理重新連線或其他錯誤處理
        #time.sleep(1)
        #connect_to_service("gesture")
        #logging.info("Try to connect to Gesture service again")

def gesture_det_freq():
    global input_request_gesture_queue
    while True:
        try:
            # blocking 取得一筆請求
            request = input_request_gesture_queue.get(timeout=1/Servicefreq[0])
            # 如果 queue 中還有其他請求，取出最新的那一筆（丟棄前面的資料）
            while not input_request_gesture_queue.empty():
                request = input_request_gesture_queue.get_nowait()
        except queue.Empty:
            # 若超時沒拿到資料，直接進入下一輪迴圈
            continue

        t = time.time()
        future = gesturedet_executor.submit(forward_to_gesture_detection, request)
        future.add_done_callback(gesture_det_callback)
        # 根據目標頻率進行睡眠控制
        sleeptime = 1 / Servicefreq[0] - (time.time() - t)
        if sleeptime > 0:
            time.sleep(sleeptime)
        else:
            print(f"gesture sleep time = {sleeptime}")
            logging.info(f"gesture sleep time = {sleeptime}")

def forward_to_gesture_detection(request):
        global gesture_sendFPS
        global gesture_resultFPS
        req = gesture_pb2.RecognitionRequest(
            image = base64.b64encode(request[:-4])
        )
        #send[struct.unpack('i', request[-4:])[0]] = datetime.datetime.now().strftime("%H_%M_%S_%f")[:-3]
        gesture_sendFPS += 1
        try:
            t = time.time()
            response = Gesture_detection_stub.Recognition(req)
            logging.info(f"gesture detection inference time = {time.time() - t}")
        except Exception as e:
            logging.error(e)
            raise grpc.RpcError("gRPC transmission failed")
        gesture_resultFPS += 1
        #get[struct.unpack('i', request[-4:])[0]] = datetime.datetime.now().strftime("%H_%M_%S_%f")[:-3]
        result = response.action
        result = json.loads(result)
        retstr = result["Left"] + " " + result["Right"] + " "
        retstr += f"{struct.unpack('i', request[-4:])[0]:04}" + " "
        responses.append(retstr[0:-1])

def counting_FPS():
    global recvFPS
    global pose_sendFPS
    global pose_resultFPS
    global gesture_sendFPS
    global gesture_resultFPS
    global returnFPS
    while True:
        if not (recvFPS == 0 and pose_sendFPS == 0 and pose_resultFPS == 0 and returnFPS == 0):
            logging.info(f"FPS: [receive from AR: {recvFPS}, send to pose service: {pose_sendFPS}, get pose result: {pose_resultFPS}, send to gesture service: {gesture_sendFPS}, get gesture result: {gesture_resultFPS}, return to AR: {returnFPS}]")
        recvFPS = 0
        pose_sendFPS = 0
        pose_resultFPS = 0
        gesture_sendFPS = 0
        gesture_resultFPS = 0
        returnFPS = 0
        time.sleep(1)

async def handle_connection(websocket, path):
    print("Client connected")
    client_ip, client_port = websocket.remote_address
    logging.info(f"WebSocket Client connected from {client_ip}:{client_port}")

    threading.Thread(target = counting_FPS).start()
    #start_adjust_freq(1)

    receive_task = asyncio.create_task(receive_messages(websocket))
    send_task = asyncio.create_task(send_messages(websocket))

    await asyncio.gather(receive_task, send_task)

    print("Client disconnected")

async def receive_messages(websocket):
    global recvFPS
    try:
        async for message in websocket:
            #print(f"received {len(message)}")
            recvFPS += 1
            #print(f"Received message: {message}")
            # 可以在這裡處理接收到的消息，例如存儲或進行某些操作
            #print(struct.unpack('i', message[-4:])[0])
            #recv[struct.unpack('i', message[-4:])[0]] = datetime.datetime.now().strftime("%H_%M_%S_%f")[:-3]

            input_request_pose_queue.put(message)
            input_request_gesture_queue.put(message)

    except Exception as e:
        print("Exception while receiving")
        print(e)
        logging.error(e)

async def send_messages(websocket):
    global returnFPS
    try:
        while True:
            if responses:

                idx = int(responses[0][-4:])
                #ret[idx] = datetime.datetime.now().strftime("%H_%M_%S_%f")[:-3]

                returnFPS += 1
                await websocket.send(responses[0].encode('utf-8'))
                responses.pop(0)
            await asyncio.sleep(0.001)
    except Exception as e:
        print("Exception while sending")
        print(e)
        logging.error(e)


Services = {
    'gesture' : 0,
    'pose' : 1
}
ServiceIP = ["", ""]
ServicePort = [0, 0]
Servicefreq = [0, 0]
hasGotService = [False, False]

input_request_pose_queue = queue.Queue()
input_request_gesture_queue = queue.Queue()
responses = []

Pose_detection_channel = None
Pose_detection_stub = None
Gesture_detection_channel = None
Gesture_detection_stub = None

posedet_executor = futures.ThreadPoolExecutor(max_workers=30)
gesturedet_executor = futures.ThreadPoolExecutor(max_workers=30)

try:
    if sys.argv[4] != "" and int(sys.argv[5]) != 0:
        ServiceIP[1] = sys.argv[4]
        ServicePort[1] = int(sys.argv[5])
        Servicefreq[1] = round(float(sys.argv[6]), 5)
        logging.info(f"Try to connect to Pose service, IP = {ServiceIP[1]}, Port = {ServicePort[1]}, Freq = {Servicefreq[1]}")
        hasGotService[1] = True
        connect_to_service("pose")
        start_adjust_freq(1)
    else:
        logging.info("no Pose service subscribed")
    if sys.argv[7] != "" and int(sys.argv[8]) != 0:
        ServiceIP[0] = sys.argv[7]
        ServicePort[0] = int(sys.argv[8])
        Servicefreq[0] = round(float(sys.argv[9]), 5)
        logging.info(f"Try to connect to Gesture service, IP = {ServiceIP[0]}, Port = {ServicePort[0]}, Freq = {Servicefreq[0]}")
        hasGotService[0] = True
        connect_to_service("gesture")
        start_adjust_freq(0)
    else:
        logging.info("no Gesture service subscribed")
except Exception as e:
    logging.error(e)
    pass

print(AgentIP)
print(AgentPort)
print(AgentWebsocketPort)
ControllerIP = '10.52.52.126'
ControllerPort = 30004

recvFPS = 0
pose_sendFPS = 0
pose_resultFPS = 0
returnFPS = 0

# 啟動 WebSocket 伺服器
async def start_server():
    try:
        websocket_server = await websockets.serve(handle_connection, AgentIP, AgentWebsocketPort)

        print(f"WebSocket server started on ws://{AgentIP}:{AgentWebsocketPort}")
        logging.info(f"WebSocket server started on ws://{AgentIP}:{AgentWebsocketPort}")

        await websocket_server.wait_closed()

    except Exception as e:
        logging.error(f"Failed to start WebSocket server: {e}")

if __name__ == '__main__':
    print("start")

    app.debug = False
    threading.Thread(target = run_http_server).start()

    #recv = ['0'] * 10000
    #send = ['0'] * 10000
    #get = ['0'] * 10000
    #ret = ['0'] * 10000



    # 啟動WebSocket伺服器
    try:

        asyncio.run(start_server())  # 啟動協程

    except Exception as e:
        logging.error(f"Failed to start WebSocket server: {e}")
    asyncio.get_event_loop().run_forever()