"""FastAPI BMI health-check service with client accounts and end-user UI."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app import auth, db, metrics
from app.bmi import calculate_bmi

APP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"

# Uvicorn only configures its own loggers, which would hide app-level
# warnings such as a failing metrics publisher.
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 8

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@asynccontextmanager
async def lifespan(_: FastAPI):
    # A database outage must not stop the pod from serving; the repository is
    # retried on first use instead.
    try:
        db.get_repository()
    except Exception:
        logger.exception("client storage unavailable at startup; will retry on use")
        db.set_repository(None)

    if not metrics.metrics_enabled():
        yield
        return

    stop = asyncio.Event()
    publisher = asyncio.create_task(metrics.publish_loop(stop))
    try:
        yield
    finally:
        stop.set()
        await publisher


app = FastAPI(title="BMI Health Check", version="2.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def track_request_metrics(request: Request, call_next):
    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000
        metrics.collector.record_request(elapsed_ms, status_code)


class BmiRequest(BaseModel):
    height_cm: float = Field(..., gt=0, description="Height in centimeters")
    weight_kg: float = Field(..., gt=0, description="Weight in kilograms")


class BmiResponse(BaseModel):
    height_cm: float
    weight_kg: float
    bmi: float
    category: str
    summary: str
    health_advice: list[str]
    exercises: list[str]
    needs_attention: bool


class HealthResponse(BaseModel):
    status: str
    pod: str | None = None
    node: str | None = None


def signed_in_email(request: Request) -> str | None:
    return auth.read_session(request.cookies.get(auth.SESSION_COOKIE))


def require_api_client(request: Request) -> str:
    """Session gate for JSON endpoints."""
    email = signed_in_email(request)
    if not email:
        raise HTTPException(status_code=401, detail="Sign in to use the BMI calculator")
    return email


def _set_session_cookie(response: RedirectResponse, email: str) -> None:
    response.set_cookie(
        auth.SESSION_COOKIE,
        auth.issue_session(email),
        max_age=auth.SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        # The load balancer serves plain HTTP today; flip this on with TLS.
        secure=os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true",
        path="/",
    )


def _to_response(result) -> BmiResponse:
    metrics.collector.record_bmi_calculation()
    return BmiResponse(
        height_cm=result.height_cm,
        weight_kg=result.weight_kg,
        bmi=result.bmi,
        category=result.category,
        summary=result.summary,
        health_advice=list(result.health_advice),
        exercises=list(result.exercises),
        needs_attention=result.needs_attention,
    )


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness/readiness probe; reports which pod answered."""
    return HealthResponse(
        status="ok",
        pod=os.getenv("POD_NAME"),
        node=os.getenv("NODE_NAME"),
    )


@app.get("/")
def home(request: Request):
    email = signed_in_email(request)
    if not email:
        return RedirectResponse("/signin", status_code=303)

    client = db.get_repository().find(email)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"email": email, "full_name": client.full_name if client else email},
    )


@app.get("/signup")
def signup_page(request: Request):
    if signed_in_email(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "signup.html", {})


@app.post("/signup")
def signup(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    repository: db.ClientRepository = Depends(db.get_repository),
):
    full_name = full_name.strip()
    email = email.strip().lower()

    def fail(message: str):
        return templates.TemplateResponse(
            request,
            "signup.html",
            {"error": message, "full_name": full_name, "email": email},
            status_code=400,
        )

    if not full_name:
        return fail("Enter your full name.")
    if not EMAIL_PATTERN.match(email):
        return fail("Enter a valid email address.")
    if len(password) < MIN_PASSWORD_LENGTH:
        return fail(f"Use a password of at least {MIN_PASSWORD_LENGTH} characters.")
    if password != confirm_password:
        return fail("Passwords do not match.")

    try:
        repository.create(email, full_name, auth.hash_password(password))
    except db.EmailAlreadyRegistered:
        return fail("That email is already registered. Sign in instead.")

    logger.info("registered client %s", email)
    response = RedirectResponse("/", status_code=303)
    _set_session_cookie(response, email)
    return response


@app.get("/signin")
def signin_page(request: Request):
    if signed_in_email(request):
        return RedirectResponse("/", status_code=303)
    registered = request.query_params.get("registered") == "1"
    return templates.TemplateResponse(request, "signin.html", {"registered": registered})


@app.post("/signin")
def signin(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    repository: db.ClientRepository = Depends(db.get_repository),
):
    email = email.strip().lower()
    client = repository.find(email)

    # Same message either way so the form cannot be used to enumerate emails.
    if not client or not auth.verify_password(password, client.password_hash):
        return templates.TemplateResponse(
            request,
            "signin.html",
            {"error": "Email or password is incorrect.", "email": email},
            status_code=401,
        )

    repository.touch_login(email)
    response = RedirectResponse("/", status_code=303)
    _set_session_cookie(response, email)
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/signin", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


@app.get("/me")
def me(email: str = Depends(require_api_client)):
    client = db.get_repository().find(email)
    return {
        "email": email,
        "full_name": client.full_name if client else None,
        "created_at": client.created_at if client else None,
        "last_login_at": client.last_login_at if client else None,
    }


@app.post("/bmi", response_model=BmiResponse)
def bmi_post(body: BmiRequest, _: str = Depends(require_api_client)) -> BmiResponse:
    try:
        result = calculate_bmi(body.height_cm, body.weight_kg)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _to_response(result)


@app.get("/bmi", response_model=BmiResponse)
def bmi_get(
    height_cm: float = Query(..., gt=0),
    weight_kg: float = Query(..., gt=0),
    _: str = Depends(require_api_client),
) -> BmiResponse:
    try:
        result = calculate_bmi(height_cm, weight_kg)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _to_response(result)


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "nextgenmark.jpg")
