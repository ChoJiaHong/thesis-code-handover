#!/usr/bin/env python3
"""
bench_workability.py

用途：對指定節點的所有服務組合（gesture / pose / object 及其全部組合）
      進行 gRPC 壓測，計算穩定 QPS 並輸出 serviceSpec_mul.json 的
      workAbility 填表建議值。

使用方式：
  python bench_workability.py \
    --node workgpu\
    --host 10.52.52.25 \
    --gesture-port 31005 \
    --pose-port 31001 \
    --object-port 30515 \
    --duration 15 \
    --concurrency 20

python bench_workability.py \
    --node workgpu\
    --host 10.52.52.25 \
    --gesture-port 30501 \
    --pose-port 31002 \
    --duration 15 \
    --concurrency 20
    
說明：
  - 多服務組合（如 gesture,pose）會「同時並行」壓測，模擬實際部署情況。
  - Stable QPS：只計算 warmup（前 1/3 時間）結束後完成的 requests。
  - 偵測到缺少 port 的服務時，自動跳過含該服務的所有組合。
  - 結果輸出到 results/workability/<node>/<datetime>/ 目錄。
"""

import argparse
import asyncio
import csv
import datetime
import json
import os
import statistics
import sys
import time
from itertools import combinations as _combinations

import grpc
import base64
import io

# ─── Proto 路徑設定 ────────────────────────────────────────────
_script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_script_dir, 'gesture', 'proto'))
sys.path.insert(0, os.path.join(_script_dir, 'pose', 'proto'))
sys.path.insert(0, os.path.join(_script_dir, 'object', 'proto'))

import gesture_pb2
import gesture_pb2_grpc
import pose_pb2
import pose_pb2_grpc
from PIL import Image

# ─── 圖片路徑 ─────────────────────────────────────────────────
_GESTURE_IMAGE_PATH = os.path.join(_script_dir, 'gesture', '1280hand.jpg')
_POSE_IMAGE_PATH    = os.path.join(_script_dir, 'pose',    '1280.jpg')
_OBJECT_IMAGE_PATH  = os.path.join(_script_dir, 'object',  '1280hand.jpg')

# 預載圖片（全域）
GESTURE_IMAGE_B64 = None
POSE_IMAGE_BYTES  = None
OBJECT_IMAGE_BYTES = None


def preload_images():
    global GESTURE_IMAGE_B64, POSE_IMAGE_BYTES, OBJECT_IMAGE_BYTES

    with open(_GESTURE_IMAGE_PATH, 'rb') as f:
        GESTURE_IMAGE_B64 = base64.b64encode(f.read())

    img = Image.open(_POSE_IMAGE_PATH)
    buf = io.BytesIO()
    img.save(buf, format='JPEG')
    POSE_IMAGE_BYTES = buf.getvalue()

    with open(_OBJECT_IMAGE_PATH, 'rb') as f:
        OBJECT_IMAGE_BYTES = bytes(f.read())

    print(f"  [Images] gesture={len(GESTURE_IMAGE_B64)}B  "
          f"pose={len(POSE_IMAGE_BYTES)}B  "
          f"object={len(OBJECT_IMAGE_BYTES)}B")


# ─── gRPC stub 建立 ────────────────────────────────────────────
def _make_stub(service_name: str, service_url: str):
    channel = grpc.aio.insecure_channel(service_url)
    stubs = {
        'gesture': gesture_pb2_grpc.GestureRecognitionStub(channel),
        'pose':    pose_pb2_grpc.MirrorStub(channel),
        'object':  pose_pb2_grpc.MirrorStub(channel),
    }
    return stubs[service_name], channel


# ─── 單次請求 ──────────────────────────────────────────────────
async def _send(service_name: str, stub):
    if service_name == 'gesture':
        req = gesture_pb2.RecognitionRequest(image=GESTURE_IMAGE_B64)
        return await stub.Recognition(req)
    elif service_name == 'pose':
        req = pose_pb2.FrameRequest(image_data=POSE_IMAGE_BYTES)
        return await stub.SkeletonFrame(req)
    else:  # object (server runs pose service intentionally)
        req = pose_pb2.FrameRequest(image_data=OBJECT_IMAGE_BYTES)
        return await stub.SkeletonFrame(req)


