# OpenTelemetry: a step-by-step beginner's guide

This guide explains how the files in this repository work together. Start with
the big picture, then follow the numbered steps in order. The examples describe
the configuration in the repository; they are not a recording of a live run.

## 1. The big picture

There are **two separate jobs**:

1. **The application creates telemetry.** Each Python service has
   auto-instrumentation installed and is configured to send telemetry to the
   collector. Traefik has its own tracing configuration.
2. **The collector receives and handles telemetry.** It runs as a separate
   container, processes incoming traces, and currently prints them to its logs.

Think of the services as people writing postcards and the collector as a
sorting office. The services need to know the sorting office's address. The
sorting office has its own instructions for what to do with the postcards
when they arrive.

```text
BUILD TIME: prepare each Python service image
requirements.txt -> pip install -> opentelemetry-bootstrap

CONTAINER START: run each service with telemetry enabled
app/compose.yaml environment -> opentelemetry-instrument -> Uvicorn / FastAPI

RUNTIME: handle a request and export traces separately
browser -> Traefik -> application services -> response
             |               |
             +---- traces ---+--> collector -> debug exporter -> collector logs

COLLECTOR SETUP: separate container and its own pipeline
otel/compose.yaml -> starts collector using otel/config.yaml
```

The browser does not send its claim to the collector. The claim is application
traffic; the traces are observations about operations that handle traffic.

## 2. A few important words

| Word | Meaning in this project |
| --- | --- |
| **Observability** | Using information about a system to understand what it is doing. |
| **Telemetry** | Information produced about a system, including traces, metrics and logs. |
| **Trace** | A journey made up of related operations, for example handling a claim request. |
| **Span** | One operation within a trace, such as a service handling an HTTP request. A span may include a duration and attributes. |
| **Trace ID** | An identifier shared by spans that belong to the same trace. |
| **Span ID** | An identifier for one span. A parent span ID describes its relationship to another span. |
| **Instrumentation** | Code that observes operations and produces telemetry. |
| **Auto-instrumentation** | Instrumentation added at startup around supported libraries, rather than writing each tracing call in application code. |
| **OTLP** | OpenTelemetry Protocol, a way to send telemetry between components. This setup uses OTLP over gRPC. |
| **Collector** | A separate process that receives and processes telemetry. It does not handle the business request itself. |
| **Exporter** | The part that sends telemetry somewhere: a Python exporter sends it to the collector; the collector's debug exporter writes it to logs. |
| **Resource attributes** | Information identifying where telemetry came from, such as `service.name`. |

Imagine a trace like this, where the times are invented:

```text
Trace ID: abc...                         Total request: 300 ms
  Traefik handles request               [----------------------]
    Frontend handles request              [-------------------]
      Frontend calls claims API             [---------------]
        Claims API handles request            [-------------]
```

The shared trace ID helps group spans; their parent and span IDs help show the
relationships. Actual spans, names, IDs and durations depend on the request
and the instrumentations that work at runtime.

## 3. Step one: add the OpenTelemetry package

Example: [claims-api/requirements.txt](../../app/claims-api/requirements.txt)
contains:

```text
splunk-opentelemetry==2.13.3
```

All seven Python application services have this dependency. The
Splunk-distributed OpenTelemetry package provides the Python
auto-instrumentation tooling and related dependencies. It does **not** by
itself start the application or the separate collector. In this repository,
adding it also does not mean data is being sent to a Splunk backend.

## 4. Step two: build each service image

The [claims API Dockerfile](../../app/claims-api/Dockerfile) contains:

```dockerfile
COPY claims-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN opentelemetry-bootstrap -a install
```

Read this in order:

1. Copy the service's dependency list into the image.
2. `pip install` installs the listed dependencies, including the
   Splunk OpenTelemetry package.
3. `opentelemetry-bootstrap -a install` is a **command supplied by the
   installed tooling**, not a separate Docker service. It detects supported
   installed libraries and installs corresponding instrumentation packages.

These `RUN` commands execute **when Docker builds the image**, not on each
request. They prepare the image so that instrumentation is available later.
Each of the seven service Dockerfiles follows this pattern. The exact
instrumentation and coverage depend on the installed package versions and
supported libraries.

## 5. Step three: start the Python service with instrumentation

The same Dockerfile ends with:

