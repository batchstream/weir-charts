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
{{ printf "%s@%s" .Values.image.repository (required "image.digest must identify a compatible immutable Weir image" .Values.image.digest) }}
{{- end -}}
{{- define "weir.validate" -}}
{{- if and .Values.config.existingSecret .Values.config.data -}}
{{- fail "config.existingSecret and config.data are mutually exclusive" -}}
{{- end -}}
{{- if and (not .Values.config.existingSecret) (not .Values.config.data) -}}
{{- fail "set config.existingSecret or non-secret config.data with node and routes" -}}
{{- end -}}
{{- if .Values.config.data -}}
{{- range .Values.config.data.routes.stores -}}
{{- $backend := default dict .backend -}}
{{- $authentication := default dict $backend.authentication -}}
{{- if or $authentication.username $authentication.password -}}
{{- fail "config.data cannot contain backend credentials; use username_file/password_file and external Secret mounts" -}}
{{- end -}}
{{- end -}}
{{- $node := .Values.config.data.node -}}
{{- $listeners := default dict $node.listeners -}}
{{- if not (or (eq (default "" $listeners.application) (printf "0.0.0.0:%v" .Values.service.port)) (eq (default "" $listeners.application) (printf "[::]:%v" .Values.service.port))) -}}
{{- fail "config.data.node.listeners.application must bind a wildcard IP:<service.port>" -}}
{{- end -}}
{{- if .Values.peer.enabled -}}
{{- if not (or (eq (default "" $listeners.peer) (printf "0.0.0.0:%v" .Values.peer.port)) (eq (default "" $listeners.peer) (printf "[::]:%v" .Values.peer.port))) -}}
{{- fail "config.data.node.listeners.peer must bind a wildcard IP:<peer.port> when peer is enabled" -}}
{{- end -}}
{{- if ne (default "" (default dict $node.discovery).peer_address_env) "WEIR_PEER_ADDRESS" -}}
{{- fail "config.data.node.discovery.peer_address_env must be WEIR_PEER_ADDRESS" -}}
{{- end -}}
{{- else if (default "" $listeners.peer) -}}
{{- fail "config.data.node.listeners.peer requires peer.enabled" -}}
{{- end -}}
{{- $diagnostics := default dict $node.diagnostics -}}
{{- $expected := printf "127.0.0.1:%v" .Values.diagnostics.port -}}
{{- if .Values.metrics.enabled -}}
{{- $expected = printf "0.0.0.0:%v" .Values.diagnostics.port -}}
{{- if not $diagnostics.allow_intranet -}}
{{- fail "metrics requires config.data.node.diagnostics.allow_intranet=true" -}}
{{- end -}}
{{- end -}}
{{- $matches := eq (default "" $diagnostics.address) $expected -}}
{{- if .Values.metrics.enabled -}}
{{- $matches = or $matches (eq (default "" $diagnostics.address) (printf "[::]:%v" .Values.diagnostics.port)) -}}
{{- end -}}
{{- if not $matches -}}
{{- fail "config.data.node.diagnostics.address must match the loopback probe or enabled metrics listener" -}}
{{- end -}}
{{- end -}}
{{- if and .Values.metrics.enabled (empty .Values.metrics.ingress) .Values.networkPolicy.enabled -}}
{{- fail "metrics.enabled requires explicit metrics.ingress sources when NetworkPolicy is enabled" -}}
{{- end -}}
{{- if and .Values.podDisruptionBudget.enabled (lt (int .Values.replicaCount) 2) -}}
{{- fail "podDisruptionBudget requires at least two replicas" -}}
{{- end -}}
{{- end -}}