# ─── 單服務壓測核心 ────────────────────────────────────────────
async def benchmark_service(service_name: str, service_url: str,
                             duration: int, concurrency: int,
                             send_interval: float = 0.001) -> tuple:
    """
    對單一服務壓測 duration 秒。(已修改為 Worker Pool 模式，確保精確壓力)
    """
    stub, channel = _make_stub(service_name, service_url)
    start_time = time.time()
    end_time = start_time + duration
    warmup_end = start_time + duration / 3  # 前 1/3 為 warmup
    results = []

    # 每個 Worker 不斷重複：發送 -> 等待結果 -> 發下一個
    async def worker():
        while time.time() < end_time:
            send_time = time.time()
            try:
                await _send(service_name, stub)
                recv_time = time.time()
                # 只記錄在時間視窗內完成的請求
                if recv_time <= end_time:
                    results.append((send_time, recv_time, recv_time - send_time))
            except grpc.RpcError:
                pass
            
            # 給予微小讓步，避免佔死 CPU
            await asyncio.sleep(0.0001)

    # 建立固定數量 (=concurrency) 的 Worker 全速運作
    workers = [asyncio.create_task(worker()) for _ in range(concurrency)]
    await asyncio.gather(*workers, return_exceptions=True)
    await channel.close()

    # Stable QPS 計算：只計算 warmup 後「完成」的 requests
    stable = [r for r in results if r[1] >= warmup_end]
    if stable:
        stable_duration = max(r[1] for r in stable) - warmup_end
    else:
        stable_duration = duration * 2 / 3
        
    stable_qps = len(stable) / stable_duration if stable_duration > 0 else 0.0

    return results, stable_qps


# ─── 多服務並行壓測 ────────────────────────────────────────────
async def benchmark_combination(services: list, urls: dict,
                                 duration: int, concurrency: int,
                                 send_interval: float = 0.001) -> dict:
    """
    同時對多個服務壓測（asyncio.gather 使各服務完全並行執行）。
    回傳：{service_name: (results, stable_qps)}
    """
    coros = {
        svc: benchmark_service(svc, urls[svc], duration, concurrency, send_interval)
        for svc in services
    }
    keys = list(coros.keys())
    results = await asyncio.gather(*coros.values())
    return dict(zip(keys, results))


# ─── 統計計算 ──────────────────────────────────────────────────
def compute_statistics(results: list) -> dict:
    if not results:
        return {
            "qps": 0.0, "min_val": 0.0, "max_val": 0.0,
            "avg_val": 0.0, "std_val": 0.0,
            "success_count": 0, "total_duration": 0.0,
            "inference_times": [], "sending_times": [], "recv_times": []
        }
    sending_times   = [r[0] for r in results]
    recv_times      = [r[1] for r in results]
    inference_times = [r[2] for r in results]
    valid           = [t for t in inference_times if t > 0]
    total_duration  = max(recv_times) - min(sending_times)
    return {
        "qps":            len(valid) / total_duration if total_duration > 0 else 0.0,
        "min_val":        min(valid) if valid else 0.0,
        "max_val":        max(valid) if valid else 0.0,
        "avg_val":        statistics.mean(valid) if valid else 0.0,
        "std_val":        statistics.stdev(valid) if len(valid) > 1 else 0.0,
        "success_count":  len(valid),
        "total_duration": total_duration,
        "inference_times": inference_times,
        "sending_times":   sending_times,
        "recv_times":      recv_times,
    }


# ─── 輸出 CSV / TXT ────────────────────────────────────────────
def output_csv(results: list, folder: str, filename: str):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"record_{filename}.csv")
    with open(path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["index", "send_time", "recv_time", "latency_s"])
        for i, (s, r, lat) in enumerate(results):
            writer.writerow([i, f"{s:.4f}", f"{r:.4f}", f"{lat:.4f}"])
    return path


def output_txt(stats: dict, stable_qps: float, combo_key: str,
               svc_name: str, folder: str, filename: str):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"summary_{filename}.txt")
    with open(path, 'w') as f:
        f.write(f"combination    : {combo_key}\n")
        f.write(f"service        : {svc_name}\n")
        f.write(f"successes      : {stats['success_count']}\n")
        f.write(f"total_duration : {stats['total_duration']:.4f} s\n")
        f.write(f"overall_qps    : {stats['qps']:.2f}\n")
        f.write(f"stable_qps     : {stable_qps:.2f}  ← 填入 serviceSpec_mul.json\n")
        f.write(f"min_latency    : {stats['min_val']:.4f} s\n")
        f.write(f"max_latency    : {stats['max_val']:.4f} s\n")
        f.write(f"avg_latency    : {stats['avg_val']:.4f} s\n")
        f.write(f"std_latency    : {stats['std_val']:.4f} s\n")
    return path


# ─── 全部服務組合定義 ──────────────────────────────────────────
_ALL_SERVICES = ['gesture', 'pose', 'object']
_ALL_COMBOS   = [
    list(combo)
    for r in range(1, len(_ALL_SERVICES) + 1)
    for combo in _combinations(_ALL_SERVICES, r)
]  # 共 7 種組合，順序與 serviceSpec_mul.json 一致


