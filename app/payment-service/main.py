import asyncio
import logging
import os
import re
import socket
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

import httpx
from fastapi import FastAPI, Path, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator, model_validator
from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry
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
logger = logging.getLogger("payment-service")

SERVICE_NAME = os.getenv("SERVICE_NAME", "payment-service")
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://claims:claims-dev-password@postgres:5432/claims"
    "?application_name=payment-service",
)
BANK_URL = os.getenv("BANK_URL", "http://bank-api:8000").rstrip("/")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
engine = create_async_engine(DATABASE_URL)

CLAIM_ID = r"^CLM-[0-9]{6,}$"
CUSTOMER_ID = r"^C-[0-9]{4}$"
PAYMENT_ID = r"^PAY-[0-9]{6,}$"


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.bank_client = httpx.AsyncClient(timeout=10.0)
    app.state.redis_client = Redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=1,
        retry=Retry(NoBackoff(), 0),
    )
    retry_task = asyncio.create_task(retry_database_connection())
    try:
        yield
    finally:
        retry_task.cancel()
        with suppress(asyncio.CancelledError):
            await retry_task
        await app.state.redis_client.aclose()
        await app.state.bank_client.aclose()
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
        field = str(location[-1]) if len(location) > 1 else "request_body"
        if issue.get("type") == "extra_forbidden":
            message = "This field is not allowed."
        elif issue.get("type") == "missing":
            message = "This field is required."
        elif field == "amount":
            message = "Enter an amount greater than 0 and at most 1000000, with at most two decimals."
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
    if exc.status_code == 405:
        return error_response(
            405,
            "METHOD_NOT_ALLOWED",
            "This HTTP method is not allowed for this address.",
            headers=exc.headers,
        )
    return error_response(404, "NOT_FOUND", "The requested address was not found.")


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception):
    logger.exception(
        "Unexpected request error",
        extra={"claim_id": request.path_params.get("claim_id")},
    )
    return error_response(500, "INTERNAL_ERROR", "An unexpected error occurred.")


class PaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: Annotated[str, StringConstraints(pattern=CLAIM_ID)]
    customer_id: Annotated[str, StringConstraints(pattern=CUSTOMER_ID)]
    amount: Annotated[Decimal, Field(gt=0, le=1_000_000)]

    @field_validator("amount", mode="before")
    @classmethod
    def amount_must_be_json_number(cls, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Amount must be a JSON number.")
        return value

    @field_validator("amount")
    @classmethod
    def amount_has_at_most_two_decimals(cls, value: Decimal):
        if value.as_tuple().exponent < -2:
            raise ValueError("Amount must have at most two decimal places.")
        return value


class BankTransfer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transfer_id: Annotated[str, StringConstraints(pattern=r"^TRF-[0-9A-F]{12}$")]
    reference: Annotated[str, Field(min_length=1, max_length=35)]
    customer_id: Annotated[str, Field(min_length=1, max_length=35)]
    amount: Annotated[Decimal, Field(gt=0, le=1_000_000)]
    status: Literal["COMPLETED"]
    completed_at: datetime

    @field_validator("amount", mode="before")
    @classmethod
    def amount_must_be_json_number(cls, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Amount must be a JSON number.")
        return value

    @model_validator(mode="after")
    def completed_at_must_have_timezone(self):
        if self.completed_at.utcoffset() is None:
            raise ValueError("completed_at must include a timezone.")
        return self


async def wait_for_database():
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except (OperationalError, InterfaceError, SQLAlchemyTimeoutError, DBAPIError) as exc:
        logger.error("Payment database is unavailable", exc_info=True)
        raise APIError(
            503,
            "DEPENDENCY_UNAVAILABLE",
            "The payment database is unavailable.",
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception("Payment database query failed")
        raise APIError(
            500, "INTERNAL_ERROR", "An unexpected error occurred."
        ) from exc


async def payment_exists(claim_id: str) -> bool:
    await wait_for_database()
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text("SELECT EXISTS (SELECT 1 FROM payment.payments WHERE claim_id = :claim_id)"),
                {"claim_id": claim_id},
            )
            return result.scalar_one()
    except SQLAlchemyError as exc:
        if database_unavailable(exc):
            logger.error(
                "Payment database is unavailable",
                extra={"claim_id": claim_id},
                exc_info=True,
            )
            raise APIError(
                503,
                "DEPENDENCY_UNAVAILABLE",
                "The payment database is unavailable.",
            ) from exc
        logger.exception("Payment lookup failed", extra={"claim_id": claim_id})
        raise APIError(500, "INTERNAL_ERROR", "An unexpected error occurred.") from exc


async def call_bank(
    request: PaymentRequest,
) -> tuple[str, str | None, str | None]:
    try:
        async with asyncio.timeout(10):
            response = await app.state.bank_client.post(
                f"{BANK_URL}/transfers",
                json={
                    "reference": request.claim_id,
                    "customer_id": request.customer_id,
                    "amount": float(request.amount),
                },
            )
    except (httpx.TimeoutException, TimeoutError):
        logger.error("Bank request timed out", extra={"claim_id": request.claim_id})
        return "FAILED", None, "BANK_TIMEOUT"
    except httpx.RequestError:
        logger.error("Bank request failed", extra={"claim_id": request.claim_id})
        return "FAILED", None, "BANK_ERROR"

    if response.status_code != 201:
        logger.error(
            "Bank returned an unsuccessful status",
            extra={"claim_id": request.claim_id},
        )
        return "FAILED", None, "BANK_ERROR"

    try:
        transfer = BankTransfer.model_validate(response.json())
    except (ValueError, ValidationError) as exc:
        logger.error(
            "Bank returned an invalid transfer",
            extra={"claim_id": request.claim_id},
            exc_info=exc,
        )
        raise APIError(
            500,
            "INTERNAL_ERROR",
            "The bank returned an invalid transfer response.",
        ) from exc

    if (
        transfer.reference != request.claim_id
        or transfer.customer_id != request.customer_id
        or transfer.amount != request.amount
    ):
        logger.error(
            "Bank transfer did not match the request",
            extra={"claim_id": request.claim_id},
        )
        raise APIError(
            500,
            "INTERNAL_ERROR",
            "The bank returned an invalid transfer response.",
        )

    return "COMPLETED", transfer.transfer_id, None


def is_duplicate_claim(exc: IntegrityError) -> bool:
    original = exc.orig
    diagnostic = getattr(original, "diag", None)
    return (
        getattr(original, "sqlstate", None) == "23505"
        and getattr(diagnostic, "constraint_name", None) == "payments_claim_id_key"
    )


async def save_payment(request: PaymentRequest, status: str, failure_reason: str | None,
                       bank_transfer_id: str | None):
    query = text(
        """
        INSERT INTO payment.payments
            (claim_id, customer_id, amount, status, bank_transfer_id, failure_reason)
        VALUES
            (:claim_id, :customer_id, :amount, :status, :bank_transfer_id, :failure_reason)
        RETURNING payment_id, claim_id, customer_id, amount, status,
                  bank_transfer_id, failure_reason, created_at
        """
    )
    try:
        async with engine.begin() as connection:
            result = await connection.execute(
                query,
                {
                    "claim_id": request.claim_id,
                    "customer_id": request.customer_id,
                    "amount": request.amount,
                    "status": status,
                    "bank_transfer_id": bank_transfer_id,
                    "failure_reason": failure_reason,
                },
            )
            return result.mappings().one()
    except IntegrityError as exc:
        if is_duplicate_claim(exc):
            raise APIError(
                409,
                "DUPLICATE_PAYMENT",
                "A payment already exists for this claim.",
            ) from exc
        logger.exception("Payment could not be saved", extra={"claim_id": request.claim_id})
        raise APIError(500, "INTERNAL_ERROR", "An unexpected error occurred.") from exc
    except SQLAlchemyError as exc:
        if database_unavailable(exc):
            logger.error(
                "Payment database is unavailable",
                extra={"claim_id": request.claim_id},
                exc_info=True,
            )
            raise APIError(
                503,
                "DEPENDENCY_UNAVAILABLE",
                "The payment database is unavailable.",
            ) from exc
        logger.exception("Payment save failed", extra={"claim_id": request.claim_id})
        raise APIError(500, "INTERNAL_ERROR", "An unexpected error occurred.") from exc


def money_number(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def payment_response(row):
    created_at = row["created_at"].astimezone(timezone.utc)
    return {
        "payment_id": row["payment_id"],
        "claim_id": row["claim_id"],
        "customer_id": row["customer_id"],
        "amount": money_number(row["amount"]),
        "status": row["status"],
        "bank_transfer_id": row["bank_transfer_id"],
        "failure_reason": row["failure_reason"],
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
    }


@app.get("/health")
async def health():
    return {
        "service": SERVICE_NAME,
        "instance": socket.gethostname(),
        "status": "ok",
    }


@app.post("/payments", status_code=201)
async def create_payment(request: PaymentRequest):
    lock_key = f"payment-lock:{request.claim_id}"
    lock_acquired = False
    try:
        try:
            lock_result = await app.state.redis_client.set(
                lock_key,
                socket.gethostname(),
                nx=True,
                ex=30,
            )
        except (RedisError, asyncio.TimeoutError):
            logger.warning(
                "Redis is unavailable; processing payment without a lock",
                extra={"claim_id": request.claim_id},
            )
        else:
            if lock_result is None:
                raise APIError(
                    409,
                    "DUPLICATE_PAYMENT",
                    "A payment already exists for this claim.",
                )
            lock_acquired = True

        if await payment_exists(request.claim_id):
            raise APIError(
                409,
                "DUPLICATE_PAYMENT",
                "A payment already exists for this claim.",
            )

        status, bank_transfer_id, failure_reason = await call_bank(request)
        row = await save_payment(request, status, failure_reason, bank_transfer_id)
        logger.info(
            "Payment attempt saved",
            extra={
                "claim_id": request.claim_id,
                "customer_id": request.customer_id,
                "payment_id": row["payment_id"],
            },
        )
        return payment_response(row)
    finally:
        if lock_acquired:
            try:
                await app.state.redis_client.delete(lock_key)
            except (RedisError, asyncio.TimeoutError):
                logger.warning(
                    "Redis lock could not be deleted; it will expire automatically",
                    extra={"claim_id": request.claim_id},
                )


@app.get("/payments/{payment_id}")
async def get_payment(payment_id: str = Path(pattern=PAYMENT_ID)):
    await wait_for_database()
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT payment_id, claim_id, customer_id, amount, status,
                           bank_transfer_id, failure_reason, created_at
                    FROM payment.payments
                    WHERE payment_id = :payment_id
                    """
                ),
                {"payment_id": payment_id},
            )
            row = result.mappings().one_or_none()
    except SQLAlchemyError as exc:
        if database_unavailable(exc):
            logger.error(
                "Payment database is unavailable",
                extra={"payment_id": payment_id},
                exc_info=True,
            )
            raise APIError(
                503,
                "DEPENDENCY_UNAVAILABLE",
                "The payment database is unavailable.",
            ) from exc
        logger.exception("Payment lookup failed", extra={"payment_id": payment_id})
        raise APIError(500, "INTERNAL_ERROR", "An unexpected error occurred.") from exc

    if row is None:
        raise APIError(404, "NOT_FOUND", "The requested payment was not found.")
    return payment_response(row)
