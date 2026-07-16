# مهاجرت اپلیکیشن میکروسرویس به Kubernetes

مهاجرت اپلیکیشن Docker Compose موجود در پوشه `last project/` به کلاستر محلی Kind با نام `devops-cluster`، در namespace برابر `user-system`.

---

## معماری سیستم — چه کسی به چه کسی وصل می‌شه

```
اینترنت / هاست
      │  localhost:30000  (Kind port mapping: 30000 → hostPort 30000)
      ▼
Service "nginx"      (NodePort 30000 → 80)
      │  reverse proxy، upstream به "backend:8000"
      ▼
Service "backend"    (ClusterIP، پورت 8000)
      │  DB_HOST=postgres، اتصال از طریق psycopg2
      ▼
Service "postgres"   (Headless ClusterIP: None، پورت 5432)
      │
      ▼
StatefulSet "postgres" (ذخیره‌سازی روی PVC)
```

- فقط `nginx` از بیرون کلاستر قابل دسترسه (NodePort). سرویس‌های `backend` و `postgres` از نوع ClusterIP هستن و از خارج کلاستر قابل دسترس نیستن.
- بک‌اند از طریق DNS داخلی Kubernetes به نام `postgres` به دیتابیس وصل می‌شه (جایگزین `DB_HOST=db` در Compose شده به `DB_HOST=postgres`).
- nginx از طریق نام سرویس `backend` به بک‌اند دسترسی داره.
- credentials دیتابیس (`POSTGRES_USER`، `POSTGRES_PASSWORD`، `POSTGRES_DB`) داخل یه Secret به نام `postgres-secret` نگهداری می‌شن و هم StatefulSet و هم Deployment بک‌اند ازشون استفاده می‌کنن.

---

## ساختار فایل‌ها

```
k8s/
├── db/
│   ├── secret.yaml          ← اعتبارنامه‌های دیتابیس
│   ├── service.yaml         ← سرویس headless برای postgres
│   └── statefulset.yaml     ← دیتابیس PostgreSQL با PVC
├── backend/
│   ├── configmap.yaml       ← تنظیمات غیرحساس (DB_HOST و ...)
│   ├── deployment.yaml      ← اپ FastAPI با ۲ replica
│   ├── service.yaml         ← سرویس ClusterIP
│   ├── hpa.yaml             ← HorizontalPodAutoscaler
│   └── pdb.yaml             ← PodDisruptionBudget
├── nginx/
│   ├── configmap.yaml       ← فایل nginx.conf
│   ├── deployment.yaml      ← پراکسی معکوس Nginx
│   └── service.yaml         ← سرویس NodePort روی پورت 30000
├── app/
│   ├── Dockerfile
│   ├── main.py
│   └── requirements.txt
├── screenshots/             ← اسکرین‌شات‌های تأیید اجرا
├── deploy.sh                ← اسکریپت کامل build → load → apply
└── README.md
```

---

## مرحله‌به‌مرحله پیاده‌سازی

### مرحله ۰ — آماده‌سازی ایمیج بک‌اند برای Kind

Kind به imageهای محلی Docker دسترسی مستقیم نداره. ایمیج باید ساخته بشه و داخل نود کلاستر load بشه، وگرنه `ImagePullBackOff` می‌گیریم.

```bash
cd k8s
docker build -t backend:local ./app
kind load docker-image backend:local --name devops-cluster
```

> در Deployment از `image: backend:local` با `imagePullPolicy: IfNotPresent` استفاده شده.

---

### مرحله ۱ — دیتابیس (`k8s/db/`)

- **`secret.yaml`** — Secret با نام `postgres-secret` شامل مقادیر `admin`، `mysecretpassword`، `project_db` (همان مقادیر فایل `.env` پروژه قبلی). همچنین namespace `user-system` رو به‌صورت idempotent می‌سازه.
- **`service.yaml`** — سرویس headless (`clusterIP: None`) با نام `postgres` تا StatefulSet یک DNS پایدار داشته باشه.
- **`statefulset.yaml`** — یک replica با ایمیج `postgres:15-alpine` و `volumeClaimTemplates` برای PVC اختصاصی.

