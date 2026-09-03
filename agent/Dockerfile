FROM python:3.12.1-slim
WORKDIR /app
RUN mkdir -p /app/logs
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# 建議一次性拷貝所有 py 檔案，確保 protobuf 生成的文件都在裡面
COPY *.py ./

# 自產幀模式用的靜態測試圖（跟 ar_emulator/1280hand.jpg 內容相同）
COPY 1280hand.jpg ./

ENTRYPOINT ["python3", "agent_refactored.py"]