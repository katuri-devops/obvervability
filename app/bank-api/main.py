"""bank-api: a stand-in for a real bank's API.

Given ready-made. It plays the part of an outside provider that pays approved
claims, so it follows its own contract, contracts/api/bank-api/bank-api.yaml,
and not the project's rules: its errors have their own body, and a request it
cannot accept answers 400. It has no database and keeps nothing. Every request
it accepts is a new transfer.
"""
import asyncio
import logging
import os
import random
import socket
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

SERVICE_NAME = os.environ.get("SERVICE_NAME", "bank-api")
INSTANCE = socket.gethostname()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(SERVICE_NAME)

app = FastAPI(title="bank-api", docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)


class TransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference: str = Field(min_length=1, max_length=35)
    customer_id: str = Field(min_length=1, max_length=35)
    amount: Decimal = Field(gt=0, le=1000000, decimal_places=2)

    @field_validator("reference", "customer_id", mode="before")
    @classmethod
    def must_be_text(cls, value):
        if not isinstance(value, str):
            raise ValueError("must be text")
        return value

    @field_validator("amount", mode="before")
    @classmethod
    def must_be_a_number(cls, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("must be a number")
        return Decimal(str(value))


def bank_error(status_code: int, error_code: str, error_message: str, headers=None) -> JSONResponse:
    return JSONResponse(status_code=status_code, headers=headers,
                        content={"error_code": error_code, "error_message": error_message})


def describe(problem: dict) -> str:
    """One sentence about the first problem in a request."""
    location = [str(part) for part in problem.get("loc", ()) if part != "body"]
    field = location[0] if location else ""
    kind = problem.get("type", "")
    if not field or kind in ("json_invalid", "model_attributes_type", "dict_type"):
        return "The request body must be a JSON object with reference, customer_id and amount."
    if kind == "missing":
        return f"{field} is required."
    if kind == "extra_forbidden":
        return f"{field} is not part of the request."
    if kind == "greater_than":
        return f"{field} must be greater than 0."
    if kind == "less_than_equal":
        return f"{field} must be at most 1000000."
    if kind == "decimal_max_places":
        return f"{field} must have at most two decimals."
    if kind in ("string_too_short", "string_too_long"):
        return f"{field} must be 1 to 35 characters long."
    if kind == "value_error":
        return f"{field} {problem['msg'].removeprefix('Value error, ')}."
    return f"{field} is not valid."


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, exc: RequestValidationError):
    problems = exc.errors()
    message = describe(problems[0]) if problems else "The request is not valid."
    return bank_error(400, "INVALID_REQUEST", message)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        return bank_error(404, "NOT_FOUND", "This address does not exist.")
    if exc.status_code == 405:
        return bank_error(405, "METHOD_NOT_ALLOWED",
                          f"This address does not accept the HTTP method {request.method}.",
                          headers=exc.headers)
    if exc.status_code < 500:
        return bank_error(400, "INVALID_REQUEST", "The request is not valid.")
    return bank_error(500, "TECHNICAL_ERROR", "The bank failed. Nothing was sent.")


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    logger.exception("Unexpected error")
    return bank_error(500, "TECHNICAL_ERROR", "The bank failed. Nothing was sent.")


@app.get("/health")
async def health():
    return {"service": SERVICE_NAME, "instance": INSTANCE, "status": "ok"}


@app.post("/transfers", status_code=201)
async def create_transfer(transfer: TransferRequest):
    # The time a real bank needs to process a transfer.
    await asyncio.sleep(random.uniform(0.1, 0.5))
    transfer_id = "TRF-" + uuid.uuid4().hex[:12].upper()
    completed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    logger.info("Transfer %s completed: %s to %s, reference %s",
                transfer_id, transfer.amount, transfer.customer_id, transfer.reference)
    return {
        "transfer_id": transfer_id,
        "reference": transfer.reference,
        "customer_id": transfer.customer_id,
        "amount": float(transfer.amount),
        "status": "COMPLETED",
        "completed_at": completed_at,
    }
 