**نکته مهم:** متغیر `PGDATA` روی `/var/lib/postgresql/data/pgdata` (یه **زیرپوشه**) تنظیم شده. بدون این، Postgres با خطای `directory not empty` بالا نمیاد چون ریشه PVC فولدر `lost+found` داره.

**Probeها:** هر دو probe با دستور `pg_isready -U $POSTGRES_USER -d $POSTGRES_DB` پیاده شدن.

**Resources:** requests: 100m/128Mi — limits: 500m/512Mi

---

### مرحله ۲ — بک‌اند (`k8s/backend/`)

- **`configmap.yaml`** — مقادیر غیرحساس: `DB_HOST=postgres` و `BACKEND_PORT=8000`.
- **`deployment.yaml`** — ۲ replica. شامل یک `initContainer` که قبل از اجرای اپ، با `pg_isready -h postgres` صبر می‌کنه تا دیتابیس آماده بشه (معادل Kubernetes برای `depends_on: service_healthy` در Compose). Probeها روی مسیر `/health` پیکربندی شدن. همچنین `podAntiAffinity` از نوع `preferred` اضافه شده تا replicaها ترجیحاً روی nodeهای مختلف پخش بشن.
- **`service.yaml`** — سرویس ClusterIP با نام `backend` روی پورت 8000.
- **`hpa.yaml`** — HPA با ۲ تا ۵ replica، target: 70% CPU (نیاز به metrics-server دارد — ببینید مشکلات).
- **`pdb.yaml`** — PodDisruptionBudget با `minAvailable: 1`.

---

### مرحله ۳ — Nginx (`k8s/nginx/`)

- **`configmap.yaml`** — فایل `nginx.conf` که از پروژه قبلی برای Kubernetes تطبیق داده شده: `proxy_pass` از `http://web:8000` به `http://backend:8000` تغییر کرده. یه endpoint مستقل `/health` هم برای probe‌های خود nginx اضافه شده.
- **`deployment.yaml`** — ConfigMap رو روی `/etc/nginx/nginx.conf` از طریق `subPath` mount می‌کنه.
- **`service.yaml`** — NodePort روی پورت `30000`، منطبق با port mapping کلاستر Kind.

---

### مرحله ۴ — قابلیت اطمینان

سه مکانیزم reliability پیاده شدن:

| مکانیزم | فایل | توضیح |
|---|---|---|
| **HPA** (اصلی) | `backend/hpa.yaml` | مقیاس‌بندی خودکار ۲ تا ۵ replica بر اساس ۷۰٪ CPU |
| **PDB** (اضافی) | `backend/pdb.yaml` | همیشه حداقل ۱ replica در دسترس باشه |
| **Anti-Affinity** (اضافی) | `backend/deployment.yaml` | replicaها ترجیحاً روی nodeهای مختلف |

برای HPA، `metrics-server` روی کلاستر نصب و با flag `--kubelet-insecure-tls` پیکربندی شد (برای سازگاری با گواهی‌نامه‌های self-signed Kind).

---

## تأیید اجرا (اسکرین‌شات‌ها)

### نود کلاستر و اطلاعات کنترل پلین

![cluster nodes](k8s/screenshots/01-cluster-nodes.jpg)

---

### Namespace مربوطه (`user-system`)

![namespace](k8s/screenshots/02-namespace.jpg)

---

### تمام پادها در حال اجرا — `1/1 Running`

![pods running](k8s/screenshots/03-pods-running.jpg)

---

### نمای کامل تمام منابع namespace

![get all](k8s/screenshots/04-get-all.jpg)

---

### Secret و ConfigMapها

![secret configmap](k8s/screenshots/05-secret-configmap.jpg)

---

### PVC بایند شده و تأیید اتصال دیتابیس

![pvc db ready](k8s/screenshots/06-pvc-db-ready.jpg)

---

### تست سرویس بک‌اند از داخل کلاستر (بدون image خارجی)

![backend internal](k8s/screenshots/07-backend-internal.jpg)

---

### تست کامل API از بیرون — از طریق nginx (NodePort 30000)

![e2e test 1](k8s/screenshots/08-app-e2e-test-1.jpg)

![e2e test 2](k8s/screenshots/08-app-e2e-test-2.jpg)

---

### HPA و PDB

