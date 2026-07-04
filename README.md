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
  (`postgres-secret`) in `db/secret.yaml`, consumed by both the StatefulSet and the
  backend Deployment via `envFrom.secretRef`.
- Non-secret backend config (`DB_HOST`, `BACKEND_PORT`) lives in ConfigMap
  `backend-config`.
- The app has no JWT / auth of its own — only DB credentials, as in the original app.