# ─── 主流程 ────────────────────────────────────────────────────
async def run_all(args):
    print("\n" + "="*60)
    print(f"  workAbility Benchmark — node: {args.node}  host: {args.host}")
    print(f"  duration={args.duration}s  concurrency={args.concurrency}")
    print("="*60)

    # 確認哪些服務有 port
    urls = {}
    if args.gesture_port:
        urls['gesture'] = f"{args.host}:{args.gesture_port}"
    if args.pose_port:
        urls['pose']    = f"{args.host}:{args.pose_port}"
    if args.object_port:
        urls['object']  = f"{args.host}:{args.object_port}"

    print("\n[Services]")
    for svc, url in urls.items():
        print(f"  {svc}: {url}")

    print("\n[Loading images...]")
    preload_images()

    now_str     = datetime.datetime.now().strftime("%m%d_%H%M%S")
    base_output = os.path.join(_script_dir, 'results', 'workability', args.node, now_str)
    os.makedirs(base_output, exist_ok=True)

    patch = {}  # 最終填表建議

    for combo in _ALL_COMBOS:
        combo_key = ','.join(combo)

        # 跳過缺少 port 的組合
        missing = [s for s in combo if s not in urls]
        if missing:
            print(f"\n[SKIP] {combo_key}  (missing port: {missing})")
            continue

        print(f"\n{'─'*60}")
        print(f"[TEST] {combo_key}  ({len(combo)} service(s) concurrently)")
        for s in combo:
            print(f"       {s} → {urls[s]}")

        combo_results = await benchmark_combination(combo, urls, args.duration, args.concurrency, args.send_interval)

        patch[combo_key] = {}
        for svc_name, (results, stable_qps) in combo_results.items():
            stats    = compute_statistics(results)
            safe_key = combo_key.replace(',', '_')
            folder   = os.path.join(base_output, safe_key, svc_name)
            filename = f"{safe_key}_{svc_name}_{now_str}"

            output_csv(results, folder, filename)
            output_txt(stats, stable_qps, combo_key, svc_name, folder, filename)

            stable_int = int(stable_qps)
            patch[combo_key][svc_name] = stable_int
            print(f"  {svc_name:8s}: {stats['success_count']:5d} req  "
                  f"avg={stats['avg_val']*1000:7.1f} ms  "
                  f"overall={stats['qps']:6.1f} QPS  "
                  f"stable={stable_qps:6.1f} QPS  → {stable_int}")

    # ─── JSON patch 輸出 ───────────────────────────────────────
    node_patch  = {args.node: patch}
    patch_path  = os.path.join(base_output, f"patch_{args.node}_{now_str}.json")

    with open(patch_path, 'w') as f:
        json.dump(node_patch, f, indent=4, ensure_ascii=False)

    print(f"\n{'='*60}")
    print(f"[RESULT] 建議填入 serviceSpec_mul.json 的 workAbility[\"{args.node}\"]：")
    print("="*60)
    print(json.dumps(node_patch, indent=4, ensure_ascii=False))
    print(f"\nPatch 已儲存：{patch_path}")
    print(f"所有結果目錄：{base_output}")
    print(f"\n>>> 確認數值後手動更新 ha/Controller_v2/information/serviceSpec_mul.json <<<")


def main():
    parser = argparse.ArgumentParser(
        description='workAbility 壓測腳本 — 對節點所有服務組合進行 gRPC 壓測並計算穩定 QPS',
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        '--node', required=True,
        help='節點名稱（對應 serviceSpec_mul.json 的 key，如 pdclab）'
    )
    parser.add_argument(
        '--host', required=True,
        help='節點 IP（如 10.52.52.26）'
    )
    parser.add_argument('--gesture-port', type=int, default=None, help='gesture 服務 hostPort')
    parser.add_argument('--pose-port',    type=int, default=None, help='pose 服務 hostPort')
    parser.add_argument('--object-port',  type=int, default=None, help='object 服務 gRPC hostPort')
    parser.add_argument(
        '--duration', type=int, default=30,
        help='每個組合的壓測時長，秒（預設 30）'
    )
    parser.add_argument(
        '--concurrency', type=int, default=20,
        help='每個服務的最大並行請求數（預設 20）'
    )
    parser.add_argument(
        '--send-interval', type=float, default=0.001,
        help='每次建立新 task 的間隔秒數（預設 0.001 = 1ms，即最高 ~1000 req/s）\n'
             '調高可限制送出頻率，例如 0.005 = 200 req/s、0.01 = 100 req/s'
    )

    args = parser.parse_args()

    if not any([args.gesture_port, args.pose_port, args.object_port]):
        parser.error('至少需要指定一個服務的 port（--gesture-port / --pose-port / --object-port）')

    asyncio.run(run_all(args))


if __name__ == '__main__':
    main()