![hpa pdb](k8s/screenshots/09-hpa-pdb.jpg)

---

### لاگ‌های InitContainer — صبر برای آماده شدن دیتابیس

![init container logs](k8s/screenshots/10-init-container-logs.jpg)

---

### تست ماندگاری دیتا — حذف پاد postgres و بازیابی خودکار

![data persistence](k8s/screenshots/11-data-persistence.jpg)

---

## مشکلات برخورد شده و راه‌حل‌ها

همه مشکلات زیر مربوط به محیط **پیش‌موجود کلاستر** بودن، نه manifest‌ها.

---

### ۱. خطای `ImagePullBackOff` روی پادهای تستی موقت

**مشکل:** در حین بررسی و تست، تلاش برای بالا آوردن یک پاد موقت از image عمومی `curlimages/curl` جهت تست Service بک‌اند از داخل کلاستر، با خطای `ImagePullBackOff` مواجه شد. دلیل: این image روی نود Kind از قبل موجود نبود و در آن لحظه پراکسی محلی (`127.0.0.1:10808`) هم در دسترس نود نبود.

**راه‌حل:** به‌جای دانلود یک image جدید، از دستور `kubectl exec` روی یکی از پادهای `backend` که از قبل در حال اجرا بود استفاده شد (ایمیج backend شامل curl هست). از این طریق تأیید شد که `GET /health` از داخل کلاستر مقدار `{"status":"healthy"}` را برمی‌گرداند.

```bash
kubectl exec -n user-system backend-788fffb76b-92sm5 -c backend -- \
  curl -s http://backend:8000/health
```

---

### ۲. خطای `ImagePullBackOff` روی imageهای عمومی (`postgres` و `nginx`)

**مشکل:** نود Kind یک متغیر محیطی `HTTP_PROXY=http://127.0.0.1:10808` داشت که فقط در network namespace هاست معتبره، نه داخل container نود. بنابراین هر pull از Docker Hub با خطا مواجه می‌شد.

**راه‌حل:** imageها روی هاست pull شدن (که پراکسی درست داره) و سپس مستقیماً داخل containerd نود import شدن:

```bash
docker pull postgres:15-alpine
docker save postgres:15-alpine | docker exec -i devops-cluster-control-plane \
  ctr --namespace=k8s.io images import -
```

> `kind load docker-image` برای این imageها به‌دلیل manifest list چند پلتفرمی کار نکرد و به روش `ctr import` برگشتیم.

---

### ۳. `local-path-provisioner` گیرکرده — PVC bind نمی‌شد

**مشکل:** پاد `local-path-provisioner` در وضعیت `CreateContainerError` گیر کرده بود (collision روی نام container در containerd) و در نتیجه PVC پستگرس هرگز bind نمی‌شد و postgres-0 در وضعیت `Pending` می‌موند.

**راه‌حل:** حذف پاد تا Deployment آن را با نام تازه بازسازی کنه:

```bash
kubectl delete pod -n local-path-storage -l app=local-path-provisioner --force
```

---

### ۴. Crash کنترل پلین به‌دلیل clock-skew — DNS کلاستر کاملاً از کار افتاد

**مشکل:** API server با خطای `service account token is not valid yet` crash می‌کرد. این خطا باعث شد همه کلایت‌های in-cluster (CoreDNS، kube-proxy و ...) با `Unauthorized` مواجه بشن. در نتیجه DNS داخلی کلاستر از کار افتاد و nginx با خطای `host not found in upstream "backend"` crash-loop می‌زد.

**علت احتمالی:** نود Kind بیش از ۹ روز قبل راه‌اندازی شده بود و احتمالاً در این مدت به‌دلیل sleep یا suspend شدن سیستم، ساعت داخلیش drift پیدا کرده بود.

**راه‌حل:** restart کانتینر نود Kind **بدون** حذف یا بازسازی کلاستر (داده‌های etcd روی دیسک حفظ شدن):

```bash
docker restart devops-cluster-control-plane
```

---

## دستورات Build → Load → Apply

> **سریع‌ترین راه:** فقط اسکریپت `./deploy.sh` رو اجرا کن — همه مراحل زیر رو به‌صورت idempotent انجام می‌ده.

