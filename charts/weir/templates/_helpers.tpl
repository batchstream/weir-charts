{{- define "weir.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "weir.fullname" -}}
{{- default (printf "%s-%s" .Release.Name (include "weir.name" .)) .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "weir.selectorLabels" -}}
app.kubernetes.io/name: {{ include "weir.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
{{- define "weir.labels" -}}
{{ include "weir.selectorLabels" . }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
{{- define "weir.image" -}}
{{- if .Values.image.digest -}}
{{ printf "%s@%s" .Values.image.repository .Values.image.digest }}
{{- else -}}
{{ printf "%s:%s" .Values.image.repository .Values.image.tag }}
{{- end -}}
{{- end -}}
{{- define "weir.validate" -}}
{{- if and .Values.metrics.enabled .Values.config.data -}}
{{- if or (not .Values.config.data.diagnostics_allow_intranet) (ne (default "" .Values.config.data.diagnostics) (printf "0.0.0.0:%v" .Values.diagnostics.port)) -}}
{{- fail "metrics requires config.data.diagnostics_allow_intranet=true and diagnostics=0.0.0.0:<diagnostics.port>" -}}
{{- end -}}
{{- end -}}
{{- if and .Values.metrics.enabled (empty .Values.metrics.ingress) .Values.networkPolicy.enabled -}}
{{- fail "metrics.enabled requires explicit metrics.ingress sources when NetworkPolicy is enabled" -}}
{{- end -}}
{{- if and .Values.config.existingSecret .Values.config.data -}}
{{- fail "config.existingSecret and config.data are mutually exclusive" -}}
{{- end -}}
{{- if and (not .Values.config.existingSecret) (not .Values.config.data) -}}
{{- fail "set config.existingSecret or non-secret config.data" -}}
{{- end -}}
{{- if and .Values.podDisruptionBudget.enabled (lt (int .Values.replicaCount) 2) -}}
{{- fail "podDisruptionBudget requires at least two replicas" -}}
{{- end -}}
{{- end -}}
