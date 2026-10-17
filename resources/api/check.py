"""Checks customer-service against its contract, customer-api.yaml.

It calls the running service through Traefik and prints PASS or FAIL for
each check. It uses only Python's own libraries.

Run it on the claims-net network, as guide 08e shows. To check another
address, set the environment variable BASE_URL.
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://traefik:8000/customer-service")
results = []


def call(method, path):
    """Sends one request. Returns the HTTP status, the JSON body (or None) and the raw text."""
    request = urllib.request.Request(BASE_URL + path, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    except OSError as error:
        print(f"Cannot reach {BASE_URL}{path}: {getattr(error, 'reason', error)}")
        print("Is customer-service running? See guide 08d, step 4.")
        sys.exit(2)
    text = raw.decode("utf-8", errors="replace")
    try:
        body = json.loads(text)
    except ValueError:
        body = None
    return status, body, text


def shown(text):
    text = text.strip()
    return text if len(text) <= 300 else text[:300] + " ..."


def check(number, title, problems, text=None):
    passed = not problems
    results.append(passed)
    print(f"{'PASS' if passed else 'FAIL'}  {number}  {title}")
    for problem in problems:
        print(f"         - {problem}")
    if problems and text is not None:
        print(f"         The answer was: {shown(text)}")


def error_problems(status, body, want_status, want_code, field=None):
    """What is wrong with an error answer, compared with the Error schema in common.yaml."""
    problems = []
    if status != want_status:
        problems.append(f"expected HTTP {want_status}, got HTTP {status}")
    if body is None:
        problems.append("the answer is not JSON")
        return problems
    if isinstance(body, dict) and "detail" in body:
        problems.append('this is FastAPI\'s default answer {"detail": ...}; the contract needs '
                        'the error answer {"error": {...}} from common.yaml')
        return problems
    if not isinstance(body, dict) or set(body) != {"error"} or not isinstance(body["error"], dict):
        problems.append('expected exactly one field, "error", holding "code" and "message"')
        return problems
    error = body["error"]
    extra = sorted(set(error) - {"code", "message", "fields"})
    if extra:
        problems.append(f'"error" has fields the contract does not allow: {extra}')
    if error.get("code") != want_code:
        problems.append(f'expected code {want_code}, got {error.get("code")!r}')
    if not isinstance(error.get("message"), str) or not error.get("message"):
        problems.append('"message" is missing or empty')
    if field:
        fields = error.get("fields")
        if not isinstance(fields, dict) or field not in fields:
            problems.append(f'"fields" must name {field}, got {fields!r}')
    elif "fields" in error:
        problems.append('"fields" belongs only to VALIDATION_FAILED')
    return problems


def customer_problems(status, body, expected):
    """What is wrong with a customer answer, compared with the Customer schema."""
    problems = []
    if status != 200:
        problems.append(f"expected HTTP 200, got HTTP {status}")
    if not isinstance(body, dict):
        problems.append("the answer is not a JSON object")
        return problems
    allowed = {"customer_id", "full_name", "email", "status", "source"}
    if sorted(set(body) - allowed):
        problems.append(f"fields the contract does not allow: {sorted(set(body) - allowed)}")
    if sorted(allowed - set(body)):
        problems.append(f"missing fields: {sorted(allowed - set(body))}")
    for name, value in expected.items():
        if name in body and body[name] != value:
            problems.append(f"{name}: expected {value!r}, got {body[name]!r}")
    if "source" in body and body["source"] not in ("cache", "database"):
        problems.append(f'source: expected "cache" or "database", got {body["source"]!r}')
    return problems


print("Checking customer-service against customer-api.yaml")
print(f"Address: {BASE_URL}")
print()

# 1. The health answer
status, body, text = call("GET", "/health")
problems = []
if status != 200:
    problems.append(f"expected HTTP 200, got HTTP {status}")
if not isinstance(body, dict) or set(body) != {"service", "instance", "status"}:
    problems.append('expected exactly the fields "service", "instance" and "status"')
else:
    if body["service"] != "customer-service":
        problems.append(f'service: expected "customer-service", got {body["service"]!r}')
    if body["status"] != "ok":
        problems.append(f'status: expected "ok", got {body["status"]!r}')
    if not isinstance(body["instance"], str) or not body["instance"]:
        problems.append('"instance" must be the container\'s host name')
check(1, "GET /health answers 200 with service, instance and status ok", problems, text)

# 2. Both copies answer
instances = set()
for _ in range(6):
    _, body, _ = call("GET", "/health")
    if isinstance(body, dict) and body.get("instance"):
        instances.add(body["instance"])
problems = [] if len(instances) >= 2 else [
    f"expected 2 different instance values in 6 calls, got {len(instances)}: {sorted(instances)}; "
    "check replicas: 2 in app/compose.yaml"]
check(2, "Both copies of the service answer", problems)

# 3 and 4. Two customers
status, body, text = call("GET", "/customers/C-1001")
check(3, "GET /customers/C-1001 answers the customer",
      customer_problems(status, body, {"customer_id": "C-1001", "full_name": "Elena Markovic",
                                       "email": "elena.markovic@example.com", "status": "ACTIVE"}), text)

status, body, text = call("GET", "/customers/C-1009")
check(4, "GET /customers/C-1009 answers the suspended customer",
      customer_problems(status, body, {"customer_id": "C-1009", "full_name": "Amara Okafor",
                                       "email": "amara.okafor@example.com", "status": "SUSPENDED"}), text)

# 5 to 8. The error answers
status, body, text = call("GET", "/customers/C-9999")
check(5, "GET /customers/C-9999 answers 404 NOT_FOUND",
      error_problems(status, body, 404, "NOT_FOUND"), text)

status, body, text = call("GET", "/customers/1001")
check(6, "GET /customers/1001 answers 422 VALIDATION_FAILED for customer_id",
      error_problems(status, body, 422, "VALIDATION_FAILED", field="customer_id"), text)

status, body, text = call("GET", "/nothing-here")
check(7, "GET /nothing-here answers 404 NOT_FOUND",
      error_problems(status, body, 404, "NOT_FOUND"), text)

status, body, text = call("POST", "/customers/C-1001")
check(8, "POST /customers/C-1001 answers 405 METHOD_NOT_ALLOWED",
      error_problems(status, body, 405, "METHOD_NOT_ALLOWED"), text)

failed = results.count(False)
print()
print(f"{len(results)} checks: {len(results) - failed} passed, {failed} failed")
sys.exit(1 if failed else 0)
 