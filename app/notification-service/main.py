import asyncio
import json
import logging
import os
import socket
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from decimal import Decimal
from uuid import UUID

import aio_pika
import httpx
from aio_pika import ExchangeType
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import text
from sqlalchemy.exc import (
    DBAPIError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    SQLAlchemyError,
    TimeoutError as SQLAlchemyTimeoutError,
)
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("notification-service")

SERVICE_NAME = os.getenv("SERVICE_NAME", "notification-service")
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://claims:claims-dev-password@postgres:5432/claims"
    "?application_name=notification-service",
)
CUSTOMER_SERVICE_URL = os.getenv(
    "CUSTOMER_SERVICE_URL", "http://traefik:8000/customer-service"
).rstrip("/")
RABBITMQ_URL = os.getenv(
    "RABBITMQ_URL", "amqp://claims:claims-dev-password@rabbitmq:5672/%2F"
)
engine = create_async_engine(DATABASE_URL)

CLAIM_ID_PATTERN = r"^CLM-[0-9]{6,}$"
CUSTOMER_ID_PATTERN = r"^C-[0-9]{4}$"
POLICY_ID_PATTERN = r"^P-[0-9]{4}$"
REJECTION_REASONS = {
    "CUSTOMER_NOT_ACTIVE": "Your customer account is not active.",
    "POLICY_NOT_ACTIVE": "The policy is not active.",
    "INCIDENT_OUTSIDE_POLICY_PERIOD": "The incident date is outside the period the policy covers.",
    "BELOW_DEDUCTIBLE": "The amount claimed is not more than the policy's deductible.",
}


class ClaimDecided(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(pattern=CLAIM_ID_PATTERN)
    customer_id: str = Field(pattern=CUSTOMER_ID_PATTERN)
    policy_id: str = Field(pattern=POLICY_ID_PATTERN)
    status: str = Field(pattern=r"^(REJECTED|UNDER_REVIEW|PAID|PAYMENT_FAILED)$")
    reason: str | None = Field(
        default=None,
        pattern=r"^(CUSTOMER_NOT_ACTIVE|POLICY_NOT_ACTIVE|INCIDENT_OUTSIDE_POLICY_PERIOD|BELOW_DEDUCTIBLE|ABOVE_REVIEW_LIMIT|PAYMENT_ERROR)$",
    )
    approved_amount: Decimal | None = Field(default=None, ge=0)
    payment_id: str | None = Field(
        default=None, pattern=r"^PAY-[0-9]{6,}$"
    )
    decided_at: datetime

    @model_validator(mode="after")
    def can_create_email(self):
        if self.status == "PAID" and self.approved_amount is None:
            raise ValueError("A paid claim needs an approved amount")
        if self.status == "REJECTED" and self.reason not in REJECTION_REASONS:
            raise ValueError("A rejected claim needs an email reason")
        return self


async def retry_database_connection():
    while True:
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            logger.info("Database connection is ready")
            return
        except SQLAlchemyError:
            logger.warning("PostgreSQL is not ready; retrying in 2 seconds")
            await asyncio.sleep(2)


def database_unavailable(exc: SQLAlchemyError) -> bool:
    return isinstance(
        exc, (OperationalError, InterfaceError, SQLAlchemyTimeoutError)
    ) or (isinstance(exc, DBAPIError) and exc.connection_invalidated)


async def connect_rabbitmq(app: FastAPI):
    while True:
        connection = None
        try:
            connection = await aio_pika.connect_robust(RABBITMQ_URL)
            channel = await connection.channel()
            await channel.set_qos(prefetch_count=1)
            claims_exchange = await channel.declare_exchange(
                "claims",
                ExchangeType.TOPIC,
                durable=True,
                auto_delete=False,
            )
            dead_letter_exchange = await channel.declare_exchange(
                "claims.dead-letter",
                ExchangeType.FANOUT,
                durable=True,
                auto_delete=False,
            )
            notifications_queue = await channel.declare_queue(
                "notifications.claim-decided",
                durable=True,
                exclusive=False,
                auto_delete=False,
                arguments={
                    "x-queue-type": "classic",
                    "x-dead-letter-exchange": "claims.dead-letter",
                },
            )
            dead_letter_queue = await channel.declare_queue(
                "notifications.claim-decided.dead-letter",
                durable=True,
                exclusive=False,
                auto_delete=False,
                arguments={"x-queue-type": "classic"},
            )
            await notifications_queue.bind(claims_exchange, routing_key="claim.decided")
            await dead_letter_queue.bind(dead_letter_exchange, routing_key="")
            await notifications_queue.consume(handle_claim_decided, no_ack=False)
            app.state.rabbit_connection = connection
            logger.info("RabbitMQ consumer is ready")
            return
        except asyncio.CancelledError:
            if connection is not None:
                with suppress(Exception):
                    await connection.close()
            raise
        except Exception:
            if connection is not None:
                with suppress(Exception):
                    await connection.close()
            logger.warning("RabbitMQ is not ready; retrying in 2 seconds")
            await asyncio.sleep(2)


def email_for_claim(claim: ClaimDecided, full_name: str):
    claim_id = claim.claim_id
    policy_id = claim.policy_id
    if claim.status == "PAID":
        amount = claim.approved_amount.quantize(Decimal("0.01"))
        subject = f"Your claim {claim_id} is paid"
        sentence = (
            f"Your claim {claim_id} on policy {policy_id} is approved. "
            f"We have sent {amount:.2f} to your bank account."
        )
    elif claim.status == "UNDER_REVIEW":
        subject = f"Your claim {claim_id} is under review"
        sentence = (
            f"A claims handler will review your claim {claim_id} on policy "
            f"{policy_id}, because the amount to pay is above 5000. We will "
            "write to you again."
        )
    elif claim.status == "REJECTED":
        subject = f"Your claim {claim_id} is rejected"
        sentence = (
            f"Your claim {claim_id} on policy {policy_id} is rejected. "
            f"{REJECTION_REASONS[claim.reason]}"
        )
    else:
        subject = f"Your claim {claim_id}: payment failed"
        sentence = (
            f"Your claim {claim_id} on policy {policy_id} is approved, "
            "but the payment could not be made. We will contact you."
        )
    body = f"Dear {full_name},\n\n{sentence}\n\nClaims Service"
    return subject, body


async def notification_exists(event_id: UUID) -> bool:
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT 1 FROM notification.notifications "
                "WHERE event_id = :event_id"
            ),
            {"event_id": event_id},
        )
        return result.one_or_none() is not None


