# Claims observability lab

This repository is a small insurance claims application for learning how
services work together and how OpenTelemetry can help you observe them. It
uses Python/FastAPI services, Traefik, PostgreSQL, Redis, RabbitMQ and an
OpenTelemetry Collector, each running in Docker containers.

## Start here

1. Read the [OpenTelemetry beginner's guide](resources/docs/opentelemetry-beginners-guide.md)
   for a step-by-step explanation of the instrumentation and collector.
2. View the [architecture diagrams](resources/docs/architecture-diagram.md) to
   see the application and telemetry flows.
3. Use the [observability overview](resources/docs/observability-overview.md)
   as a shorter reference.

## How it works

A browser connects to the frontend through Traefik. When a customer submits a
claim, the claims API checks the customer and policy, saves the decision, and
may request payment through the payment service and a simulated bank API. It
also attempts to publish a decision event to RabbitMQ for the notification
service.

The Python containers use OpenTelemetry auto-instrumentation, while Traefik has
its own tracing configuration. They send telemetry to a separate collector on
the shared Docker network:

```text
Browser -> Traefik -> Frontend -> Claims and other services
              |                         |
              +-------- traces ---------+-> OpenTelemetry Collector
                                                |
                                                v
                                         Collector logs
```

The collector currently has a **trace pipeline only**. Its debug exporter
prints trace data to container logs; no trace-storage backend or trace UI is
configured. Python OTLP log export is disabled, and the collector has no
metrics or logs pipeline. Actual instrumentation and trace coverage depend on
the supported libraries and runtime behavior.

## Repository layout

| Path | Contents |
| --- | --- |
| [app/](app/) | Frontend, APIs and services, their Dockerfiles, and application Compose configuration. |
| [otel/](otel/) | Collector Compose configuration and telemetry pipeline. |
| [postgres/](postgres/), [redis/](redis/), [rabbitmq/](rabbitmq/) | Compose configurations for application dependencies. |
| [resources/db/](resources/db/) | Database schema and sample data. |
| [resources/docs/](resources/docs/) | Beginner's guide, overview and architecture diagrams. |
| [resources/api/](resources/api/), [resources/messages/](resources/messages/), [resources/ui/](resources/ui/) | Application contracts and related resources. |

## Run locally

You need Docker with Docker Compose and an available Docker daemon. Commands
below are run from the repository root. They are intended for a local lab,
not a production deployment.

1. Create the shared network if it does not already exist:

   ```bash
   docker network inspect claims-net
   # Only if the network does not exist:
   docker network create claims-net
   ```

2. The collector Compose file expects `otel/.env`. If you do not already have
   one, create an empty file. The current collector pipeline does not require
   values from it. Keep any credentials out of Git.

   ```bash
   test -f otel/.env || touch otel/.env
   ```

3. Start the dependencies and collector:

   ```bash
   docker compose -f postgres/compose.yaml up -d
   docker compose -f redis/compose.yaml up -d
   docker compose -f rabbitmq/compose.yaml up -d
   docker compose -f otel/compose.yaml up -d
   ```

4. Wait for PostgreSQL to report `healthy`:

   ```bash
   docker compose -f postgres/compose.yaml ps
   ```

   For a new lab database, create the schema and load the sample data:

   ```bash
   docker compose -f postgres/compose.yaml exec -T postgres \
     psql -v ON_ERROR_STOP=1 -U claims -d claims < resources/db/schema.sql
   docker compose -f postgres/compose.yaml exec -T postgres \
     psql -v ON_ERROR_STOP=1 -U claims -d claims < resources/db/seed.sql
   ```

   The seed script updates existing seeded customers and policies if run again;
   do not rerun it if you need to preserve changes to those records.

5. Build and start the application:

   ```bash
   docker compose -f app/compose.yaml up -d --build
   docker compose -f app/compose.yaml ps
   ```

Open [http://localhost:8080](http://localhost:8080) for the claims frontend.
The Traefik dashboard is at [http://localhost:8088](http://localhost:8088),
and the RabbitMQ console is at [http://localhost:15672](http://localhost:15672).
The Compose files contain local development settings, including credentials;
they are not production security settings.

## See traces

With the containers running, generate a request and inspect the collector:

```bash
curl http://localhost:8080/
docker compose -f otel/compose.yaml logs --tail=100 otel-collector
```

Look for service names, trace IDs and span details. Batching may delay output.
The homepage request does not submit a claim; use the frontend form to explore
the claim flow. See the [beginner's guide](resources/docs/opentelemetry-beginners-guide.md)
for an explanation and troubleshooting steps. Collector debug logs can contain
sensitive attributes, so take care when sharing them.
