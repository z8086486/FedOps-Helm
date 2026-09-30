{{/* Service names retain the existing application contracts. */}}
{{- define "fedops.storageMode" -}}
{{- if eq .Values.deployment.storageMode "auto" -}}
{{- ternary "local" "storage-class" (eq .Values.deployment.topology "single-node") -}}
{{- else -}}{{ .Values.deployment.storageMode }}{{- end -}}
{{- end -}}
{{- define "fedops.roles" -}}
frontend: {name: fedops-web-frontend, service: fedops-web-frontend-service, port: 3000, servicePort: 80}
backend: {name: fedops-web-backend, service: fedops-web-backend-service, port: 4000, servicePort: 4000}
manager: {name: fl-server-st, service: server-manager-service, port: 8000, servicePort: 8000}
performance: {name: fl-perf, service: fl-perf-service, port: 8001, servicePort: 8001}
gateway: {name: fedops-gateway, service: fedops-gateway-service, port: 8080, servicePort: 8080}
registry: {name: fedops1-registry, service: fedops1-registry-svc, port: 8012, servicePort: 8012}
mongo: {name: fedops-mongo-deploy, service: fedops-mongo-np, port: 27017, servicePort: 5000, dataPath: /data/db}
registryMongo: {name: fedops1-registry-mongo, service: fedops1-registry-mongo-svc, port: 27017, servicePort: 27017, dataPath: /data/db}
minio: {name: fedops1-registry-minio, service: fedops1-registry-minio-svc, port: 9000, servicePort: 9000, dataPath: /data}
{{- end -}}

{{- define "fedops.prefix" -}}
{{- printf "%s-%s" .Release.Namespace .Release.Name -}}
{{- end -}}

{{- define "fedops.labels" -}}
app.kubernetes.io/part-of: fedops
app.kubernetes.io/instance: {{ .Release.Name | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service | quote }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{- end -}}

{{- define "fedops.url" -}}
{{- $role := index (include "fedops.roles" .root | fromYaml) .role -}}
{{- printf "http://%s.%s.svc.cluster.local:%v" $role.service .root.Release.Namespace $role.servicePort -}}
{{- end -}}

{{- define "fedops.publicUrl" -}}
{{- $port := "" -}}
{{- if .root.Values.access.publicPort -}}
{{- $port = printf ":%v" .root.Values.access.publicPort -}}
{{- end -}}
{{- if .root.Values.access.singleOrigin -}}
{{- $path := index (dict "managerHost" "/fedops/services/manager" "performanceHost" "/fedops/services/performance" "registryHost" "/fedops/services/registry") .host | default "" -}}
{{- printf "%s://%s%s%s" .root.Values.access.scheme .root.Values.access.webHost $port $path -}}
{{- else -}}
{{- printf "%s://%s%s" .root.Values.access.scheme (index .root.Values.access .host) $port -}}
{{- end -}}
{{- end -}}

{{- define "fedops.claim" -}}
{{- $p := index .root.Values.persistence .role -}}
{{- default (printf "%s-%s-data" (include "fedops.prefix" .root) (.role | kebabcase)) $p.existingClaim -}}
{{- end -}}

{{/* Explicit keys prevent unrelated credentials entering a process. */}}
{{- define "fedops.secretEnv" -}}
{{- range .keys }}
- name: {{ . }}
  valueFrom:
    secretKeyRef:
      name: {{ $.name | quote }}
      key: {{ . }}
{{- end -}}
{{- end -}}
