# Claims application: observability overview

This guide explains the application and its current OpenTelemetry setup. It is
based on the repository configuration and source code, not a live-system test.
See the [architecture diagrams](./architecture-diagram.md) alongside this guide.

## 1. The application

The application is a small insurance claims system made of separate services.
A customer submits a claim through a web page. The system checks the customer
and policy, decides the claim, and may request a payment. It also tries to send
an event so that a notification can be recorded.

| Component | Responsibility |
| --- | --- |
| Traefik | Receives and routes HTTP requests to services. |
| Frontend | Displays the claims pages and calls the claims API. |
| Claims API | Coordinates the claim decision, payment, and decision event. |
| Customer service | Provides customer information. |
| Policy service | Checks policy coverage. |
| Payment service | Handles payment requests and calls the bank simulator. |
| Bank API | Simulates a bank; it does not make real transfers. |
| Notification service | Consumes decision events and records notifications in PostgreSQL. |
| PostgreSQL | Stores application data in service-specific schemas. |
| Redis | Caches customer and policy data and helps with payment locking. |
| RabbitMQ | Carries claim decision events asynchronously. |
| OpenTelemetry Collector | Receives trace data and currently writes it to its container logs. |

These components use a shared external Docker network named `claims-net`.
Traefik exposes the application on port `8080`. Most application services are
configured with two replicas; the bank simulator has no explicit replica count.

## 2. A claim's journey

1. A customer submits a claim to the frontend through Traefik.
2. The frontend calls the claims API through Traefik's internal entry point.
3. Claims API calls the customer and policy services through Traefik, then
   saves its decision in PostgreSQL.
4. If the claim is approved, claims API calls the payment service through
   Traefik. Payment service calls the bank simulator directly.
5. Claims API attempts to publish a `claim.decided` event to RabbitMQ.
6. Notification service consumes the event, looks up the customer through
   Traefik, and records a notification in PostgreSQL.

Not every claim follows every step: rejected or under-review claims do not take
the approved-payment path. If RabbitMQ is unavailable, the claim can still be
returned, but the decision event may not be sent.

## 3. What is observability?

Observability helps you understand what is happening inside a system:

- **Traces** describe the journey of an operation across services.
- **Metrics** are numerical measurements, such as request counts or latency.
- **Logs** are messages describing events or errors.

A **trace** contains one or more **spans**. A span describes an operation,
such as an HTTP request, and can include its duration and attributes. Related
spans share a trace ID; parent and child relationships show how operations fit
together. A **resource** describes the entity producing telemetry, such as a
service name and deployment environment.

For example, a trace of a claim request can help you see which service or
operation took time. The operations that appear depend on the instrumentations
installed and supported at runtime.

## 4. How auto-instrumentation works

Each application service lists `splunk-opentelemetry==2.13.3` in its
`requirements.txt`. Each service's Dockerfile:

1. Installs the requirements.
2. Runs `opentelemetry-bootstrap -a install` to install instrumentation for
   detected libraries.
3. Starts Uvicorn through `opentelemetry-instrument`.

This can capture supported operations without adding tracing calls throughout
the application code. It does not guarantee that every library or operation is
instrumented. In particular, verify RabbitMQ trace propagation in actual output
rather than assuming the notification is connected to the original HTTP trace.

Traefik has separate built-in tracing configuration. Its configured sample rate
is `1.0`.

## 5. Sending telemetry to the collector

The shared application configuration sets:

| Setting | Meaning |
| --- | --- |
| `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317` | Sends OTLP telemetry to the collector on the Docker network. |
| `OTEL_EXPORTER_OTLP_PROTOCOL=grpc` | Uses OTLP over gRPC. |
| `OTEL_SERVICE_NAME` | Identifies each Python service separately. |
| `OTEL_RESOURCE_ATTRIBUTES` | Adds `deployment.environment=claims-lab` and `service.version=1.0.0`. |
| `OTEL_PYTHON_EXCLUDED_URLS=health` | Excludes matching health URLs from Python instrumentation. |
| `OTEL_PYTHON_LOG_CORRELATION=true` | Enables Python log correlation; the log format also determines whether trace IDs appear in a line. |
| `OTEL_LOGS_EXPORTER=none` | Disables OTLP log export from the Python services. |

The collector listens on port `4317` for OTLP/gRPC and `4318` for OTLP/HTTP.
These are container-network endpoints; the collector Compose file does not
publish them to the host. Traefik and the Python services are configured to use
gRPC on port `4317`.

The collector's trace pipeline is:

```text
OTLP receiver -> memory limiter -> batch -> resource detection
              -> deployment environment attribute -> debug exporter
```

The **receiver** accepts data, **processors** handle it, and the **exporter**
determines where it goes. The debug exporter writes detailed trace data to the
collector's container logs.

## 6. What is and is not available today

- The collector has a **trace pipeline only**; no metrics or logs pipelines are
  configured.
- The debug exporter prints traces. No trace-storage backend or trace UI is
  configured here, so this is not long-term storage.
- Python application logs are not exported through OTLP by this configuration.
- Detailed debug output can contain request or span attributes. Treat collector
  logs as potentially sensitive.
- The collector image uses `latest`, so the version can change when pulled again.

## 7. See trace output

With the application and collector already running, run these commands from the
repository root:

```bash
# Generate a request.
curl http://localhost:8080/

# Inspect collector output.
docker compose -f otel/compose.yaml logs --tail=100 otel-collector
```

Look for resource attributes such as `service.name`, followed by trace and span
details. The collector batches data, so output may not appear immediately.
Health requests excluded by the Python configuration are not a good test. If
no spans appear, check that the containers are running, share `claims-net`, and
can reach the collector.

## 8. Where to look in the repository

- [Application Compose configuration](../../app/compose.yaml): services, routing,
  environment variables, and Traefik tracing.
- [Claims API Dockerfile](../../app/claims-api/Dockerfile) and
  [requirements](../../app/claims-api/requirements.txt): an example of Python
  auto-instrumentation. The other application services follow the same pattern.
- [Collector Compose configuration](../../otel/compose.yaml): collector container
  and shared network.
- [Collector pipeline configuration](../../otel/config.yaml): receivers,
  processors, exporter, and trace pipeline.
