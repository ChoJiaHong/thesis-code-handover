# arha Helm Chart（順便學Helm用）

這份不只是「怎麼跑」的說明，是照著這個chart實際的檔案，把Helm的核心觀念走一遍。
建議照順序讀，每一節都對應你資料夾裡真實存在的檔案，讀完可以直接動手試。

## 為什麼要用Helm

沒有Helm之前，部署這三個服務要做的事：

```bash
kubectl apply -f controller/controller-deployment.yaml
kubectl apply -f controller/controller-service.yaml
kubectl apply -f controller/controller-rolebinding.yaml
kubectl apply -f agentmanager/agentmanager-deployment.yaml   # 裡面密碼是明碼寫死的
kubectl apply -f agentmanager/agentmanager-service.yaml
kubectl apply -f monitor/monitor-deployment.yaml
kubectl apply -f monitor/monitor-service.yaml
# 然後還要手動 kubectl cp 程式碼進controller/agentmanager的pod、exec進去手動啟動
```

換一台環境（IP不同、要不要加result容器、密碼不同），就要一個個手改yaml。
Helm做的事就是把「一堆yaml」變成「一份chart + 一份可以覆蓋的參數表
（values.yaml）」，換環境只需要換參數，不用改yaml本體。

## Chart長什麼樣子（對照你資料夾裡的檔案）

```
arha/                        ← umbrella chart（頂層，管全局）
├── Chart.yaml                 chart的身分證：名字、版本、依賴哪些subchart
├── values.yaml                全局預設參數（namespace、PVC名稱、node標籤...）
├── .helmignore                打包時要忽略的檔案（像.gitignore）
└── charts/                    umbrella底下掛的三個subchart，各自獨立
    ├── controller/
    │   ├── Chart.yaml
    │   ├── values.yaml         controller自己的預設參數
    │   └── templates/          真正的K8s資源，寫成「樣板」而非死的yaml
    │       ├── deployment.yaml
    │       ├── service.yaml
    │       ├── rolebinding.yaml
    │       ├── _helpers.tpl    可重複使用的小樣板片段
    │       └── NOTES.txt       helm install跑完後印出來的提示訊息
    ├── agentmanager/  （同樣結構，多一個secret.yaml）
    └── monitor/       （同樣結構，最簡單，適合先看這個）
```

**建議先看`charts/monitor/templates/deployment.yaml`**，三個裡面最單純，
沒有`sleepMode`這種條件判斷，看得出「原本的yaml」跟「templates裡的樣板」
哪裡不一樣：把寫死的值（image tag、namespace、node標籤）換成
`{{ .Values.xxx }}`這種佔位符，執行時才由values.yaml的值填進去。

## 動手試——這是學Helm最快的方式

以下指令**不需要真的有K8s叢集**就能跑前三個，全部在`arha/`這個目錄下執行：

```bash
cd thesis-code-handover/helm/arha

# 1. 語法檢查（最常用、最快，寫template時開兩個視窗，改一行存檔就跑一次）
helm lint .

# 2. 把template實際「渲染」成最終yaml印出來看（不會真的連叢集，純本機運算）
#    這是理解Helm在做什麼最直接的方法：改values.yaml或加--set，
#    重跑這行看輸出差在哪
helm template arha .

# 3. 只看某個subchart的輸出（範圍太大時很有用）
helm template arha . --show-only charts/controller/templates/deployment.yaml

# 4. 試改一個值，看輸出怎麼變（這裡示範把controller切回sleepMode）
helm template arha . --set controller.sleepMode=true \
  --show-only charts/controller/templates/deployment.yaml
```

跑完第4個指令，你應該會看到`command: ["sleep", "infinity"]`出現在輸出裡——
這就是`charts/controller/templates/deployment.yaml`裡`{{- if .Values.sleepMode }}`
那段條件判斷在起作用。回去翻那個檔案對照著看，會比看任何教學文章都直觀。

## 真的要裝到叢集上（需要`infra/`層已完成）

