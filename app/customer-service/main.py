import asyncio
import logging
import os
import socket
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Path, Request
from fastapi.exceptions import RequestValidationError
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
engine = create_async_engine(DATABASE_URL)


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
    retry_task = asyncio.create_task(retry_database_connection())
    yield
    retry_task.cancel()
    with suppress(asyncio.CancelledError):
        await retry_task
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
    return {
        "customer_id": customer["customer_id"],
        "full_name": customer["full_name"],
        "email": customer["email"],
        "status": customer["status"],
        "source": "database",
    }