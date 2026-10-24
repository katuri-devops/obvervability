"""frontend: the claims web pages. A customer submits a claim, and sees its status.

What it serves is fixed by contracts/ui/frontend/frontend.yaml. It has no
database: it sends each claim to claims-api, as contracts/api/claims-api/claims-api.yaml
describes, and shows claims-api's answer.

Addresses: GET /health, GET / (the claim page), POST /claims (the claim form),
GET /claims?claim_id=... (the look-up form) and GET /claims/{claim_id} (the
status page). Under /static/: the CSS, the page's one script, and the
aiiqinsure studio brand images (the AIIQ Labs logo and the favicon), all served
from this container: nothing is loaded from the internet.
"""

import hashlib
import json
import logging
import math
import os
import re
import socket
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from starlette.exceptions import HTTPException as StarletteHTTPException

SERVICE_NAME = os.environ.get("SERVICE_NAME", "frontend")
CLAIMS_API_URL = os.environ["CLAIMS_API_URL"].rstrip("/")
INSTANCE = socket.gethostname()  # the container's host name

# From frontend.yaml: wait at most 30 seconds for a new claim, because
# claims-api itself may wait for the bank, and 5 seconds for a status page.
SUBMIT_TIMEOUT_SECONDS = 30.0
STATUS_TIMEOUT_SECONDS = 5.0

HERE = Path(__file__).parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(SERVICE_NAME)


def today_in_utc() -> date:
    return datetime.now(timezone.utc).date()


# ---------------------------------------------------------------------------
# The rules of the forms, written once. The server checks them on every
# submit; the page's script gets the same rules through data-* attributes
# (see rule_attributes below), so the browser and the server cannot disagree.
# Each rule has one message, word for word from frontend.yaml.
# Patterns are written without ^ and $: both sides match the whole value.
# ---------------------------------------------------------------------------

CLAIM_RULES = {
    "customer_id": {
        "pattern": r"C-[0-9]{4}",
        "message": "Enter your customer ID: the letter C, a dash and four digits, for example C-1001.",
    },
    "policy_id": {
        "pattern": r"P-[0-9]{4}",
        "message": "Enter the policy ID: the letter P, a dash and four digits, for example P-2001.",
    },
    "incident_date": {
        "pattern": r"[0-9]{4}-[0-9]{2}-[0-9]{2}",
        "date": True,             # a day that exists
        "not_after_today": True,  # today in UTC
        "message": "Enter the date of the incident. It cannot be in the future.",
    },
    "description": {
        "min_length": 10,
        "max_length": 1000,
        "message": "Describe what happened in 10 to 1000 characters.",
    },
    "amount_claimed": {
        # Digits, at most one decimal point, at most two decimals. No minus
        # sign and no digit grouping: an amount is never negative.
        "pattern": r"[0-9]+(\.[0-9]{0,2})?",
        "decimals": 2,
        "more_than": 0,
        "max": 1000000,
        "message": "Enter the amount claimed: more than 0 and at most 1000000, with at most two decimals.",
    },
}
CLAIM_FIELDS = list(CLAIM_RULES)

LOOKUP_RULES = {
    "claim_id": {
        "pattern": r"CLM-[0-9]{6,}",
        "message": "Enter a claim ID: CLM, a dash and at least six digits, for example CLM-100001.",
    },
}


def follows_rule(value: str, rule: dict) -> bool:
    """True if the value follows its rule. The page's script checks exactly the same things."""
    if "pattern" in rule and not re.fullmatch(rule["pattern"], value):
        return False
    if "min_length" in rule and len(value) < rule["min_length"]:
        return False
    if "max_length" in rule and len(value) > rule["max_length"]:
        return False
    if rule.get("date"):
        try:
            day = date.fromisoformat(value)
        except ValueError:  # for example 2026-02-30
            return False
        if rule.get("not_after_today") and day > today_in_utc():
            return False
    if "more_than" in rule:
        amount = Decimal(value)  # the pattern above makes sure this works
        if amount <= rule["more_than"] or amount > rule["max"]:
            return False
    return True


def check_fields(values: dict, rules: dict) -> dict:
    """Returns {field: message} for every field that breaks its rule; empty when all are right."""
    return {name: rule["message"] for name, rule in rules.items() if not follows_rule(values[name], rule)}


