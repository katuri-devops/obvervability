# Project rules

Your AI assistant reads this file at the start of every step and follows it. The rules keep every student's system the same wherever it matters, so that the exercises later in the course work on every machine. Everything these rules do not fix is up to you.

This file grows. Later guides add rules when they introduce something new.

## 1. How we work

1. Work only on the step you are given. When it is done, say which commands to run and what I should see. Then stop and wait.
2. Explain what you did in simple words. I am new to this.
3. Keep the code small and simple. Having several services matters more than clever code.
4. The files in `contracts/` decide what every service accepts and returns. Follow them exactly.
5. If something in a step is unclear, ask me instead of guessing.

## 2. Files you must not change

- `postgres/compose.yaml`, `redis/compose.yaml` and `rabbitmq/compose.yaml`
- the `traefik` service in `app/compose.yaml`. You add services to that file, but leave `traefik` as it is.
- everything in `contracts/` and `database/`
- `.gitignore`
- this file

## 3. No monitoring code

Do not add OpenTelemetry, tracing, metrics endpoints or any vendor agent. The course adds these later, step by step. Plain logs, as described in section 12, are fine.

## 4. The services

| Service | Folder | Copies | Database schema |
|---|---|---|---|
| frontend | `app/frontend` | 2 | none |
| claims-api | `app/claims-api` | 2 | `claim` |
| customer-service | `app/customer-service` | 2 | `customer` |
| policy-service | `app/policy-service` | 2 | `policy` |
| payment-service | `app/payment-service` | 2 | `payment` |
| notification-service | `app/notification-service` | 2 | `notification` |

The names are fixed. They appear in URLs, container names and logs, and later in every trace. Each service is built in its own guide.

## 5. Tools and versions

Python 3.12, on the base image `python:3.12-slim`. Each service's `requirements.txt` lists only the libraries that service needs, with exactly these versions:

```
fastapi==0.143.2
uvicorn==0.54.0
httpx==0.28.1
sqlalchemy==2.0.54
psycopg[binary]==3.3.6
redis==8.1.0
aio-pika==10.1.1
jinja2==3.1.6
pydantic==2.14.1
python-multipart==0.0.32
```

SQLAlchemy stays on 2.0: the OpenTelemetry instrumentation used later in the course does not support 2.1.

Use these libraries for these jobs, and no others:

| Job | Library |
|---|---|
| HTTP server | FastAPI, run by Uvicorn |
| Calling another service | httpx, with `AsyncClient` |
| Database | SQLAlchemy 2.0 with its async engine, on psycopg |
| Redis | redis-py, with `redis.asyncio` |
| Web pages | Jinja2 |

A later guide adds the rule for RabbitMQ.

Not `requests`, `aiohttp`, `asyncpg` or `psycopg2`. Tracing tools support each library separately, so a different library means missing data later.

## 6. Inside a service

- Everything is async: `async def` endpoints, `create_async_engine` for the database, `redis.asyncio` for Redis, and one `httpx.AsyncClient` per service, created at start-up and reused.
- One Uvicorn worker per container, started with `uvicorn main:app --host 0.0.0.0 --port 8000`.
- Leave SQLAlchemy's pool settings and httpx's connection limits at their defaults.
- Calculate money with Python's `Decimal`, round it to two decimals, and send it as a JSON number.
- Times are in UTC.
- Do not crash when PostgreSQL, Redis or RabbitMQ is not ready yet. Try again every 2 seconds.

## 7. Containers

Each service has a folder `app/<service-name>/` with at least a `Dockerfile`, `requirements.txt` and `main.py`.

The build context is the `app` folder, not the service's own folder, so that shared code added in a later guide can be used by every service. Paths in a `Dockerfile` therefore start from `app/`, for example `COPY customer-service/requirements.txt .`

A service's entry in `app/compose.yaml` looks like this, shown for customer-service:

```yaml
  customer-service:
    build:
      context: .
      dockerfile: customer-service/Dockerfile
    restart: unless-stopped
    environment:
      SERVICE_NAME: customer-service
      DATABASE_URL: postgresql+psycopg://claims:claims-dev-password@postgres:5432/claims?application_name=customer-service
    deploy:
      replicas: 2
      resources:
        limits:
          cpus: "0.50"
          memory: 256M
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=2)"]
      interval: 5s
      timeout: 3s
      retries: 12
    labels:
      - traefik.enable=true
      - traefik.http.routers.customer-service.rule=PathPrefix(`/customer-service`)
      - traefik.http.routers.customer-service.entrypoints=web,internal
      - traefik.http.routers.customer-service.middlewares=customer-service-strip
      - traefik.http.middlewares.customer-service-strip.stripprefix.prefixes=/customer-service
      - traefik.http.services.customer-service.loadbalancer.server.port=8000
      - traefik.http.services.customer-service.loadbalancer.healthcheck.path=/health
      - traefik.http.services.customer-service.loadbalancer.healthcheck.interval=5s
    networks:
      - claims-net
```

- No `ports:` and no `container_name:`. Only Traefik publishes ports.
- Every service runs as two copies (`replicas: 2`), limited to half a CPU and 256 MB of memory each.

## 8. Environment variables

| Variable | Value | Used by |
|---|---|---|
| `SERVICE_NAME` | The service's name from section 4 | Every service |
| `DATABASE_URL` | `postgresql+psycopg://claims:claims-dev-password@postgres:5432/claims?application_name=<service-name>` | Services with a database schema |

`application_name` makes the database show which service runs each query. Later guides add more variables.

## 9. Routing through Traefik

A request to `/<service-name>/...` goes to that service, with the prefix removed: `/customer-service/customers/C-1001` reaches customer-service as `/customers/C-1001`. Use exactly the labels shown in section 7, with the service's name in place of `customer-service`.

## 10. Health

`GET /health` answers `{"service": SERVICE_NAME, "instance": <the container's host name>, "status": "ok"}`.

It never checks the database, Redis or RabbitMQ. A copy whose database is down must still answer, so that a failure shows where it happens, instead of making the whole service disappear from Traefik.

## 11. Database

- A service uses only its own schema. It gets other data by calling the service that owns it.
- The tables come from `database/schema.sql`. Never create, change or drop tables from code: no `create_all`, no migrations.
- The database creates the IDs: `claim_id`, `payment_id` and `notification_id`. Do not make them in Python.

## 12. Logging

- Log through Python's `logging` module. Never use `print()`.
- Add the IDs a log line is about with `extra`, using these names: `claim_id`, `customer_id`, `policy_id`, `payment_id`.
- Levels: INFO for normal events, such as a claim decided; WARNING when the service carries on in a reduced way, such as without Redis; ERROR when something failed.
- Never log passwords or whole request bodies.
- The names `trace_id` and `span_id` are reserved for later in the course.

A later guide sets the exact log format, with shared code.

## 13. Addresses on your computer

| Address | What |
|---|---|
| http://localhost:8080 | Every service, under `/<service-name>/`, and later the claims web pages |
| http://localhost:8088 | Traefik dashboard |
| http://localhost:15672 | RabbitMQ console. User `claims`, password `claims-dev-password`. |
| localhost:15432 | PostgreSQL. User `claims`, password `claims-dev-password`, database `claims`. |
| localhost:16379 | Redis |
 