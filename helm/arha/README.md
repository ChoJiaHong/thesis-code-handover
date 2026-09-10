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

## 打包（`helm package`）——把整個chart變成一個檔案

到目前為止都是直接對著`arha/`這個資料夾跑指令（`helm template .`、`helm install .`）。
但如果要把chart交給別人（例如學弟），與其叫他clone整個`thesis-code-handover`
repo再找到`helm/arha/`這個路徑，不如直接打包成一個檔案給他：

```bash
cd thesis-code-handover/helm/arha
helm package .
```

會在目前目錄產生`arha-0.1.0.tgz`——這個版本號**不是隨便來的，是抄
`Chart.yaml`裡的`version:`欄位**。改一下版本號、重新打包，檔名會跟著換：

```bash
# 例如你改了controller的deployment.yaml，想標記這是新的一版chart
sed -i 's/^version: 0.1.0/version: 0.1.1/' Chart.yaml
helm package .        # 這次會產生 arha-0.1.1.tgz
```

**這裡有兩個version，容易搞混，值得記清楚**：
- `Chart.yaml`的`version:`——chart本身的版本（模板、values結構有改就該bump）
- `Chart.yaml`的`appVersion:`——裡面部署的應用程式版本（controller_v2.py
  是哪一版，跟chart模板寫得好不好無關），純粹給人看的標籤，不影響任何行為

打包出來的`.tgz`是**自我完整**的——把umbrella chart跟三個subchart（`charts/`
底下那三包）、連同這份`README.md`全部打包進去一個檔案，不需要額外複製`charts/`
資料夾：

```bash
# 看裡面實際裝了什麼（會看到 arha/charts/controller/... 等完整路徑）
tar -tzf arha-0.1.0.tgz

# 直接從這個.tgz渲染／安裝，效果跟對著資料夾跑一模一樣，不用先解壓縮
helm template arha arha-0.1.0.tgz
helm install arha arha-0.1.0.tgz
```

### 怎麼分享給別人（依複雜度排序）

1. **最簡單：直接把`.tgz`檔案傳給對方**（email、雲端硬碟、或跟這個git repo
   一起commit）。對方拿到後`helm install arha arha-0.1.0.tgz`就能裝，
   不需要`helm repo add`這些額外設定。**這個專案的規模（交接給一個學弟），
   這樣做就夠了，不需要下面第2種做法。**
2. **進階：架一個chart repository**，讓別人可以`helm repo add`＋
   `helm search repo`找到你的chart、之後`helm upgrade`也能直接抓新版本，
   不用每次都手動傳檔案。做法是把打包好的`.tgz`跟一份`index.yaml`
   （下面指令產生）放到同一個能用http存取的地方（例如GitHub Pages）：

   ```bash
   helm repo index . --url https://<你的域名或GitHub Pages網址>/charts
   # 會產生index.yaml，記錄repo裡有哪些chart、哪些版本、去哪個url下載
   ```

   對方那邊就變成：
   ```bash
   helm repo add arha https://<你的域名>/charts
   helm install my-arha arha/arha
   ```

   這個做法對「持續維護、多人共用」的專案比較划算；單純交接一次性的專案
   （像這份交接包）用第1種做法就好，架repo是多做工。

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