async def handle_database_failure(message, claim: ClaimDecided, exc: SQLAlchemyError):
    if database_unavailable(exc):
        logger.warning(
            "Notification database is unavailable; requeueing message in 5 seconds",
            extra={"claim_id": claim.claim_id, "customer_id": claim.customer_id},
        )
        await asyncio.sleep(5)
        await message.reject(requeue=True)
        return
    raise exc


async def process_claim_decided(message):
    try:
        if message.content_type != "application/json":
            raise ValueError("Message content type is not application/json")
        event_id = UUID(message.message_id or "")
        claim = ClaimDecided.model_validate_json(message.body)
    except (ValueError, ValidationError, json.JSONDecodeError) as exc:
        logger.error("Unreadable claim-decided message", extra={})
        await message.reject(requeue=False)
        return

    try:
        if await notification_exists(event_id):
            await message.ack()
            return
    except SQLAlchemyError as exc:
        await handle_database_failure(message, claim, exc)
        return

    try:
        async with asyncio.timeout(5):
            response = await app.state.http_client.get(
                f"{CUSTOMER_SERVICE_URL}/customers/{claim.customer_id}"
            )
    except (httpx.HTTPError, TimeoutError):
        response = None

    if response is not None and response.status_code == 404:
        try:
            customer_error = response.json()
        except ValueError:
            customer_error = None
        error_body = customer_error.get("error") if isinstance(customer_error, dict) else None
        if isinstance(error_body, dict) and error_body.get("code") == "NOT_FOUND":
            logger.error(
                "Claim notification refers to a missing customer",
                extra={"claim_id": claim.claim_id, "customer_id": claim.customer_id},
            )
            await message.reject(requeue=False)
            return

    customer = None
    if response is not None and response.status_code == 200:
        try:
            customer_data = response.json()
            if (
                isinstance(customer_data, dict)
                and isinstance(customer_data.get("full_name"), str)
                and isinstance(customer_data.get("email"), str)
            ):
                customer = customer_data
        except ValueError:
            pass
    if customer is None:
        logger.warning(
            "Customer-service is unavailable; requeueing message in 5 seconds",
            extra={"claim_id": claim.claim_id, "customer_id": claim.customer_id},
        )
        await asyncio.sleep(5)
        await message.reject(requeue=True)
        return

    subject, body = email_for_claim(claim, customer["full_name"])
    try:
        async with engine.begin() as connection:
            result = await connection.execute(
                text(
                    "INSERT INTO notification.notifications "
                    "(event_id, claim_id, customer_id, email, subject, body, status) "
                    "VALUES (:event_id, :claim_id, :customer_id, :email, "
                    ":subject, :body, 'SENT') "
                    "RETURNING notification_id"
                ),
                {
                    "event_id": event_id,
                    "claim_id": claim.claim_id,
                    "customer_id": claim.customer_id,
                    "email": customer["email"],
                    "subject": subject,
                    "body": body,
                },
            )
            result.scalar_one()
    except IntegrityError:
        try:
            if await notification_exists(event_id):
                await message.ack()
                return
        except SQLAlchemyError as exc:
            await handle_database_failure(message, claim, exc)
            return
        raise
    except SQLAlchemyError as exc:
        await handle_database_failure(message, claim, exc)
        return

    logger.info(
        "Email recorded",
        extra={"claim_id": claim.claim_id, "customer_id": claim.customer_id},
    )
    await message.ack()


