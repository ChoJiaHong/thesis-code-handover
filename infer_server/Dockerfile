FROM ultralytics/ultralytics:latest

WORKDIR /app

COPY . /app

# 安裝必要的 Python 套件
RUN pip install grpcio grpcio-tools pydantic fastapi uvicorn grpcio-health-checking==1.65.0 prometheus_client
RUN apt update && apt install -y nano


# 設置環境變數以防止 Python 生成 pyc 檔案
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

CMD ["bash"]
