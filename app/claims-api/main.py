import asyncio
import json
import logging
import os
import socket
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal
from uuid import uuid4

import aio_pika
import httpx
from aio_pika import DeliveryMode, ExchangeType, Message
from fastapi import FastAPI, Path, Request
from fastapi.exceptions import RequestValidationError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import text
from sqlalchemy.exc import (
    DBAPIError,
    InterfaceError,
    OperationalError,
    SQLAlchemyError,
    TimeoutError as SQLAlchemyTimeoutError,
)
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("claims-api")

SERVICE_NAME = os.getenv("SERVICE_NAME", "claims-api")
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://claims:claims-dev-password@postgres:5432/claims"
    "?application_name=claims-api",
)
CUSTOMER_SERVICE_URL = os.getenv(
    "CUSTOMER_SERVICE_URL", "http://traefik:8000/customer-service"
).rstrip("/")
POLICY_SERVICE_URL = os.getenv(
    "POLICY_SERVICE_URL", "http://traefik:8000/policy-service"
).rstrip("/")
PAYMENT_SERVICE_URL = os.getenv(
    "PAYMENT_SERVICE_URL", "http://traefik:8000/payment-service"
).rstrip("/")
RABBITMQ_URL = os.getenv(
    "RABBITMQ_URL", "amqp://claims:claims-dev-password@rabbitmq:5672/%2F"
)
engine = create_async_engine(DATABASE_URL)

CLAIM_ID_PATTERN = r"^CLM-[0-9]{6,}$"
CUSTOMER_ID_PATTERN = r"^C-[0-9]{4}$"
POLICY_ID_PATTERN = r"^P-[0-9]{4}$"


