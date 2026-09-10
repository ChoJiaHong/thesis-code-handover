{{- define "agentmanager.name" -}}
{{- .Chart.Name -}}
{{- end -}}

{{- define "agentmanager.fullname" -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "agentmanager.labels" -}}
app: agentmanager
app.kubernetes.io/name: {{ include "agentmanager.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "agentmanager.namespace" -}}
{{- .Values.global.namespace | default "arha-system" -}}
{{- end -}}