async def handle_claim_decided(message):
    try:
        await process_claim_decided(message)
    except Exception:
        logger.exception("Unexpected claim notification failure")
        if not message.processed:
            await message.reject(requeue=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http_client = httpx.AsyncClient(timeout=5.0)
    app.state.rabbit_connection = None
    database_task = asyncio.create_task(retry_database_connection())
    rabbit_task = asyncio.create_task(connect_rabbitmq(app))
    try:
        yield
    finally:
        database_task.cancel()
        rabbit_task.cancel()
        with suppress(asyncio.CancelledError):
            await database_task
        with suppress(asyncio.CancelledError):
            await rabbit_task
        if app.state.rabbit_connection is not None:
            await app.state.rabbit_connection.close()
        await app.state.http_client.aclose()
        await engine.dispose()


app = FastAPI(
    title=SERVICE_NAME,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def error_response(status_code: int, code: str, message: str, fields=None, headers=None):
    error = {"code": code, "message": message}
    if fields is not None:
        error["fields"] = fields
    return JSONResponse(
        status_code=status_code,
        content={"error": error},
        headers=headers,
    )


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError):
    fields = {}
    for issue in exc.errors():
        field = str(issue["loc"][-1]) if issue["loc"] else "request"
        if field == "claim_id":
            fields[field] = "Enter a claim ID in the format CLM-123456."
        else:
            fields[field] = "This field is not valid."
    return error_response(
        422,
        "VALIDATION_FAILED",
        "Some fields are not valid.",
        fields=fields,
    )


@app.exception_handler(StarletteHTTPException)
async def handle_http_error(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        return error_response(404, "NOT_FOUND", "The requested resource was not found.")
    if exc.status_code == 405:
        return error_response(
            405,
            "METHOD_NOT_ALLOWED",
            "This HTTP method is not allowed for this address.",
            headers=exc.headers,
        )
    return error_response(500, "INTERNAL_ERROR", "An unexpected error occurred.")


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception):
    logger.exception("Unexpected request error")
    return error_response(500, "INTERNAL_ERROR", "An unexpected error occurred.")


@app.get("/health")
async def health():
    return {
        "service": SERVICE_NAME,
        "instance": socket.gethostname(),
        "status": "ok",
    }


@app.get("/notifications")
async def list_notifications(
    claim_id: str = Query(pattern=CLAIM_ID_PATTERN),
):
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT notification_id, event_id, claim_id, customer_id, email, "
                    "subject, body, status, created_at "
                    "FROM notification.notifications "
                    "WHERE claim_id = :claim_id "
                    "ORDER BY created_at ASC, notification_id ASC"
                ),
                {"claim_id": claim_id},
            )
            return [dict(row) for row in result.mappings().all()]
    except SQLAlchemyError as exc:
        if database_unavailable(exc):
            return error_response(
                503,
                "DEPENDENCY_UNAVAILABLE",
                "The notification database is unavailable.",
            )
        logger.exception("Notification lookup failed")
        return error_response(500, "INTERNAL_ERROR", "An unexpected error occurred.")