class APIError(Exception):
    def __init__(self, status_code: int, code: str, message: str, fields=None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.fields = fields


def error_response(status_code: int, code: str, message: str, fields=None, headers=None):
    error = {"code": code, "message": message}
    if fields is not None:
        error["fields"] = fields
    return JSONResponse(
        status_code=status_code,
        content={"error": error},
        headers=headers,
    )


def database_unavailable(exc: SQLAlchemyError) -> bool:
    return isinstance(
        exc, (OperationalError, InterfaceError, SQLAlchemyTimeoutError)
    ) or (isinstance(exc, DBAPIError) and exc.connection_invalidated)


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


async def connect_rabbitmq(app: FastAPI):
    while True:
        connection = None
        try:
            connection = await aio_pika.connect_robust(RABBITMQ_URL)
            channel = await connection.channel(publisher_confirms=True)
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
            app.state.rabbit_connection = connection
            app.state.claims_exchange = claims_exchange
            logger.info("RabbitMQ connection is ready")
            return
        except Exception:
            if connection is not None:
                with suppress(Exception):
                    await connection.close()
            logger.warning("RabbitMQ is not ready; retrying in 2 seconds")
            await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http_client = httpx.AsyncClient()
    retry_task = asyncio.create_task(retry_database_connection())
    app.state.rabbit_connection = None
    app.state.claims_exchange = None
    rabbit_task = asyncio.create_task(connect_rabbitmq(app))
    try:
        yield
    finally:
        retry_task.cancel()
        rabbit_task.cancel()
        with suppress(asyncio.CancelledError):
            await retry_task
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


@app.exception_handler(APIError)
async def handle_api_error(_: Request, exc: APIError):
    return error_response(exc.status_code, exc.code, exc.message, exc.fields)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(_: Request, exc: RequestValidationError):
    fields = {}
    for issue in exc.errors():
        location = issue.get("loc", ())
        field = str(location[-1]) if location else "request"
        error_type = issue.get("type", "")
        if error_type == "extra_forbidden":
            message = "This field is not allowed."
        elif error_type == "missing":
            message = "This field is required."
        elif field == "amount_claimed":
            message = "Enter an amount greater than 0 and at most 1000000, with at most two decimals."
        elif field == "description":
            message = "Enter a description between 10 and 1000 characters."
        elif field == "incident_date":
            message = "Enter a valid date that is not after today in UTC."
        elif field == "customer_id":
            message = "Enter a customer ID in the format C-1234."
        elif field == "policy_id":
            message = "Enter a policy ID in the format P-1234."
        else:
            message = "Enter a valid value."
        fields[field] = message
    return error_response(
        422,
        "VALIDATION_FAILED",
        "Some fields are not valid.",
        fields=fields,
    )


@app.exception_handler(StarletteHTTPException)
async def handle_http_error(_: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        return error_response(404, "NOT_FOUND", "The requested address was not found.")
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
    logger.exception(
        "Unexpected request error",
        extra={"claim_id": request.path_params.get("claim_id")},
    )
    return error_response(500, "INTERNAL_ERROR", "An unexpected error occurred.")


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: Annotated[str, StringConstraints(pattern=CUSTOMER_ID_PATTERN)]
    policy_id: Annotated[str, StringConstraints(pattern=POLICY_ID_PATTERN)]
    incident_date: date
    description: Annotated[str, Field(min_length=10, max_length=1000)]
    amount_claimed: Annotated[Decimal, Field(gt=0, le=1_000_000)]

    @field_validator("amount_claimed", mode="before")
    @classmethod
    def amount_must_be_json_number(cls, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Amount must be a JSON number.")
        return value

    @field_validator("amount_claimed")
    @classmethod
    def amount_has_at_most_two_decimals(cls, value: Decimal):
        if value.as_tuple().exponent < -2:
            raise ValueError("Amount must have at most two decimal places.")
        return value


class CustomerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: Annotated[str, StringConstraints(pattern=CUSTOMER_ID_PATTERN)]
    full_name: str
    email: Annotated[str, StringConstraints(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    status: Literal["ACTIVE", "SUSPENDED"]
    source: Literal["cache", "database"]


class CoverageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_id: Annotated[str, StringConstraints(pattern=POLICY_ID_PATTERN)]
    covered: bool
    reason: Literal[
        "POLICY_NOT_OWNED",
        "POLICY_NOT_ACTIVE",
        "INCIDENT_OUTSIDE_POLICY_PERIOD",
        "BELOW_DEDUCTIBLE",
    ] | None
    coverage_limit: Annotated[Decimal, Field(ge=0)]
    deductible: Annotated[Decimal, Field(ge=0)]
    payable_amount: Annotated[Decimal, Field(ge=0)]

    @field_validator("coverage_limit", "deductible", "payable_amount", mode="before")
    @classmethod
    def money_must_be_json_number(cls, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Money values must be JSON numbers.")
        return value

    @field_validator("coverage_limit", "deductible", "payable_amount")
    @classmethod
    def money_has_at_most_two_decimals(cls, value: Decimal):
        if value.as_tuple().exponent < -2:
            raise ValueError("Money values must have at most two decimal places.")
        return value

    @model_validator(mode="after")
    def response_fields_are_consistent(self):
        if self.covered != (self.reason is None):
            raise ValueError("covered and reason do not match.")
        if not self.covered and self.payable_amount != 0:
            raise ValueError("An uncovered claim must have a zero payable amount.")
        return self


class PaymentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payment_id: Annotated[str, StringConstraints(pattern=r"^PAY-[0-9]{6,}$")]
    claim_id: Annotated[str, StringConstraints(pattern=CLAIM_ID_PATTERN)]
    customer_id: Annotated[str, StringConstraints(pattern=CUSTOMER_ID_PATTERN)]
    amount: Annotated[Decimal, Field(gt=0, le=1_000_000)]
    status: Literal["COMPLETED", "FAILED"]
    bank_transfer_id: str | None
    failure_reason: Literal["BANK_TIMEOUT", "BANK_ERROR"] | None
    created_at: datetime

    @field_validator("amount", mode="before")
    @classmethod
    def payment_amount_must_be_json_number(cls, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Amount must be a JSON number.")
        return value

    @field_validator("amount")
    @classmethod
    def payment_amount_has_at_most_two_decimals(cls, value: Decimal):
        if value.as_tuple().exponent < -2:
            raise ValueError("Amount must have at most two decimal places.")
        return value

    @model_validator(mode="after")
    def payment_result_is_consistent(self):
        if self.created_at.utcoffset() is None:
            raise ValueError("created_at must include a timezone.")
        if self.status == "COMPLETED":
            if self.bank_transfer_id is None or self.failure_reason is not None:
                raise ValueError("Completed payment fields do not match.")
        elif self.bank_transfer_id is not None or self.failure_reason is None:
            raise ValueError("Failed payment fields do not match.")
        return self


def is_error_code(response: httpx.Response, status_code: int, code: str) -> bool:
    if response.status_code != status_code:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    error = body.get("error") if isinstance(body, dict) else None
    return isinstance(error, dict) and error.get("code") == code and isinstance(
        error.get("message"), str
    )


def dependency_error(message: str):
    return APIError(503, "DEPENDENCY_UNAVAILABLE", message)


async def call_customer(client: httpx.AsyncClient, customer_id: str) -> CustomerResponse:
    try:
        response = await client.get(
            f"{CUSTOMER_SERVICE_URL}/customers/{customer_id}", timeout=5.0
        )
    except httpx.RequestError as exc:
        logger.error("customer-service request failed", extra={"customer_id": customer_id})
        raise dependency_error("customer-service is unavailable.") from exc

    if is_error_code(response, 404, "NOT_FOUND"):
        raise APIError(
            422,
            "VALIDATION_FAILED",
            "Some fields are not valid.",
            {"customer_id": "There is no customer with this ID."},
        )
    if response.status_code != 200:
        logger.error(
            "customer-service returned an unexpected status",
            extra={"customer_id": customer_id},
        )
        raise dependency_error("customer-service is unavailable.")

    try:
        customer = CustomerResponse.model_validate(response.json())
    except (ValueError, ValidationError) as exc:
        logger.error(
            "customer-service returned an invalid answer",
            extra={"customer_id": customer_id},
        )
        raise dependency_error("customer-service returned an invalid answer.") from exc

    if customer.customer_id != customer_id:
        raise dependency_error("customer-service returned an invalid answer.")
    return customer


async def call_policy(
    client: httpx.AsyncClient, request: ClaimRequest
) -> CoverageResponse:
    try:
        response = await client.post(
            f"{POLICY_SERVICE_URL}/policies/{request.policy_id}/coverage-check",
            json={
                "customer_id": request.customer_id,
                "incident_date": request.incident_date.isoformat(),
                "amount_claimed": float(request.amount_claimed),
            },
            timeout=5.0,
        )
    except httpx.RequestError as exc:
        logger.error("policy-service request failed", extra={"policy_id": request.policy_id})
        raise dependency_error("policy-service is unavailable.") from exc

    if is_error_code(response, 404, "NOT_FOUND"):
        raise APIError(
            422,
            "VALIDATION_FAILED",
            "Some fields are not valid.",
            {"policy_id": "There is no policy with this ID."},
        )
    if response.status_code != 200:
        logger.error(
            "policy-service returned an unexpected status",
            extra={"policy_id": request.policy_id},
        )
        raise dependency_error("policy-service is unavailable.")

    try:
        coverage = CoverageResponse.model_validate(response.json())
    except (ValueError, ValidationError) as exc:
        logger.error(
            "policy-service returned an invalid answer",
            extra={"policy_id": request.policy_id},
        )
        raise dependency_error("policy-service returned an invalid answer.") from exc

    if coverage.policy_id != request.policy_id:
        raise dependency_error("policy-service returned an invalid answer.")
    if coverage.reason == "POLICY_NOT_OWNED":
        raise APIError(
            422,
            "VALIDATION_FAILED",
            "Some fields are not valid.",
            {"policy_id": "This policy does not belong to this customer."},
        )
    return coverage


async def call_payment(
    client: httpx.AsyncClient,
    claim_id: str,
    customer_id: str,
    amount: Decimal,
) -> PaymentResponse | None:
    try:
        response = await client.post(
            f"{PAYMENT_SERVICE_URL}/payments",
            json={
                "claim_id": claim_id,
                "customer_id": customer_id,
                "amount": float(amount),
            },
            timeout=15.0,
        )
    except httpx.RequestError:
        logger.error("payment-service request failed", extra={"claim_id": claim_id})
        return None

    if response.status_code != 201:
        logger.error(
            "payment-service returned an unexpected status",
            extra={"claim_id": claim_id},
        )
        return None

    try:
        payment = PaymentResponse.model_validate(response.json())
    except (ValueError, ValidationError):
        logger.error(
            "payment-service returned an invalid answer",
            extra={"claim_id": claim_id},
        )
        return None

    if (
        payment.claim_id != claim_id
        or payment.customer_id != customer_id
        or payment.amount != amount
    ):
        logger.error(
            "payment-service answer did not match the request",
            extra={"claim_id": claim_id},
        )
        return None
    return payment


def money_number(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def utc_timestamp(value: datetime) -> str:
    if value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def claim_response(row):
    return {
        "claim_id": row["claim_id"],
        "customer_id": row["customer_id"],
        "policy_id": row["policy_id"],
        "incident_date": row["incident_date"].isoformat(),
        "description": row["description"],
        "amount_claimed": money_number(row["amount_claimed"]),
        "approved_amount": money_number(row["approved_amount"]),
        "status": row["status"],
        "reason": row["reason"],
        "payment_id": row["payment_id"],
        "created_at": utc_timestamp(row["created_at"]),
        "updated_at": utc_timestamp(row["updated_at"]),
    }


async def publish_claim_decided(row):
    exchange = app.state.claims_exchange
    if exchange is None:
        logger.warning(
            "RabbitMQ is unavailable; claim-decided message was not sent",
            extra={"claim_id": row["claim_id"]},
        )
        return

    payload = {
        "claim_id": row["claim_id"],
        "customer_id": row["customer_id"],
        "policy_id": row["policy_id"],
        "status": row["status"],
        "reason": row["reason"],
        "approved_amount": money_number(row["approved_amount"]),
        "payment_id": row["payment_id"],
        "decided_at": utc_timestamp(row["updated_at"]),
    }
    message = Message(
        body=json.dumps(payload).encode("utf-8"),
        content_type="application/json",
        delivery_mode=DeliveryMode.PERSISTENT,
        message_id=str(uuid4()),
        type="claim.decided",
    )
    try:
        async with asyncio.timeout(2):
            await exchange.publish(message, routing_key="claim.decided")
    except Exception:
        logger.warning(
            "RabbitMQ did not confirm claim-decided message",
            extra={"claim_id": row["claim_id"]},
        )


async def save_claim(request: ClaimRequest, status: str, reason: str | None,
                     approved_amount: Decimal | None):
    query = text(
        """
        INSERT INTO claim.claims
            (customer_id, policy_id, incident_date, description, amount_claimed,
             approved_amount, status, reason)
        VALUES
            (:customer_id, :policy_id, :incident_date, :description, :amount_claimed,
             :approved_amount, :status, :reason)
        RETURNING claim_id, customer_id, policy_id, incident_date, description,
                  amount_claimed, approved_amount, status, reason, payment_id,
                  created_at, updated_at
        """
    )
    try:
        async with engine.begin() as connection:
            result = await connection.execute(
                query,
                {
                    "customer_id": request.customer_id,
                    "policy_id": request.policy_id,
                    "incident_date": request.incident_date,
                    "description": request.description,
                    "amount_claimed": request.amount_claimed,
                    "approved_amount": approved_amount,
                    "status": status,
                    "reason": reason,
                },
            )
            return result.mappings().one()
    except SQLAlchemyError as exc:
        if database_unavailable(exc):
            logger.error("Claim database is unavailable", extra={"customer_id": request.customer_id})
            raise dependency_error("The claim database is unavailable.") from exc
        logger.exception("Claim could not be saved", extra={"customer_id": request.customer_id})
        raise APIError(500, "INTERNAL_ERROR", "An unexpected error occurred.") from exc


async def update_claim_payment(claim_id: str, status: str, reason: str | None,
                               payment_id: str | None):
    query = text(
        """
        UPDATE claim.claims
        SET status = :status, reason = :reason, payment_id = :payment_id,
            updated_at = now()
        WHERE claim_id = :claim_id
        RETURNING claim_id, customer_id, policy_id, incident_date, description,
                  amount_claimed, approved_amount, status, reason, payment_id,
                  created_at, updated_at
        """
    )
    try:
        async with engine.begin() as connection:
            result = await connection.execute(
                query,
                {
                    "claim_id": claim_id,
                    "status": status,
                    "reason": reason,
                    "payment_id": payment_id,
                },
            )
            row = result.mappings().one_or_none()
    except SQLAlchemyError as exc:
        if database_unavailable(exc):
            logger.error("Claim database is unavailable", extra={"claim_id": claim_id})
            raise dependency_error("The claim database is unavailable.") from exc
        logger.exception("Claim update failed", extra={"claim_id": claim_id})
        raise APIError(500, "INTERNAL_ERROR", "An unexpected error occurred.") from exc
    if row is None:
        raise APIError(500, "INTERNAL_ERROR", "An unexpected error occurred.")
    return row


@app.get("/health")
async def health():
    return {
        "service": SERVICE_NAME,
        "instance": socket.gethostname(),
        "status": "ok",
    }


@app.post("/claims", status_code=201)
async def submit_claim(request: ClaimRequest):
    if request.incident_date > datetime.now(timezone.utc).date():
        raise APIError(
            422,
            "VALIDATION_FAILED",
            "Some fields are not valid.",
            {"incident_date": "The incident date cannot be after today's date in UTC."},
        )

    client = app.state.http_client
    customer = await call_customer(client, request.customer_id)
    coverage = await call_policy(client, request)

    status: str
    reason: str | None
    approved_amount: Decimal | None
    if customer.status != "ACTIVE":
        status = "REJECTED"
        reason = "CUSTOMER_NOT_ACTIVE"
        approved_amount = None
    elif not coverage.covered:
        status = "REJECTED"
        reason = coverage.reason
        approved_amount = None
    elif coverage.payable_amount > Decimal("5000"):
        status = "UNDER_REVIEW"
        reason = "ABOVE_REVIEW_LIMIT"
        approved_amount = coverage.payable_amount
    else:
        status = "APPROVED"
        reason = None
        approved_amount = coverage.payable_amount

    row = await save_claim(request, status, reason, approved_amount)

    if status == "APPROVED":
        payment = await call_payment(
            client,
            row["claim_id"],
            request.customer_id,
            approved_amount,
        )
        if payment is None:
            status = "PAYMENT_FAILED"
            reason = "PAYMENT_ERROR"
            payment_id = None
        elif payment.status == "COMPLETED":
            status = "PAID"
            reason = None
            payment_id = payment.payment_id
        else:
            status = "PAYMENT_FAILED"
            reason = "PAYMENT_ERROR"
            payment_id = payment.payment_id
        row = await update_claim_payment(row["claim_id"], status, reason, payment_id)

    logger.info(
        "Claim decided",
        extra={
            "claim_id": row["claim_id"],
            "customer_id": row["customer_id"],
            "policy_id": row["policy_id"],
            "payment_id": row["payment_id"],
        },
    )
    await publish_claim_decided(row)
    return claim_response(row)


@app.get("/claims/{claim_id}")
async def get_claim(claim_id: str = Path(pattern=CLAIM_ID_PATTERN)):
    query = text(
        """
        SELECT claim_id, customer_id, policy_id, incident_date, description,
               amount_claimed, approved_amount, status, reason, payment_id,
               created_at, updated_at
        FROM claim.claims
        WHERE claim_id = :claim_id
        """
    )
    try:
        async with engine.connect() as connection:
            result = await connection.execute(query, {"claim_id": claim_id})
            row = result.mappings().one_or_none()
    except SQLAlchemyError as exc:
        if database_unavailable(exc):
            logger.error("Claim database is unavailable", extra={"claim_id": claim_id})
            raise dependency_error("The claim database is unavailable.") from exc
        logger.exception("Claim lookup failed", extra={"claim_id": claim_id})
        raise APIError(500, "INTERNAL_ERROR", "An unexpected error occurred.") from exc

    if row is None:
        raise APIError(404, "NOT_FOUND", "The requested claim was not found.")
    return claim_response(row)
