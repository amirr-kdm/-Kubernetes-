#!/usr/bin/env bash
# Build -> load -> apply the whole project from scratch, idempotently.
# Usage: ./deploy.sh
set -euo pipefail

CLUSTER="devops-cluster"
NODE="${CLUSTER}-control-plane"
NS="user-system"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Ensure a public image is present on the Kind node. Try `kind load` first;
# fall back to host-pull + `ctr import` (needed when the node's containerd
# cannot reach the registry directly, e.g. broken in-node proxy config, or
# when the image is a multi-arch manifest list that `kind load` rejects).
ensure_image() {
  local img="$1"
  if docker exec "$NODE" crictl images 2>/dev/null | grep -q "${img%:*}.*${img#*:}"; then
    echo "  [ok] $img already on node"
    return
  fi
  echo "  [pull] $img"
  docker pull "$img"
  if ! kind load docker-image "$img" --name "$CLUSTER" 2>/dev/null; then
    echo "  [fallback] kind load failed, importing via ctr"
    docker save "$img" | docker exec -i "$NODE" ctr --namespace=k8s.io images import -
  fi
}

echo "==> 0. Backend image"
docker build -t backend:local ./app
kind load docker-image backend:local --name "$CLUSTER"

echo "==> Public images (postgres / nginx)"
ensure_image postgres:15-alpine
ensure_image nginx:alpine

echo "==> 1. Database"
kubectl apply -f db/secret.yaml
kubectl apply -f db/service.yaml
kubectl apply -f db/statefulset.yaml

echo "==> 2. Backend"
kubectl apply -f backend/configmap.yaml
kubectl apply -f backend/deployment.yaml
kubectl apply -f backend/service.yaml
kubectl apply -f backend/pdb.yaml

echo "==> metrics-server (for HPA) — install if missing"
if ! kubectl get deployment metrics-server -n kube-system >/dev/null 2>&1; then
  kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
  kubectl patch deployment metrics-server -n kube-system --type='json' \
    -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
fi
kubectl apply -f backend/hpa.yaml

echo "==> 3. Nginx"
kubectl apply -f nginx/configmap.yaml
kubectl apply -f nginx/deployment.yaml
kubectl apply -f nginx/service.yaml

echo "==> Waiting for rollouts"
kubectl rollout status statefulset/postgres -n "$NS" --timeout=180s
kubectl rollout status deployment/backend  -n "$NS" --timeout=180s
kubectl rollout status deployment/nginx    -n "$NS" --timeout=180s

echo "==> Done. Smoke test:"
curl -sS -m 5 http://localhost:30000/health && echo
echo "All good. Try: curl http://localhost:30000/"