```bash
cd k8s

# ۰. ساخت و لود ایمیج بک‌اند
docker build -t backend:local ./app
kind load docker-image backend:local --name devops-cluster

# ایمیج‌های عمومی (در صورت نبود دسترسی مستقیم به رجیستری)
docker pull postgres:15-alpine
docker save postgres:15-alpine | docker exec -i devops-cluster-control-plane \
  ctr --namespace=k8s.io images import -
docker pull nginx:alpine
docker save nginx:alpine | docker exec -i devops-cluster-control-plane \
  ctr --namespace=k8s.io images import -

# ۱. دیتابیس
kubectl apply -f db/secret.yaml
kubectl apply -f db/service.yaml
kubectl apply -f db/statefulset.yaml

# ۲. بک‌اند
kubectl apply -f backend/configmap.yaml
kubectl apply -f backend/deployment.yaml
kubectl apply -f backend/service.yaml
kubectl apply -f backend/hpa.yaml
kubectl apply -f backend/pdb.yaml

# نصب metrics-server (اگر روی کلاستر نیست)
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
kubectl patch deployment metrics-server -n kube-system --type='json' \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'

# ۳. Nginx
kubectl apply -f nginx/configmap.yaml
kubectl apply -f nginx/deployment.yaml
kubectl apply -f nginx/service.yaml
```

پایش rollout:

```bash
kubectl get pods -n user-system -w
```

---

## نحوه تست هر سرویس

همه چیز از طریق nginx (تنها نقطه ورود خارجی) قابل دسترسه:

```bash
# تست سلامت
curl http://localhost:30000/health
curl http://localhost:30000/

# عملیات CRUD
curl -X POST http://localhost:30000/items/ \
  -H "Content-Type: application/json" -d '{"name":"apple"}'

curl http://localhost:30000/items/
curl "http://localhost:30000/items/search?name=app"
curl -X DELETE http://localhost:30000/items/1

# تست از داخل کلاستر (فقط از طریق exec)
kubectl exec -n user-system deploy/backend -c backend -- \
  curl -s http://backend:8000/health

# وضعیت دیتابیس
kubectl exec -n user-system postgres-0 -- pg_isready -U admin -d project_db

# وضعیت HPA و PDB
kubectl get hpa backend-hpa -n user-system
kubectl get pdb backend-pdb -n user-system
```

---

## فرضیات

- مقادیر `POSTGRES_USER=admin`، `POSTGRES_PASSWORD=mysecretpassword`، `POSTGRES_DB=project_db` مستقیماً از `last project/.env` گرفته شدن.
- سورس اپ (`main.py`، `requirements.txt`، `Dockerfile`) به پوشه `k8s/app/` کپی شده تا `k8s/` به‌تنهایی self-contained و قابل build باشه.
- حجم PVC برای postgres (`1Gi`) برای این اپ demo کافیه.
- Pod anti-affinity از نوع `preferred` (نه `required`) تعریف شده چون کلاستر Kind تک‌نود داره — اگه `required` بود، replica دوم هرگز schedule نمی‌شد.
- endpoint `/health` در nginx یک پاسخ استاتیک می‌ده (نه پروکسی به بک‌اند) تا probe خود nginx مستقل از وضعیت بک‌اند عمل کنه.

---

## کارهای آینده (پیاده نشده)

- **Redis + NetworkPolicy (مرحله ۵):** اضافه کردن Redis برای caching و تعریف `NetworkPolicy` برای محدود کردن دسترسی: فقط nginx بتونه به backend روی 8000 وصل بشه، فقط backend بتونه به postgres روی 5432 و redis روی 6379 وصل بشه.

- **Ingress + annotationهای امنیتی (مرحله ۶):** جایگزین کردن NodePort با یک `Ingress` resource (نیاز به نصب ingress-nginx controller روی Kind) با TLS و header‌های امنیتی.

- **Prometheus + Grafana با Helm (مرحله ۷):**
  ```bash
  helm install kube-prom-stack prometheus-community/kube-prometheus-stack \
    --namespace monitoring --create-namespace
  ```
  بعد اضافه کردن `ServiceMonitor` برای بک‌اند وقتی endpoint `/metrics` expose بشه.
