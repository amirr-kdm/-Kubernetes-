# Kubernetes Migration — FastAPI + PostgreSQL + Nginx

Migration of the Docker Compose app in `last project/` onto a local Kind cluster
(`devops-cluster`), namespace `user-system`.

## Architecture / who talks to whom

```
Internet / host
      │  localhost:30000  (Kind port mapping 30000 -> hostPort 30000)
      ▼
Service "nginx"      (NodePort 30000 -> 80)
      │  reverse proxy, upstream "backend:8000"
      ▼
Service "backend"    (ClusterIP, 8000)
      │  DB_HOST=postgres, psycopg2 connection
      ▼
Service "postgres"   (headless ClusterIP: None, 5432)
      │
      ▼
StatefulSet "postgres" pod  (PVC-backed storage)
```

- Only `nginx` is reachable from outside the cluster (NodePort). `backend` and `postgres`
  are `ClusterIP`, so nothing outside the cluster (and nothing inside except through nginx)
  can hit the backend directly by design.
- `backend` finds the database via the Service DNS name `postgres` (headless Service,
  `clusterIP: None`, backed by the `postgres` StatefulSet). This replaces the Compose
  value `DB_HOST=db` with `DB_HOST=postgres`.
- `nginx` finds the backend via the Service DNS name `backend` (ClusterIP Service in
  front of the 2-replica backend Deployment).
- Credentials (`POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`) live in one Secret
  (`postgres-secret`) in `k8s/db/secret.yaml`, consumed by both the StatefulSet and the
  backend Deployment via `envFrom.secretRef`.
- Non-secret backend config (`DB_HOST`, `BACKEND_PORT`) lives in ConfigMap
  `backend-config`.
- The app has no JWT / auth of its own — only DB credentials, as in the original app.

## Steps performed

0. **Backend image for Kind.** Kind's containerd cannot see the host's local Docker
   images, so any locally-built image needs to be built then `kind load`-ed into the
   node, or the pod ends up in `ImagePullBackOff`.
   - The Dockerfile already existed in `last project/app/Dockerfile` (python:3.12-slim
     base, installs `requirements.txt`, runs `uvicorn`). It was copied unchanged into
     `k8s/app/`, along with `main.py` and `requirements.txt`.
   - Built as `backend:local` and loaded into `devops-cluster` with
     `kind load docker-image`. The Deployment uses `image: backend:local` +
     `imagePullPolicy: IfNotPresent`.

1. **Database** (`k8s/db/`):
   - `secret.yaml` — `postgres-secret` Secret (`admin` / `mysecretpassword` /
     `project_db`, same values as the Compose `.env`). Also (idempotently) creates the
     `user-system` namespace if missing.
   - `service.yaml` — headless Service `postgres` (`clusterIP: None`) so the
     StatefulSet pod gets a stable DNS identity.
   - `statefulset.yaml` — 1 replica, `postgres:15-alpine`, `volumeClaimTemplates` for a
     dedicated PVC. `PGDATA` is set to `/var/lib/postgresql/data/pgdata` (a *subdirectory*
     of the mount) — mounting a PVC directly onto Postgres's data dir without this causes
     Postgres to refuse to start with `initdb: directory ".../data" exists but is not
     empty`, because the PVC root has a `lost+found` folder from the filesystem.
     `pg_isready` is used for both probes; requests/limits are 100m/128Mi requests and
     500m/512Mi limits.

2. **Backend** (`k8s/backend/`):
   - `configmap.yaml` — `DB_HOST=postgres`, `BACKEND_PORT=8000`.
   - `deployment.yaml` — 2 replicas. An `initContainer` (`postgres:15-alpine` image, so
     `pg_isready` is available) loops `pg_isready -h postgres -p 5432` until the database
     answers, so the app container never starts against a database that isn't ready yet
     (Compose's `depends_on: condition: service_healthy` has no direct equivalent in
     Kubernetes). `readinessProbe`/`livenessProbe` hit `GET /health`. Also carries a
     `preferredDuringScheduling` pod anti-affinity on `app: backend` so replicas prefer
     separate nodes (a no-op on this single-node Kind cluster, but correct for a
     multi-node cluster).
   - `service.yaml` — `ClusterIP` Service `backend` on port 8000.
   - `hpa.yaml` — HorizontalPodAutoscaler, 2–5 replicas, target 70% CPU (see below).
   - `pdb.yaml` — PodDisruptionBudget, `minAvailable: 1`, as a second reliability
     mechanism alongside the HPA.