裝之前，這些前提要先滿足（見`infra/操作手冊.md`）：
- K8s叢集已建立、GPU Time-Slicing已設定
- `arha-system` namespace存在
- 兩個PVC（`arha-system-information`、`arha-logs-pvc`）已建立——這份chart
  **不會**幫你建這兩個PVC，只會去「掛載」既有的，這是刻意的設計（叢集建置
  跟應用程式部署分成兩層，見`infra/操作手冊.md`開頭說明）

準備好之後：

```bash
# 5. server-side dry-run：真的問過API server「這樣送過去合不合法」，
#    但不會真的建立資源——比helm template更進一步的檢查
helm install arha . --dry-run --debug

# 6. 真的裝
helm install arha .

# 7. 裝完會印出各subchart的NOTES.txt，之後想再看一次
helm status arha

# 8. 列出目前叢集裝了哪些release
helm list

# 9. 改了values.yaml或程式碼版本後要更新，用upgrade不是重裝
helm upgrade arha .

# 10. 升級壞了要退回上一版
helm rollback arha

# 11. 移除整個release（幫你砍掉當初helm install建立的所有資源）
helm uninstall arha
```

## 密碼不要進values.yaml（AgentManager的SSH帳密）

`agentmanager`需要SSH帳密連到Computing Node，原始`agentmanager-deployment.yaml`
是把這三個值明碼寫在`args`裡，這份chart改成用K8s Secret（見
`charts/agentmanager/templates/secret.yaml`），但**Secret的值還是從
values.yaml的`agentHost.*`來**——如果你直接把真密碼寫進這份`values.yaml`再
commit進git，等於沒改善。正確作法：

```bash
# 另外寫一份「不進版控」的檔案，例如 my-secrets.yaml：
cat > my-secrets.yaml <<'EOF'
agentmanager:
  agentHost:
    ip: "10.52.52.xxx"
    account: "真帳號"
    password: "真密碼"
EOF

# 加進.gitignore，然後安裝時用 -f 疊加：
helm install arha . -f my-secrets.yaml
# 或單一個值臨時覆蓋，不寫檔案：
helm install arha . --set agentmanager.agentHost.password='真密碼'
```

`-f`可以疊加多份values檔（後面的蓋過前面同名的key），這是Helm處理「共用
預設值 + 環境專屬覆蓋」最常見的模式，跟`.env.example` vs `.env`的概念很像。

## 三個subchart各自的旋鈕（values.yaml裡可調的重點）

| Key | 說明 |
|---|---|
| `controller.sleepMode` | `true`＝維持原始「sleep infinity+手動kubectl cp」流程；`false`＝直接吃已修好的Dockerfile自動啟動 |
| `controller.extraContainers.enabled` | 要不要加回`result`／`crd-syncer`（已確認重現實驗用不到，預設關） |
| `agentmanager.agentHost.*` | SSH連線資訊，務必用`-f`外部檔案覆蓋，不要寫進這份values.yaml |
| `global.namespace` | 三個subchart共用，改這裡等於全部一起換namespace |
| `global.pvc.information`／`global.pvc.logs` | 對應既有PVC名稱，跟`infra/`那邊建立的要一致 |

## 為什麼Service名稱沒有跟著release name變（沒用`{{ include "xxx.fullname" }}`）

留意`templates/service.yaml`裡的`metadata.name`是寫死的`controller-service`／
`monitor-service`／`agentmanager-service`，Deployment卻是用
`{{ include "controller.fullname" . }}`這種樣板算出來的名字。這不是漏改，是
故意的：`monitor.py`裡直接寫死呼叫`http://controller-service:80/alert`，
`infra/values.yaml`的Alertmanager設定也寫死`monitor-service`這個DNS名稱——
如果Service名稱也套用release-name前綴，這些寫死在程式碼裡的呼叫就會全部斷掉。
這是設計Helm chart時常見的取捨：**新建的資源套模板化命名沒問題，但如果有別的
程式碼用固定名稱去呼叫某個Service，那個Service的名稱就得保持固定**，不能無腦
套用fullname這個慣例。
