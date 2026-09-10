{{- define "monitor.name" -}}
{{- .Chart.Name -}}
{{- end -}}

{{- define "monitor.fullname" -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "monitor.labels" -}}
app: monitor
app.kubernetes.io/name: {{ include "monitor.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "monitor.namespace" -}}
{{- .Values.global.namespace | default "arha-system" -}}
{{- end -}}