```dockerfile
CMD ["opentelemetry-instrument", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

`uvicorn main:app` starts the FastAPI application. Putting
`opentelemetry-instrument` before it starts that application with the
auto-instrumentation enabled. At runtime, supported instrumentation can
observe operations and create spans.

This command runs **when the container starts**. It is different from the
bootstrap command, which runs at image build time.

Traefik is not a Python application and does not use this Dockerfile pattern.
Its tracing is configured separately under the `traefik` service in
[app/compose.yaml](../../app/compose.yaml).

## 6. Step four: give the services their settings

Near the top of [app/compose.yaml](../../app/compose.yaml):

```yaml
x-otel-env: &otel-env
  OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector:4317
  OTEL_EXPORTER_OTLP_PROTOCOL: grpc
  OTEL_RESOURCE_ATTRIBUTES: deployment.environment=claims-lab,service.version=1.0.0
  OTEL_PYTHON_EXCLUDED_URLS: health
  OTEL_PYTHON_LOG_CORRELATION: "true"
  OTEL_LOGS_EXPORTER: none
```

This is a reusable block of **environment variables**. An environment variable
is a named setting made available to a process in a container.

### What do `x-otel-env`, `&otel-env` and `<<` mean?

- `x-otel-env` is a Compose **extension field**: a place to hold reusable
  configuration. It does not create a service or start a collector.
- `&otel-env` is a YAML **anchor**: it gives this block a name for reuse.
- `<<: *otel-env` merges the anchored values into a service's environment.
  The `*otel-env` part refers back to the anchor.

For example, the claims API includes:

```yaml
environment:
  SERVICE_NAME: claims-api
  OTEL_SERVICE_NAME: claims-api
  <<: *otel-env
```

Conceptually, after the merge, the service has both its own settings and the
common settings. `SERVICE_NAME` is an application setting;
`OTEL_SERVICE_NAME` tells OpenTelemetry which service name to attach to its
telemetry. Each Python service sets its own OpenTelemetry service name.

### What do the common settings do?

| Setting | Why it is there |
| --- | --- |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | The destination: the collector container named `otel-collector`, on port `4317`. |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | Uses gRPC to send OTLP telemetry. |
| `OTEL_RESOURCE_ATTRIBUTES` | Adds the lab environment and service version as resource attributes. |
| `OTEL_PYTHON_EXCLUDED_URLS` | Excludes matching health URLs from Python instrumentation, reducing routine health-check traces. |
| `OTEL_PYTHON_LOG_CORRELATION` | Enables Python log correlation; seeing trace IDs in a log line also depends on the log format and active context. |
| `OTEL_LOGS_EXPORTER` | `none` disables OTLP log export from the Python services. It does not turn off ordinary application logging. |

These settings tell instrumented Python processes **how to behave**. The anchor
alone does not install instrumentation; the Dockerfile and dependency do that.
Conversely, installing instrumentation does not create the collector.

Traefik has separate `--tracing...` settings: it sends traces to
`otel-collector:4317` over gRPC and has a configured sample rate of `1.0`.

## 7. Step five: start the separate collector

[otel/compose.yaml](../../otel/compose.yaml) defines the collector container:

```yaml
services:
  otel-collector:
    image: quay.io/signalfx/splunk-otel-collector:latest
    command: ["--config=/etc/otelcol/config.yaml"]
    env_file:
      - .env
    volumes:
      - ./config.yaml:/etc/otelcol/config.yaml:ro
    networks:
      - claims-net
```

- `image` chooses the collector image; `latest` is a moving tag, not a pinned
  version.
- `command` tells the collector which configuration file to read inside its
  container.
- `env_file` supplies environment variables to the collector container. Do
  not share its contents if they contain secrets. The checked-in collector
  configuration shown below does not reference any environment variables.
- `volumes` makes the local `otel/config.yaml` available as
  `/etc/otelcol/config.yaml` inside the container. `ro` means read-only.
- `networks` joins the shared `claims-net` network.

Both Compose projects use the same **external** network, which must already
exist. This lets application containers resolve the collector service by the
name `otel-collector`. `localhost` inside an application container would mean
that application container itself, not the collector.

The collector Compose file does not publish ports to your computer. The
collector ports described next are used on the Docker network.

## 8. Step six: understand the collector's configuration

[otel/config.yaml](../../otel/config.yaml) describes what the collector does
after it starts. Declaring a receiver, processor or exporter is not enough on
its own: the `service.pipelines` section connects them into a working pipeline.

### Receiver: the entrance

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318
```