def rule_attributes(rule: dict) -> Markup:
    """The rule as data-* attributes, for the page's script."""
    attributes = {"data-message": rule["message"]}
    if "pattern" in rule:
        attributes["data-pattern"] = rule["pattern"]
    if "min_length" in rule:
        attributes["data-min-length"] = rule["min_length"]
    if "max_length" in rule:
        attributes["data-max-length"] = rule["max_length"]
    if rule.get("date"):
        attributes["data-date"] = "day"
    if rule.get("not_after_today"):
        attributes["data-not-after"] = today_in_utc().isoformat()
    if "more_than" in rule:
        attributes["data-more-than"] = rule["more_than"]
        attributes["data-max"] = rule["max"]
        attributes["data-decimals"] = rule["decimals"]
    return Markup(" ".join(f'{name}="{escape(value)}"' for name, value in attributes.items()))


# ---------------------------------------------------------------------------
# The texts of the status page, word for word from frontend.yaml.
# ---------------------------------------------------------------------------

STATUS_NAMES = {
    "PAID": "Paid",
    "UNDER_REVIEW": "Under review",
    "REJECTED": "Rejected",
    "PAYMENT_FAILED": "Payment failed",
    "APPROVED": "Approved",
}
REJECTION_REASONS = {
    "CUSTOMER_NOT_ACTIVE": "Your customer account is not active.",
    "POLICY_NOT_ACTIVE": "The policy is not active.",
    "INCIDENT_OUTSIDE_POLICY_PERIOD": "The incident date is outside the period the policy covers.",
    "BELOW_DEDUCTIBLE": "The amount claimed is not more than the policy's deductible.",
}
FORM_NOT_SENT = "Your claim could not be sent. Nothing was saved. Please try again in a moment."
NO_SUCH_CLAIM = "There is no claim with this ID."
CLAIM_NOT_SHOWN = "The claim cannot be shown right now. Please try again in a moment."


# How the status page presents each status: the colour family ("tone") of its
# badge, its icon, the steps of the progress tracker, the note under the
# approved amount, and what happens next.
# frontend.yaml fixes only the texts above; this part is the look.
STATUS_LOOK = {
    "PAID": {
        "tone": "success",
        "icon": "check-circle",
        "steps": [("Submitted", "done"), ("Checked", "done"), ("Approved", "done"), ("Paid", "done")],
        "amount_note": "Paid to your bank account",
        "next": "Nothing more to do. Keep your claim ID for your records.",
    },
    "APPROVED": {
        "tone": "info",
        "icon": "clock",
        "steps": [("Submitted", "done"), ("Checked", "done"), ("Approved", "done"), ("Paying", "current")],
        "amount_note": "Being paid to your bank account",
        "next": "The payment is being made. Look at this page again in a moment.",
    },
    "UNDER_REVIEW": {
        "tone": "warning",
        "icon": "clock",
        "steps": [("Submitted", "done"), ("Checked", "done"), ("In review", "current"), ("Decision", "upcoming")],
        "amount_note": "To be confirmed by a claims handler",
        "next": "A claims handler will look at your claim and write to you. You do not need to do anything now.",
    },
    "REJECTED": {
        "tone": "danger",
        "icon": "x-circle",
        "steps": [("Submitted", "done"), ("Checked", "done"), ("Rejected", "failed")],
        "amount_note": "Nothing was approved",
        "next": "If something in your claim was not right, you can submit a new claim with the right details.",
    },
    "PAYMENT_FAILED": {
        "tone": "danger",
        "icon": "alert-circle",
        "steps": [("Submitted", "done"), ("Checked", "done"), ("Approved", "done"), ("Payment failed", "failed")],
        "amount_note": "Approved, but not paid yet",
        "next": "We will contact you about the payment. You do not need to submit this claim again.",
    },
}


