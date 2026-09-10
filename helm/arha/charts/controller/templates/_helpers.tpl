{{/*
_helpers.tpl：可重複使用的樣板片段，用 {{ include "controller.xxx" . }} 呼叫。
這份寫法跟 `helm create` 鷹架出來的樣板幾乎一樣，是Helm chart的標準慣例——
之後看到任何別人寫的chart，這個檔案的內容通常都長這樣，先認得這個模式很有用。
*/}}

{{- define "controller.name" -}}
{{- .Chart.Name -}}
{{- end -}}

{{/*
fullname：加上release name當前綴，這樣同一個chart裝兩次（不同release name）
才不會撞名。這個專案固定只會裝一份，但養成用fullname的習慣是Helm的通用慣例。
*/}}
{{- define "controller.fullname" -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "controller.labels" -}}
app: controller
app.kubernetes.io/name: {{ include "controller.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
namespace：優先吃全域設定(.Values.global.namespace)，
單獨安裝這個subchart（沒有global）時退回本地.Values.namespace，
再退回寫死的"arha-system"。這種「一路往下找，找不到才用預設值」的寫法
在Helm template裡很常見。
*/}}
{{- define "controller.namespace" -}}
{{- .Values.global.namespace | default .Values.namespace | default "arha-system" -}}
{{- end -}}