The OTLP receiver listens for telemetry using gRPC on `4317` and HTTP on
`4318`. `0.0.0.0` means it listens on the collector container's network
interfaces. The Python services and Traefik are configured to use gRPC on
`4317`; `4318` is another available receiver endpoint, not their configured
destination.

### Processors: what happens in the middle

The trace pipeline lists processors in this order:

1. `memory_limiter`: checks memory every two seconds and applies a configured
   400 MiB limit to help protect the collector under pressure.
2. `batch`: groups telemetry for export instead of necessarily sending each
   item immediately.
3. `resourcedetection`: uses the `system` detector with `override: true` to
   add detected resource information and allow detected values to replace
   existing values for matching attributes.
4. `resource/env`: sets `deployment.environment` to `claims-lab` using
   `upsert`, meaning insert it if absent or update it if present.

The application also sets `deployment.environment=claims-lab`. The collector's
processor independently sets that attribute on telemetry passing through it.

### Exporter: the destination

```yaml
exporters:
  debug:
    verbosity: detailed
```

The `debug` exporter prints detailed telemetry to the collector's own logs.
It is useful for learning and troubleshooting, but it is not a trace database
or a graphical trace viewer.

### Pipeline: connect the pieces

```yaml
service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, batch, resourcedetection, resource/env]
      exporters: [debug]
```

This means:

```text
receive OTLP traces
  -> limit memory -> batch -> detect resources -> set environment
  -> print detailed trace data in collector logs
```

Only a **traces** pipeline is configured. The presence of an OTLP receiver does
not by itself create metrics or logs pipelines.

## 9. Putting it all together: one request

Suppose a customer submits a claim:

1. The browser sends the request to Traefik; Traefik routes it to the frontend.
2. The frontend calls the claims API through Traefik. Claims API may call the
   customer, policy and payment services. Payment may call the bank simulator.
3. Instrumented components may create spans while handling their operations.
   When supported components propagate trace context in HTTP headers, their
   spans can be related across service boundaries.
4. Each configured component exports its telemetry to
   `otel-collector:4317` over the shared network. Export is separate from the
   application's business response.
5. The collector's OTLP receiver accepts traces, the processors handle them,
   and the debug exporter writes them to the collector logs.

The service-to-service HTTP calls routed through Traefik use Traefik's internal
entry point. Payment calls the bank directly. A claim decision can also be sent
through RabbitMQ to the notification service. Whether the messaging operation
is instrumented and trace context continues across that asynchronous boundary
must be checked in real output; do not assume it from the configuration alone.

## 10. Try it without changing the application

These commands assume the application and collector are already running.
Run them from the repository root:

```bash
# See whether the application and collector containers are running.
docker compose -f app/compose.yaml ps
docker compose -f otel/compose.yaml ps

# Generate a normal frontend request.
curl http://localhost:8080/

# Read the collector's recent output.
docker compose -f otel/compose.yaml logs --tail=100 otel-collector
```

The homepage request demonstrates a frontend request; it does not by itself
submit a claim or exercise the payment path. To explore those services, use the
application's claim form with valid data and inspect the collector output again.

Look for `service.name`, trace IDs, span IDs, span names and timestamps. The
exact format and spans vary. Related spans have the same trace ID, but may
appear in different log entries or batches, and some operations may not be
instrumented. Allow a little time for batching. Detailed output can contain
sensitive attributes; be careful where you share it.

If you see no trace data:

1. Check that the relevant containers are running with the `ps` commands above.
2. Check both the collector logs and the relevant application service logs for
   connection or export errors, for example:

   ```bash
   docker compose -f app/compose.yaml logs --tail=100 frontend
   ```

3. Confirm both Compose projects use the existing `claims-net` network, and that
   the application endpoint is `otel-collector:4317`.
4. Generate a normal request, not just `/health`, which the Python
   instrumentation is configured to exclude.
5. Allow for batching and check the collector logs again. A successful HTTP
   response alone does not prove telemetry was exported.

## 11. What is not configured

- There is no configured trace-storage backend or trace UI: collector debug
  output goes to container logs.
- The collector has no metrics or logs pipeline, and Python OTLP log export is
  disabled. Ordinary application logging remains separate.
- This guide describes configuration, not a guarantee of live trace coverage,
  end-to-end messaging propagation, or successful export.

For the system-wide picture, see the [overview](./observability-overview.md)
and [architecture diagrams](./architecture-diagram.md).