def money(amount) -> str:
    """Money with two decimals, for example 700.00."""
    return str(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def readable_day(text: str) -> str:
    """2026-10-24 as 24 Oct 2026. Anything else is shown as it is."""
    try:
        day = date.fromisoformat(text)
    except ValueError:
        return text
    return f"{day.day} {day:%b %Y}"


def readable_time(text: str) -> str:
    """2026-10-24T05:00:01Z as 24 Oct 2026, 05:00 UTC. Anything else is shown as it is."""
    try:
        moment = datetime.fromisoformat(text).astimezone(timezone.utc)
    except ValueError:
        return text
    return f"{moment.day} {moment:%b %Y, %H:%M} UTC"


def short_time(text: str) -> str:
    """2026-10-24T05:00:01Z as 24 Oct, 05:00: the time under a step of the tracker."""
    try:
        moment = datetime.fromisoformat(text).astimezone(timezone.utc)
    except ValueError:
        return ""
    return f"{moment.day} {moment:%b, %H:%M}"


def tracker_steps(claim: dict, look: dict) -> list[dict]:
    """The steps of the progress tracker. The first shows when the claim was
    submitted; the last one reached shows when it last changed."""
    steps = [{"label": label, "state": state, "time": ""} for label, state in look["steps"]]
    steps[0]["time"] = short_time(claim["created_at"])
    reached = [step for step in steps if step["state"] != "upcoming"]
    if len(reached) > 1:
        reached[-1]["time"] = short_time(claim["updated_at"])
    return steps


def amount_breakdown(claim: dict) -> dict:
    """The bar under the amounts: how much of the amount claimed was approved."""
    claimed = Decimal(str(claim["amount_claimed"]))
    approved = None if claim["approved_amount"] is None else Decimal(str(claim["approved_amount"]))
    if approved is None or claimed <= 0:
        return {"approved": None, "rest": money(claimed), "percent": 0}
    approved = min(approved, claimed)
    return {
        "approved": money(approved),
        "rest": money(claimed - approved),
        "percent": float((approved / claimed * 100).quantize(Decimal("0.1"))),
    }


def barcode(text: str) -> dict:
    """A decorative barcode for the receipt, drawn from the claim ID: the same ID
    always gives the same bars. It encodes nothing."""
    bars, x = [], 0
    for character in text:
        code = ord(character)
        for bit in range(5):
            width = 1 + (code >> bit) % 3
            if bit % 2 == 0:
                bars.append({"x": x, "w": width})
            x += width + 1
    return {"bars": bars, "width": x}


def confetti() -> list[dict]:
    """The pieces of the burst around a paid claim's badge: evenly around a
    circle, at varied distances, in the brand's colours."""
    colours = ["#c8860d", "#ffb24d", "#17b26a", "#3349c4", "#ff8a1f", "#f0d28c"]
    pieces = []
    for i in range(18):
        angle = 2 * math.pi * i / 18 + (0.35 if i % 2 else 0)
        distance = 70 + (i * 37) % 60
        pieces.append({
            "x": round(math.cos(angle) * distance * 1.6),
            "y": round(math.sin(angle) * distance),
            "turn": (i * 83) % 360 + 180,
            "colour": colours[i % len(colours)],
            "delay": (i % 6) * 25,
            "wide": i % 3 == 0,
        })
    return pieces


def status_text(claim: dict) -> str:
    """The sentence shown under the claim's status."""
    status = claim["status"]
    if status == "PAID":
        return f"We have sent {money(claim['approved_amount'])} to your bank account."
    if status == "UNDER_REVIEW":
        return "A claims handler will review your claim, because the amount to pay is above 5000."
    if status == "REJECTED":
        return REJECTION_REASONS.get(claim["reason"], "")
    if status == "PAYMENT_FAILED":
        return "Your claim is approved, but the payment could not be made. We will contact you."
    return "The payment is on its way."  # APPROVED


# ---------------------------------------------------------------------------
# Start-up, and the pages
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # One client for every call to claims-api: created here, at start-up, and
    # reused. Each call sets the contract's timeout.
    async with httpx.AsyncClient() as client:
        app.state.http_client = client
        yield


app = FastAPI(
    title=SERVICE_NAME,
    lifespan=lifespan,
    # No /docs page: frontend.yaml lists only the pages.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    redirect_slashes=False,
)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals["rule_attributes"] = rule_attributes


def static_fingerprint() -> str:
    """A short fingerprint of everything under static/. The pages add it to the
    address of the CSS, the script and the images (?v=...), so it changes with
    every new version: a browser never mixes a new page with an old file it kept."""
    digest = hashlib.sha256()
    for path in sorted((HERE / "static").rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(HERE).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


templates.env.globals["asset_version"] = static_fingerprint()


def claim_page(request: Request, status_code: int = 200, values: dict | None = None,
               errors: dict | None = None, form_message: str = "", lookup_value: str = "",
               lookup_error: str = ""):
    """The claim page: the claim form and, below it, the look-up form."""
    errors = errors or {}
    return templates.TemplateResponse(
        request,
        "claim.html",
        {
            "values": values or {name: "" for name in CLAIM_FIELDS},
            "errors": errors,
            # The first wrong field gets the focus when the page opens.
            "first_error": next((name for name in CLAIM_FIELDS if name in errors), None),
            "form_message": form_message,
            "claim_rules": CLAIM_RULES,
            "lookup_rule": LOOKUP_RULES["claim_id"],
            "lookup_value": lookup_value,
            "lookup_error": lookup_error,
            "today": today_in_utc().isoformat(),
        },
        status_code=status_code,
    )


def message_page(request: Request, status_code: int, kind: str, title: str, text: str,
                 headers: dict | None = None):
    """A page with one message and a way back to /: for 404, 405, 503 and 500.

    kind picks the picture: "not-found", "not-allowed", "unavailable" or "error".
    """
    return templates.TemplateResponse(
        request, "message.html", {"kind": kind, "code": status_code, "title": title, "text": text},
        status_code=status_code, headers=headers,
    )


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    """404 for an unknown address, 405 for a wrong HTTP method, each with a page in the same layout."""
    if exc.status_code == 405:
        # exc.headers carries the "Allow" header, which lists the methods that do work.
        return message_page(request, 405, "not-allowed", "Not allowed",
                            f"This address does not accept the HTTP method {request.method}.", exc.headers)
    if exc.status_code == 404:
        return message_page(request, 404, "not-found", "Page not found", "There is no page at this address.")
    logger.error("Unexpected HTTP error %s on %s", exc.status_code, request.url.path)
    return message_page(request, 500, "error", "Something went wrong", "Something went wrong. Please try again.")


@app.exception_handler(Exception)
async def internal_error(request: Request, exc: Exception):
    """500: any other, unexpected error. Uvicorn writes the full error to the log."""
    return message_page(request, 500, "error", "Something went wrong", "Something went wrong. Please try again.")


@app.get("/health")
async def health() -> dict:
    """Never checks claims-api: a copy still answers when claims-api is down."""
    return {"service": SERVICE_NAME, "instance": INSTANCE, "status": "ok"}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Browsers ask for /favicon.ico by themselves; it is the aiiq icon."""
    return FileResponse(HERE / "static" / "favicon.ico", media_type="image/x-icon")


@app.get("/")
async def show_claim_page(request: Request):
    return claim_page(request)


@app.post("/claims")
async def submit_claim(request: Request):
    form = await request.form()
    # The values as typed, kept for the page if there is an error. Unknown
    # fields are ignored. A browser sends line breaks as \r\n; the page's
    # script counts them as one character, and so does this line.
    values = {name: str(form.get(name, "")).replace("\r\n", "\n") for name in CLAIM_FIELDS}

    # 1. Check the five fields. If any is wrong, nothing is sent to claims-api.
    errors = check_fields(values, CLAIM_RULES)
    if errors:
        logger.info("Claim form refused: %s", ", ".join(errors))
        return claim_page(request, 422, values, errors)

    # 2. Send the claim to claims-api, with amount_claimed as a JSON number.
    ids = {"customer_id": values["customer_id"], "policy_id": values["policy_id"]}
    claim = {**values, "amount_claimed": float(Decimal(values["amount_claimed"]))}
    try:
        answer = await request.app.state.http_client.post(
            f"{CLAIMS_API_URL}/claims", json=claim, timeout=SUBMIT_TIMEOUT_SECONDS
        )
    except httpx.TimeoutException:
        logger.error("claims-api did not answer within %g seconds", SUBMIT_TIMEOUT_SECONDS, extra=ids)
        return claim_page(request, 503, values, form_message=FORM_NOT_SENT)
    except httpx.RequestError as error:
        logger.error("claims-api cannot be reached (%s: %s)", type(error).__name__, error, extra=ids)
        return claim_page(request, 503, values, form_message=FORM_NOT_SENT)

    # 3. 201: go to the claim's status page.
    if answer.status_code == 201:
        claim_id = read_json(answer).get("claim_id")
        if isinstance(claim_id, str) and follows_rule(claim_id, LOOKUP_RULES["claim_id"]):
            logger.info("Claim %s submitted", claim_id, extra={**ids, "claim_id": claim_id})
            return RedirectResponse(f"/claims/{claim_id}", status_code=303)

    # 4. 422 naming one or more of the five fields: claims-api's message beside each.
    if answer.status_code == 422:
        named = read_json(answer).get("error", {}).get("fields", {})
        errors = {name: message for name, message in named.items()
                  if name in CLAIM_RULES and isinstance(message, str)}
        if errors:
            logger.info("claims-api refused the claim: %s", ", ".join(errors), extra=ids)
            return claim_page(request, 422, values, errors)

    # 5. Anything else, including Traefik's own "404 page not found".
    logger.error("claims-api answered with HTTP status %s", answer.status_code, extra=ids)
    return claim_page(request, 503, values, form_message=FORM_NOT_SENT)


def read_json(answer: httpx.Response) -> dict:
    """The answer as a JSON object, or {} if it is not one. Numbers become Decimal, never float."""
    try:
        body = json.loads(answer.text, parse_float=Decimal)
    except ValueError:  # for example Traefik's plain-text "404 page not found"
        return {}
    return body if isinstance(body, dict) else {}


@app.get("/claims")
async def look_up_claim(request: Request, claim_id: str = ""):
    errors = check_fields({"claim_id": claim_id}, LOOKUP_RULES)
    if errors:
        return claim_page(request, 422, lookup_value=claim_id, lookup_error=errors["claim_id"])
    return RedirectResponse(f"/claims/{claim_id}", status_code=303)


@app.get("/claims/{claim_id}")
async def show_status_page(request: Request, claim_id: str):
    if not follows_rule(claim_id, LOOKUP_RULES["claim_id"]):
        return message_page(request, 404, "not-found", "Claim not found", NO_SUCH_CLAIM)

    ids = {"claim_id": claim_id}
    try:
        answer = await request.app.state.http_client.get(
            f"{CLAIMS_API_URL}/claims/{claim_id}", timeout=STATUS_TIMEOUT_SECONDS
        )
    except httpx.TimeoutException:
        logger.error("claims-api did not answer within %g seconds", STATUS_TIMEOUT_SECONDS, extra=ids)
        return message_page(request, 503, "unavailable", "Claim not available", CLAIM_NOT_SHOWN)
    except httpx.RequestError as error:
        logger.error("claims-api cannot be reached (%s: %s)", type(error).__name__, error, extra=ids)
        return message_page(request, 503, "unavailable", "Claim not available", CLAIM_NOT_SHOWN)

    claim = read_json(answer)
    if answer.status_code == 404 and claim.get("error", {}).get("code") == "NOT_FOUND":
        return message_page(request, 404, "not-found", "Claim not found", NO_SUCH_CLAIM)
    if answer.status_code != 200 or not is_claim(claim, claim_id):
        # Anything else, including Traefik's own "404 page not found".
        logger.error("claims-api answered with HTTP status %s", answer.status_code, extra=ids)
        return message_page(request, 503, "unavailable", "Claim not available", CLAIM_NOT_SHOWN)

    look = STATUS_LOOK[claim["status"]]
    return templates.TemplateResponse(
        request,
        "status.html",
        {
            "claim": claim,
            "status_name": STATUS_NAMES[claim["status"]],
            "status_text": status_text(claim),
            "look": look,
            "steps": tracker_steps(claim, look),
            "breakdown": amount_breakdown(claim),
            "barcode": barcode(claim["claim_id"]),
            "confetti": confetti() if claim["status"] == "PAID" else [],
            "amount_claimed": money(claim["amount_claimed"]),
            "approved_amount": None if claim["approved_amount"] is None else money(claim["approved_amount"]),
            "incident_day": readable_day(claim["incident_date"]),
            "submitted_at": readable_time(claim["created_at"]),
            "updated_at": readable_time(claim["updated_at"]),
        },
    )


def is_claim(claim: dict, claim_id: str) -> bool:
    """True if claims-api's answer has what the status page shows, for this claim."""
    try:
        return (
            claim["claim_id"] == claim_id
            and claim["status"] in STATUS_NAMES
            and isinstance(claim["customer_id"], str)
            and isinstance(claim["policy_id"], str)
            and isinstance(claim["created_at"], str)
            and isinstance(claim["updated_at"], str)
            and (claim["payment_id"] is None or isinstance(claim["payment_id"], str))
            and isinstance(claim["incident_date"], str)
            and isinstance(claim["description"], str)
            and isinstance(claim["amount_claimed"], (int, Decimal))
            and (claim["approved_amount"] is None or isinstance(claim["approved_amount"], (int, Decimal)))
            and (claim["status"] != "PAID" or claim["approved_amount"] is not None)
        )
    except (KeyError, TypeError):
        return False