3. **Nginx** (`k8s/nginx/`):
   - `configmap.yaml` — `nginx.conf`, adapted from `last project/nginx/nginx.conf`:
     `proxy_pass` upstream changed from `http://web:8000` to `http://backend:8000`
     (the Kubernetes Service name), and a `/health` location was added (nginx's own
     health endpoint for its liveness/readiness probes — the original conf had none).
   - `deployment.yaml` — mounts the ConfigMap as `/etc/nginx/nginx.conf` via `subPath`.
   - `service.yaml` — `NodePort`, `nodePort: 30000`, matching the Kind cluster's
     `30000 -> hostPort 30000` port mapping, so the app is reachable at
     `http://localhost:30000`.

4. **Reliability — HPA (primary) + PDB + anti-affinity (bonus).**
   `metrics-server` was not installed on the cluster; it was installed via the upstream
   manifest and patched with `--kubelet-insecure-tls` (required for Kind's self-signed
   kubelet certs — without it `kubectl top` / HPA calls fail with x509 errors). The HPA
   (`backend-hpa`) scales the backend Deployment 2→5 replicas on 70% average CPU
   utilization.

## Problems encountered and how they were resolved

These were all pre-existing environment issues on the long-running Kind cluster, not
caused by the manifests themselves — listed here since fixing them was required to get
pods running at all:

- **`ImagePullBackOff` / `ErrImagePull` on `postgres:15-alpine` and `nginx:alpine`.**
  The Kind node container had a broken `HTTP_PROXY=http://127.0.0.1:10808` environment
  variable pointing at a proxy that only exists in the *host's* network namespace, not
  the node container's. Fix: pulled the images with the host's `docker pull` (which
  uses the host's own proxy config successfully) and imported them into the node
  directly with `docker save <image> | docker exec -i devops-cluster-control-plane ctr
  --namespace=k8s.io images import -` (plain `kind load docker-image` failed on these
  images with a `content digest ... not found` error caused by multi-platform manifest
  lists / attestation layers).
- **`local-path-provisioner` pod stuck in `CreateContainerError`**, which blocked the
  Postgres PVC from ever binding (`WaitForFirstConsumer` storage class). Root cause was
  a stale containerd container-name reservation ("failed to reserve container name...
  is reserved for ..."). Fix: `kubectl delete pod` on it so its Deployment recreated it
  with a fresh name.
- **CoreDNS pods `0/1 Not Ready` with repeated `Unauthorized` errors watching
  Namespaces/Services/EndpointSlices**, which in turn made nginx crash-loop with
  `host not found in upstream "backend"` (no working cluster DNS). This turned out to
  be a symptom of a deeper control-plane clock/token issue (see next point), not a
  CoreDNS-specific problem.
- **`kube-apiserver` crash-looping with `invalid bearer token, service account token is
  not valid yet`**, affecting every in-cluster client (CoreDNS, kube-proxy, etc.) — a
  clock-skew symptom, most likely from the long-lived Kind node container having been
  paused/suspended (VM sleep) at some point during its 9-day uptime. Fix: `docker
  restart devops-cluster-control-plane` to force a clean clock resync, **without**
  deleting or recreating the Kind cluster (etcd data on disk was preserved). After the
  restart, `kubectl get pods` briefly showed stale `CreateContainerError` states for the
  static control-plane pods (kubelet status cache lag) even though `crictl ps` showed
  them already `Running` — this cleared itself within under a minute.

None of these required touching `./last project/` or recreating the cluster/namespace.

## Build → load → apply, in order

> **TL;DR:** just run `./deploy.sh` — it does everything below idempotently
> (build, load, image-import fallback, metrics-server, apply, wait, smoke-test).
> The manual steps are spelled out here for reference.

```bash
cd k8s

# 0. Build and load the backend image
docker build -t backend:local ./app
kind load docker-image backend:local --name devops-cluster

# 1. Database
kubectl apply -f db/secret.yaml
kubectl apply -f db/service.yaml
kubectl apply -f db/statefulset.yaml

# 2. Backend
kubectl apply -f backend/configmap.yaml
kubectl apply -f backend/deployment.yaml
kubectl apply -f backend/service.yaml
kubectl apply -f backend/hpa.yaml     # requires metrics-server, see below
kubectl apply -f backend/pdb.yaml

# 3. Nginx
kubectl apply -f nginx/configmap.yaml
kubectl apply -f nginx/deployment.yaml
kubectl apply -f nginx/service.yaml

# 4. metrics-server (only if not already installed on the cluster)
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
kubectl patch deployment metrics-server -n kube-system --type='json' \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
```

Watch rollout:

```bash
kubectl get pods -n user-system -w
```

## Testing each service

```bash
# Everything, through nginx (the only externally reachable entry point)
curl http://localhost:30000/health
curl http://localhost:30000/

curl -X POST http://localhost:30000/items/ -H "Content-Type: application/json" -d '{"name":"apple"}'
curl http://localhost:30000/items/
curl "http://localhost:30000/items/search?name=app"
curl -X DELETE http://localhost:30000/items/1

# Backend, from inside the cluster only (it has no NodePort)
kubectl run -n user-system curltest --image=curlimages/curl --restart=Never --rm -it -- \
  curl http://backend:8000/health

# Database, from inside the cluster only
kubectl exec -n user-system postgres-0 -- pg_isready -U admin -d project_db

# HPA / PDB status
kubectl get hpa backend-hpa -n user-system
kubectl get pdb backend-pdb -n user-system
```

## Assumptions made

- `POSTGRES_USER=admin`, `POSTGRES_PASSWORD=mysecretpassword`, `POSTGRES_DB=project_db`
  were taken verbatim from `last project/.env` / `docker-compose.yml`.
- The app source (`main.py`, `requirements.txt`, `Dockerfile`) was copied byte-for-byte
  into `k8s/app/` rather than referencing `../last project/app` directly from the
  Dockerfile build context, so `k8s/` is self-contained and buildable on its own.
- Postgres StatefulSet PVC size (`1Gi`) and backend/nginx `resources` values were not
  specified in the task beyond "reasonable" — chosen to be generous enough for this
  small demo app while still bounded.
- The nginx `/health` endpoint is nginx's own static response (`return 200 "healthy"`),
  not a proxy to the backend's `/health` — this is what nginx's own liveness/readiness
  probes hit, kept separate from the backend health check reachable via `GET
  /health` → proxied to the backend.
- Pod anti-affinity is `preferred` (soft), not `required` (hard), since this is a
  single-node Kind cluster — a hard requirement would leave the second backend replica
  permanently unschedulable.

## Future work (not implemented — see task instructions)

- **Redis + NetworkPolicy**: add a Redis Deployment/Service for caching, and
  `NetworkPolicy` resources restricting ingress so only `nginx` can reach `backend` on
  8000, and only `backend` can reach `postgres` on 5432 / `redis` on 6379.
- **Ingress + security annotations**: replace the nginx NodePort with an actual
  `Ingress` resource (needs an ingress controller such as `ingress-nginx` installed on
  the Kind cluster), fronted with TLS and security-header annotations
  (`nginx.ingress.kubernetes.io/*`).
- **Prometheus + Grafana via Helm**: `helm install` the `kube-prometheus-stack` chart
  into a `monitoring` namespace, and wire up a `ServiceMonitor` for the backend once it
  exposes `/metrics`.
