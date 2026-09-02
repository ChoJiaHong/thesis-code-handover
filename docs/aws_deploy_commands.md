# AWS 部署與壓測指令紀錄

> 整理自原始 `command.aws.sh`，已將個人帳號、來源主機位址、EC2 公開主機名稱與 kubeadm join
> token 等敏感資訊替換為佔位符。內部私有 IP（172.31.x.x）僅為 VPC 內部範例位址，予以保留。

## 1. 將程式碼與相依檔案複製到部署主機

```bash
# 語法：scp -r <帳號>@<來源主機>:<來源路徑> <目的地路徑>
scp -r <USER>@<SOURCE_HOST>:~/git_repo/ha /home/ubuntu/
scp -r <USER>@<SOURCE_HOST>:/home/hiro/git_repo/ar_emulator ./
scp -r <USER>@<SOURCE_HOST>:~/git_repo/ha/k8s_computing_install.sh ./
scp -r <USER>@<SOURCE_HOST>:~/git_repo/ha/NVIDIA_container_rumtime_install.sh ./
scp -r <USER>@<SOURCE_HOST>:~/git_repo/AgentManager ./
scp -r <USER>@<SOURCE_HOST>:~/git_repo/ar_measure_performance/bench_workability.py ./
scp -r <USER>@<SOURCE_HOST>:/home/hiro/git_repo/agent/agent_refactored.py ./
scp -r <USER>@<SOURCE_HOST>:~/git_repo/ha/Controller_v2/controller_v2.py ./
scp -r <USER>@<SOURCE_HOST>:~/git_repo/ha/Controller_v2/bench_ga_v2_CUR.py ./
scp -r <USER>@<SOURCE_HOST>:/home/hiro/git_repo/AgentManager/AgentManager_websocket_refact.py ./
```

## 2. Kubernetes 叢集加入（範例，token 需重新產生）

```bash
export KUBE_EDITOR="nano"

# token 與 CA cert hash 會過期，需在 master 節點上以下方指令重新產生
kubeadm token create --print-join-command

sudo kubeadm join <MASTER_IP>:6443 --token <TOKEN> \
        --discovery-token-ca-cert-hash sha256:<CA_CERT_HASH>
sudo kubeadm reset -f
```

## 3. 離線壓測（`bench_workability.py`）實際執行範例

```bash
python3 bench_workability.py --node workgpu --host <WORKER_INTERNAL_IP> \
    --pose-port 30510 --object-port 31003 --gesture-port 30501 \
    --duration 10 --concurrency 20

python3 bench_workability.py --node workgpu --host <WORKER_INTERNAL_IP> \
    --pose-port 30510 --duration 10 --concurrency 20
```

## 4. Worker / Master 節點 SSH 連線（主機名稱已用佔位符替代，實際請自行填入）

```bash
# worker_1
ssh -i "<SSH_KEY>.pem" ubuntu@<WORKER1_HOST>
# worker_2
ssh -i "<SSH_KEY>.pem" ubuntu@<WORKER2_HOST>
# worker_3
ssh -i "<SSH_KEY>.pem" ubuntu@<WORKER3_HOST>
# worker_4
ssh -i "<SSH_KEY>.pem" ubuntu@<WORKER4_HOST>
# master
ssh -i "<SSH_KEY>.pem" ubuntu@<MASTER_HOST>
```

## 5. 壓測環境依賴安裝

```bash
apt update && apt install -y openssh-client
apt install -y procps
```

## 6. 複製壓測程式檔（gesture/pose/object 各自所需的 proto 與測試圖片）

```bash
mkdir -p gesture pose object

scp <USER>@<SOURCE_HOST>:~/git_repo/ar_measure_performance/bench_workability.py ./
scp <USER>@<SOURCE_HOST>:~/git_repo/ar_measure_performance/requirements.txt ./

scp -r <USER>@<SOURCE_HOST>:~/git_repo/ar_measure_performance/gesture/proto ./gesture/
scp <USER>@<SOURCE_HOST>:~/git_repo/ar_measure_performance/gesture/1280hand.jpg ./gesture/

scp -r <USER>@<SOURCE_HOST>:~/git_repo/ar_measure_performance/pose/proto ./pose/
scp <USER>@<SOURCE_HOST>:~/git_repo/ar_measure_performance/pose/1280.jpg ./pose/

scp -r <USER>@<SOURCE_HOST>:~/git_repo/ar_measure_performance/object/proto ./object/
scp <USER>@<SOURCE_HOST>:~/git_repo/ar_measure_performance/object/1280hand.jpg ./object/
```

## 7. 節點標籤與資料目錄

```bash
sudo mkdir -p /arha /arha/data /arha/logs
sudo cp ./logs/* /arha/logs/
sudo cp ./information/* /arha/data/

kubectl label nodes <MASTER_NODE_NAME> arha-node-type=controller-node
kubectl label nodes <NODE_NAME>        arha-node-type=computing-node
# ...依實際節點數量重複
```

## 8. Containerd / NVIDIA Runtime 設定（GPU 節點必要步驟）

```bash
# 1. 備份舊設定
sudo mv /etc/containerd/config.toml /etc/containerd/config.toml.old

# 2. 產生 containerd 官方預設設定
sudo sh -c "containerd config default > /etc/containerd/config.toml"

# 3. 修正 K8s 所需的 Cgroup 設定
sudo sed -i 's/SystemdCgroup = false/SystemdCgroup = true/g' /etc/containerd/config.toml

# 4. 重新套用 NVIDIA runtime 設定
sudo nvidia-ctk runtime configure --runtime=containerd

# 5. 重啟 containerd
sudo systemctl restart containerd

# 若 nvidia-ctk 解析設定檔出錯，可強制降版後重新設定：
sudo sed -i 's/^version =.*/version = 2/' /etc/containerd/config.toml
sudo nvidia-ctk runtime configure --runtime=containerd
grep "nvidia" /etc/containerd/config.toml
sudo systemctl restart containerd
```
