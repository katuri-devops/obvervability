# Claims application architecture

See the [observability overview](./observability-overview.md) for a
beginner-friendly explanation. These diagrams describe the configured
architecture; actual trace coverage must be verified at runtime.

## Application architecture

Solid arrows are HTTP calls, messages, or data access. Calls labelled
**through Traefik** use its internal entry point; the bank call is direct.

```mermaid
flowchart TD
    User[Customer / browser] -->|HTTP| Traefik[Traefik gateway]
    Traefik --> Frontend[Frontend]
    Frontend -->|HTTP through Traefik| Claims[Claims API]

    Claims -->|HTTP through Traefik| Customer[Customer service]
    Claims -->|HTTP through Traefik| Policy[Policy service]
    Claims -->|If approved: HTTP through Traefik| Payment[Payment service]
    Payment -->|HTTP| Bank[Bank API simulator]

    Claims -->|claim.decided event| RabbitMQ[RabbitMQ]
    RabbitMQ --> Notification[Notification service]
    Notification -->|HTTP through Traefik| Customer

    Claims --> Postgres[(PostgreSQL)]
    Customer --> Postgres
    Policy --> Postgres
    Payment --> Postgres
    Notification --> Postgres

    Customer --> Redis[(Redis cache)]
    Policy --> Redis
    Payment -->|Locking| Redis
```

## Telemetry flow

The dotted arrows represent configured OTLP destinations, not a guarantee that
every operation produces a span. The collector currently has a trace pipeline
only.

```mermaid
flowchart LR
    Traefik[Traefik] -.->|OTLP/gRPC| Receiver
    Python[Frontend and Python API services] -.->|OTLP/gRPC| Receiver

    subgraph Collector[OpenTelemetry Collector]
        Receiver[OTLP receiver] --> Memory[Memory limiter]
        Memory --> Batch[Batch]
        Batch --> Detection[Resource detection]
        Detection --> Environment[Environment attribute]
        Environment --> Debug[Debug exporter]
    end

    Debug --> Logs[Collector container logs]
```

The application services and collector share `claims-net`. The collector also
listens for OTLP/HTTP internally, but the application configuration uses
OTLP/gRPC on port `4317`.
