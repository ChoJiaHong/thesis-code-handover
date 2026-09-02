from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from batch_config import global_batch_config
from typing import Union
from metrics.max_load_estimator import MaxLoadEstimator
from metrics.registry import monitorRegistry
from config import settings
app = FastAPI()

class BatchConfigUpdate(BaseModel):
    batch_size: Union[int, None] = None
    queue_timeout: Union[float, None] = None

class TimeoutConfigUpdate(BaseModel):
    queue_timeout: float

@app.get("/config/batching")
def get_batch_config():
    return global_batch_config.as_dict()

@app.post("/config/batching")
def update_batch_config(update: BatchConfigUpdate):
    print(update)  # 打印接收到的資料
    if update.batch_size is None and update.queue_timeout is None:
        raise HTTPException(status_code=400, detail="No parameters provided")
    global_batch_config.update(update.batch_size, update.queue_timeout)
    return global_batch_config.as_dict()

@app.get("/config/timeout")
def get_timeout_config():
    """Get current queue timeout configuration."""
    return {"queue_timeout": global_batch_config.queue_timeout}

@app.post("/config/timeout")
def update_timeout_config(update: TimeoutConfigUpdate):
    """Update queue timeout configuration dynamically."""
    if update.queue_timeout <= 0:
        raise HTTPException(status_code=400, detail="queue_timeout must be positive")
    global_batch_config.update(queue_timeout=update.queue_timeout)
    return {"queue_timeout": global_batch_config.queue_timeout, "message": "Timeout updated successfully"}

@app.get("/config/all")
def get_all_config():
    """Get all dynamic configuration parameters."""
    return {
        "batch_config": global_batch_config.as_dict(),
        "static_config": {
            "gRPC_port": settings.gRPC_port,
            "service": settings.service,
            "device": settings.device,
            "weights": settings.weights
        }
    }


@app.get("/metrics/latency")
def get_latency():
    """Return average end-to-end response latency over the last 5 seconds."""
    mon = monitorRegistry.get("latency")
    if mon is None:
        return {"avg_ms": 0.0, "count": 0}
    return mon.get_stats()


class EstimateRequest(BaseModel):
    max_rps: int = 50
    step: int = 5
    duration: float = 5.0


@app.post("/system/estimate_throughput")
def estimate_throughput(req: EstimateRequest):
    estimator = MaxLoadEstimator(
        target=f"localhost:{settings.gRPC_port}",
        duration=req.duration,
        step=req.step,
        max_rps=req.max_rps,
    )
    max_rps = estimator.estimate()
    return {"max_rps": max_rps}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
