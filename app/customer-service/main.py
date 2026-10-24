import asyncio
import json
import logging
import os
import socket
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Path, Request
from fastapi.exceptions import RequestValidationError
from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry
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
logger = logging.getLogger(__name__)

SERVICE_NAME = os.getenv("SERVICE_NAME", "customer-service")
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://claims:claims-dev-password@postgres:5432/claims"
    "?application_name=customer-service",
)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
engine = create_async_engine(DATABASE_URL)
redis_client = None


async def retry_database_connection():
    while True:
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return
        except SQLAlchemyError:
            logger.warning("PostgreSQL is not ready; retrying in 2 seconds")
            await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client
    redis_client = Redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=1,
        retry=Retry(NoBackoff(), 0),
    )
    retry_task = asyncio.create_task(retry_database_connection())
    yield
    retry_task.cancel()
    with suppress(asyncio.CancelledError):
        await retry_task
    await redis_client.aclose()
    redis_client = None
    await engine.dispose()


app = FastAPI(lifespan=lifespan)


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
        if field == "customer_id":
            fields[field] = "Enter a customer ID in the format C-1234."
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
    logger.error(
        "Unexpected request error",
        extra={"customer_id": request.path_params.get("customer_id")},
    )
    return error_response(500, "INTERNAL_ERROR", "An unexpected error occurred.")


@app.get("/health")
async def health():
    return {
        "service": SERVICE_NAME,
        "instance": socket.gethostname(),
        "status": "ok",
    }


@app.get("/customers/{customer_id}")
async def get_customer(customer_id: str = Path(pattern=r"^C-[0-9]{4}$")):
    cache_key = f"customer:{customer_id}"
    cache_available = True
    try:
        cached_customer = await redis_client.get(cache_key)
    except (RedisError, asyncio.TimeoutError):
        cache_available = False
        logger.warning(
            "Redis is unavailable; reading customer from database",
            extra={"customer_id": customer_id},
        )
        cached_customer = None

    if cached_customer is not None:
        try:
            customer_data = json.loads(cached_customer)
            if not isinstance(customer_data, dict):
                raise ValueError("Cached customer is not an object")
        except (json.JSONDecodeError, ValueError):
            cache_available = False
            logger.warning(
                "Redis customer entry is invalid; reading customer from database",
                extra={"customer_id": customer_id},
            )
        else:
            customer_data["source"] = "cache"
            return customer_data

    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT customer_id, full_name, email, status "
                    "FROM customer.customers WHERE customer_id = :customer_id"
                ),
                {"customer_id": customer_id},
            )
            customer = result.mappings().one_or_none()
    except (OperationalError, InterfaceError, SQLAlchemyTimeoutError):
        logger.error(
            "Customer database is unavailable",
            extra={"customer_id": customer_id},
        )
        return error_response(
            503,
            "DEPENDENCY_UNAVAILABLE",
            "The customer database is unavailable.",
        )
    except DBAPIError as exc:
        if exc.connection_invalidated:
            logger.error(
                "Customer database connection was lost",
                extra={"customer_id": customer_id},
            )
            return error_response(
                503,
                "DEPENDENCY_UNAVAILABLE",
                "The customer database is unavailable.",
            )
        logger.error("Customer lookup failed", extra={"customer_id": customer_id})
        return error_response(500, "INTERNAL_ERROR", "An unexpected error occurred.")
    except SQLAlchemyError:
        logger.error("Customer lookup failed", extra={"customer_id": customer_id})
        return error_response(500, "INTERNAL_ERROR", "An unexpected error occurred.")

    if customer is None:
        return error_response(404, "NOT_FOUND", "The requested customer was not found.")

    logger.info("Customer found", extra={"customer_id": customer_id})
    customer_data = {
        "customer_id": customer["customer_id"],
        "full_name": customer["full_name"],
        "email": customer["email"],
        "status": customer["status"],
    }
    if cache_available:
        try:
            await redis_client.set(cache_key, json.dumps(customer_data), ex=60)
        except (RedisError, asyncio.TimeoutError):
            logger.warning(
                "Redis is unavailable; customer was not cached",
                extra={"customer_id": customer_id},
            )
    return {**customer_data, "source": "database"}