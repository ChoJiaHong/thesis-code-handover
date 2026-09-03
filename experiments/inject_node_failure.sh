#!/bin/bash
# 對應論文5-2-5節「節點故障情境」之真實網路層故障注入腳本。
# 於experiment_ctrl_v2.py --scenario 2 (高負載設定) 穩定運行期間手動執行，
# 而非使用該腳本內建的--fail-node邏輯（該邏輯僅對Controller送出邏輯事件，並未真的斷網）。
# 見 操作手冊.md「scenario編號與論文三個實驗情境的對應關係」一節。

# 定義目標節點名稱 (請確認與 kubectl get nodes 看到的名稱一致)
NODES=(
    "ip-172-31-25-218"
    "ip-172-31-12-231"
    "ip-172-31-28-107"
)

# 遠端執行的腳本：使用 trap 確保就算中斷也一定會執行還原
REMOTE_CMD='
cleanup() {
    iptables -D INPUT -p tcp --dport 30501 -j DROP 2>/dev/null || true
    iptables -D OUTPUT -p tcp -d 172.31.22.137 -j DROP 2>/dev/null || true
}
trap cleanup EXIT INT TERM

iptables -A INPUT -p tcp --dport 30501 -j DROP
iptables -A OUTPUT -p tcp -d 172.31.22.137 -j DROP
conntrack -F 2>/dev/null || true

echo "規則已注入，等待 60 秒..."
sleep 60
cleanup
'

echo "=== 步驟 1：刪除指定節點上的 Pods 並等待終止 ==="
for node in "${NODES[@]}"; do
    echo "正在刪除 Node: ${node} 上 namespace=default 的 Pod..."
    kubectl delete pods -n default --field-selector "spec.nodeName=${node}" &
done
wait
echo "所有目標 Pod 已順利清除完成。"

echo ""
echo "=== 步驟 2：注入 iptables 網路故障 (持續 60 秒) ==="
for node in "${NODES[@]}"; do
    # 取得該 Node 上的 kube-proxy Pod
    pod_name=$(kubectl get pods -n kube-system \
        -l k8s-app=kube-proxy \
        --field-selector "spec.nodeName=${node}" \
        -o jsonpath='{.items[0].metadata.name}')

    if [ -z "${pod_name}" ]; then
        echo "錯誤: 在 Node ${node} 上找不到 kube-proxy Pod，跳過此節點。"
        continue
    fi

    echo "正在對 Node: ${node} (Proxy Pod: ${pod_name}) 執行斷網測試..."
    kubectl exec -n kube-system "${pod_name}" -- sh -c "${REMOTE_CMD}" &
done

wait
echo "網路故障注入結束，iptables 規則已還原。"
