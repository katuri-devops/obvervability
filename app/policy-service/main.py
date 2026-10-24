import asyncio
import json
import logging
import os
import re
import socket
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Annotated

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.exceptions import HTTPException as StarletteHTTPException

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("policy-service")

SERVICE_NAME = os.getenv("SERVICE_NAME", "policy-service")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
POLICY_ID_PATTERN = re.compile(r"^P-[0-9]{4}$")
engine = create_async_engine(os.environ["DATABASE_URL"])
redis_client = None


class APIError(Exception):
    def __init__(self, status_code: int, code: str, message: str, fields=None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.fields = fields


def error_response(status_code: int, code: str, message: str, fields=None):
    error = {"code": code, "message": message}
    if fields is not None:
        error["fields"] = fields
    return JSONResponse(status_code=status_code, content={"error": error})


async def wait_for_database():
    while True:
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            logger.info("Database connection is ready")
            return
        except SQLAlchemyError:
            logger.warning("Database is not ready; retrying in 2 seconds")
            await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global redis_client
    redis_client = Redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=1,
        retry=Retry(NoBackoff(), 0),
    )
    retry_task = asyncio.create_task(wait_for_database())
    try:
        yield
    finally:
        retry_task.cancel()
        await redis_client.aclose()
        redis_client = None
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
    for item in exc.errors():
        location = item.get("loc", ())
        field = str(location[-1]) if len(location) > 1 else "request_body"
        error_type = item.get("type", "")

        if error_type == "extra_forbidden":
            message = "This field is not allowed."
        elif error_type == "missing":
            message = "This field is required."
        elif error_type in {"greater_than", "greater_than_equal"}:
            message = "Enter an amount greater than 0."
        elif error_type == "decimal_places":
            message = "Enter an amount with at most two decimal places."
        elif field == "incident_date":
            message = "Enter a valid date in YYYY-MM-DD format."
        else:
            message = "Enter a valid value."

        fields[field] = message

    return error_response(
        422, "VALIDATION_FAILED", "Some fields are not valid.", fields
    )


@app.exception_handler(StarletteHTTPException)
async def handle_http_error(_: Request, exc: StarletteHTTPException):
    if exc.status_code == 405:
        return error_response(
            405, "METHOD_NOT_ALLOWED", "This method is not allowed for this address."
        )
    return error_response(404, "NOT_FOUND", "The requested address was not found.")


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception):
    logger.exception(
        "Unexpected error",
        extra={"policy_id": request.path_params.get("policy_id")},
    )
    return error_response(500, "INTERNAL_ERROR", "An unexpected error occurred.")


class CoverageCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: Annotated[str, StringConstraints(pattern=r"^C-[0-9]{4}$")]
    incident_date: date
    amount_claimed: Annotated[Decimal, Field(gt=0)]

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


@app.get("/health")
async def health():
    return {
        "service": SERVICE_NAME,
        "instance": socket.gethostname(),
        "status": "ok",
    }


async def read_policy(policy_id: str):
    if not POLICY_ID_PATTERN.fullmatch(policy_id):
        raise APIError(
            422,
            "VALIDATION_FAILED",
            "Some fields are not valid.",
            {"policy_id": "Enter a valid policy ID, such as P-2001."},
        )

    cache_key = f"policy:{policy_id}"
    cache_available = True
    try:
        cached_policy = await redis_client.get(cache_key)
    except (RedisError, asyncio.TimeoutError):
        cache_available = False
        logger.warning(
            "Redis is unavailable; reading policy from database",
            extra={"policy_id": policy_id},
        )
        cached_policy = None

    if cached_policy is not None:
        try:
            cached_data = json.loads(cached_policy)
            if not isinstance(cached_data, dict):
                raise ValueError("Cached policy is not an object")
            row = {
                "policy_id": cached_data["policy_id"],
                "customer_id": cached_data["customer_id"],
                "policy_type": cached_data["policy_type"],
                "status": cached_data["status"],
                "coverage_limit": Decimal(str(cached_data["coverage_limit"])),
                "deductible": Decimal(str(cached_data["deductible"])),
                "start_date": date.fromisoformat(cached_data["start_date"]),
                "end_date": date.fromisoformat(cached_data["end_date"]),
            }
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, InvalidOperation):
            cache_available = False
            logger.warning(
                "Redis policy entry is invalid; reading policy from database",
                extra={"policy_id": policy_id},
            )
        else:
            return row, "cache"

    query = text(
        """
        SELECT policy_id, customer_id, policy_type, status,
               coverage_limit, deductible, start_date, end_date
        FROM policy.policies
        WHERE policy_id = :policy_id
        """
    )

    try:
        async with engine.connect() as connection:
            result = await connection.execute(query, {"policy_id": policy_id})
            row = result.mappings().one_or_none()
    except SQLAlchemyError as exc:
        logger.error(
            "Database query failed",
            extra={"policy_id": policy_id},
            exc_info=True,
        )
        raise APIError(
            503, "DEPENDENCY_UNAVAILABLE", "The database is unavailable."
        ) from exc

    if row is None:
        raise APIError(404, "NOT_FOUND", "The requested policy was not found.")

    policy_data = {
        "policy_id": row["policy_id"],
        "customer_id": row["customer_id"],
        "policy_type": row["policy_type"],
        "status": row["status"],
        "coverage_limit": money_number(row["coverage_limit"]),
        "deductible": money_number(row["deductible"]),
        "start_date": row["start_date"].isoformat(),
        "end_date": row["end_date"].isoformat(),
    }
    if cache_available:
        try:
            await redis_client.set(cache_key, json.dumps(policy_data), ex=60)
        except (RedisError, asyncio.TimeoutError):
            logger.warning(
                "Redis is unavailable; policy was not cached",
                extra={"policy_id": policy_id},
            )

    return row, "database"


def money_number(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@app.get("/policies/{policy_id}")
async def get_policy(policy_id: str):
    row, source = await read_policy(policy_id)
    return {
        "policy_id": row["policy_id"],
        "customer_id": row["customer_id"],
        "policy_type": row["policy_type"],
        "status": row["status"],
        "coverage_limit": money_number(row["coverage_limit"]),
        "deductible": money_number(row["deductible"]),
        "start_date": row["start_date"],
        "end_date": row["end_date"],
        "source": source,
    }


@app.post("/policies/{policy_id}/coverage-check")
async def check_coverage(policy_id: str, request: CoverageCheckRequest):
    row, _ = await read_policy(policy_id)

    reason = None
    payable = Decimal("0.00")

    if request.customer_id != row["customer_id"]:
        reason = "POLICY_NOT_OWNED"
    elif row["status"] != "ACTIVE":
        reason = "POLICY_NOT_ACTIVE"
    elif not row["start_date"] <= request.incident_date <= row["end_date"]:
        reason = "INCIDENT_OUTSIDE_POLICY_PERIOD"
    else:
        payable = min(request.amount_claimed, row["coverage_limit"])
        payable = (payable - row["deductible"]).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if payable <= 0:
            reason = "BELOW_DEDUCTIBLE"

    return {
        "policy_id": row["policy_id"],
        "covered": reason is None,
        "reason": reason,
        "coverage_limit": money_number(row["coverage_limit"]),
        "deductible": money_number(row["deductible"]),
        "payable_amount": money_number(payable) if reason is None else 0.0,
    }
