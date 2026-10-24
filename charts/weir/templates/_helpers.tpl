{{- define "weir.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "weir.fullname" -}}
{{- default (printf "%s-%s" .Release.Name (include "weir.name" .)) .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "weir.metricsName" -}}
{{ printf "%s-metrics" (include "weir.fullname" . | trunc 55 | trimSuffix "-") }}
{{- end -}}
{{- define "weir.group" -}}
{{ printf "%s/%s" .Release.Namespace .Release.Name }}
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
{{ printf "%s@%s" .Values.image.repository (required "image.digest must identify a compatible immutable Weir image" .Values.image.digest) }}
{{- end -}}
{{- define "weir.config" -}}
{{- $listeners := dict "application" (printf "[::]:%v" .Values.service.port) "diagnostics" (printf "[::]:%v" .Values.diagnosticsPort) -}}
{{- $config := dict "listeners" $listeners "shutdown_timeout" (printf "%ds" (int .Values.shutdownTimeoutSeconds)) "stores" .Values.stores -}}
{{- $config | toYaml -}}
{{- end -}}
{{- define "weir.validate" -}}
{{- range $key := list "checksum/config" "weir.batchstream.io/source-revision" -}}
{{- if hasKey $.Values.podAnnotations $key -}}
{{- fail (printf "%s is managed by the Chart and cannot be overridden in podAnnotations" $key) -}}
{{- end -}}
{{- end -}}
{{- if eq (int .Values.service.port) (int .Values.diagnosticsPort) -}}
{{- fail "service.port and diagnosticsPort must be distinct" -}}
{{- end -}}
{{- range $key := list "app.kubernetes.io/name" "app.kubernetes.io/instance" "app.kubernetes.io/component" "app.kubernetes.io/version" "app.kubernetes.io/managed-by" "helm.sh/chart" -}}
{{- if hasKey $.Values.metrics.serviceMonitor.labels $key -}}
{{- fail (printf "metrics.serviceMonitor.labels cannot override managed label %s" $key) -}}
{{- end -}}
{{- end -}}
{{- $names := dict -}}
{{- range .Values.stores -}}
{{- if hasKey $names .name -}}{{- fail "stores names must be unique within the group" -}}{{- end -}}
{{- $_ := set $names .name true -}}
{{- $mongodb := default dict .mongodb -}}
{{- $search := default dict .search -}}
{{- if or (regexMatch "^[^:]+://[^/?#]*@" (default "" $mongodb.uri)) (regexMatch "^[^:]+://[^/?#]*@" (default "" $search.url)) -}}
{{- fail "backend URI userinfo is unsupported; use credential-file authentication" -}}
{{- end -}}
{{- end -}}
{{- if and .Values.metrics.enabled (empty .Values.metrics.ingress) .Values.networkPolicy.enabled -}}
{{- fail "metrics.enabled requires explicit metrics.ingress sources when NetworkPolicy is enabled" -}}
{{- end -}}
{{- if and .Values.metrics.serviceMonitor.enabled (not .Values.metrics.enabled) -}}
{{- fail "metrics.serviceMonitor.enabled requires metrics.enabled" -}}
{{- end -}}
{{- $minimum := int .Values.replicaCount -}}
{{- if .Values.autoscaling.enabled -}}
{{- $minimum = int .Values.autoscaling.minReplicaCount -}}
{{- if gt $minimum (int .Values.autoscaling.maxReplicaCount) -}}
{{- fail "autoscaling.minReplicaCount must not exceed maxReplicaCount" -}}
{{- end -}}
{{- if empty .Values.autoscaling.triggers -}}{{- fail "autoscaling.enabled requires triggers" -}}{{- end -}}
{{- range .Values.autoscaling.triggers -}}
{{- if and (eq .type "prometheus") (not $.Values.metrics.enabled) -}}
{{- fail "Prometheus autoscaling requires metrics.enabled" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if and .Values.podDisruptionBudget.enabled (lt $minimum 2) -}}
{{- fail "podDisruptionBudget requires at least two minimum replicas" -}}
{{- end -}}
{{- end -}}
