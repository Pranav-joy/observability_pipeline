import asyncio
import json
import os
import random
import sys
import time
import uuid
import jwt
import uvicorn
import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from motor.motor_asyncio import AsyncIOMotorClient
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from opentelemetry import trace
from opentelemetry.baggage import set_baggage
from opentelemetry.context import attach

from tracing import setup, log_span, enrich_span_from_context
import db as database


app = FastAPI()
FastAPIInstrumentor.instrument_app(app,excluded_urls="health,metrics")
HTTPXClientInstrumentor().instrument()


# --- Config ---

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "dev-secret-change-in-prod")
ALGORITHM = "HS256"
MONGO_URL = os.getenv("MONGO_URL", "mongodb://mongodb:27017")
DB_NAME = "observability"
USERS_COLLECTION = "users"

SKIP_TRACING = {"/metrics", "/health"}

MOCK_USERS = [
    {"user_id": "alice", "org_id": "acme"},
    {"user_id": "bob", "org_id": "acme"},
    {"user_id": "charlie", "org_id": "globex"},
    {"user_id": "diana", "org_id": "globex"},
]

mongo_client: AsyncIOMotorClient = None
db = None
# --- Logging ---

logger = setup()

LOGGING_CONFIG = json.loads(open("logging_config.json").read())


# --- Auth ---

security = HTTPBearer(auto_error=False)


class SigninRequest(BaseModel):
    user_id: str
    org_id: str


async def require_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    if not credentials:
        if request.client and request.client.host in ("127.0.0.1", "localhost"):
            user_id = request.headers.get("X-User-ID", "internal")
            org_id = request.headers.get("X-Org-ID", "internal")
            ctx = set_baggage("user_id", user_id)
            ctx = set_baggage("org_id", org_id, context=ctx)
            attach(ctx)
            request.state.user_id = user_id
            request.state.org_id = org_id
            return
        raise HTTPException(status_code=401, detail="Not authenticated")

    token = credentials.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    user_id = payload.get("user_id")
    org_id = payload.get("org_id")
    if user_id is None or org_id is None:
        raise HTTPException(status_code=401, detail="Token missing user_id or org_id")

    ctx = set_baggage("user_id", user_id)
    ctx = set_baggage("org_id", org_id, context=ctx)
    attach(ctx)
    request.state.user_id = user_id
    request.state.org_id = org_id


# --- Middleware ---

@app.middleware("http")
async def tracing_middleware(request: Request, call_next):
    span = trace.get_current_span()
    span.set_attribute("http.method", request.method)
    span.set_attribute("http.url", str(request.url))
    span.set_attribute("http.route", request.url.path)
    try:
        response = await call_next(request)
        if span.is_recording():
            enrich_span_from_context(span, request)
            span.set_attribute("http.status_code", response.status_code)
            if response.status_code >= 500:
                span.set_status(trace.StatusCode.ERROR, f"HTTP {response.status_code}")
            else:
                span.set_status(trace.StatusCode.OK)
            ctx = span.get_span_context()
            duration_ms = round((time.time() - span.start_time / 1e9) * 1000, 2) if span.start_time else 0
            log_span(span, f"{request.method} {request.url.path}",
                     duration_ms=duration_ms,
                     trace_id=format(ctx.trace_id, "032x"),
                     span_id=format(ctx.span_id, "016x"))
        return response
    except Exception as exc:
        if span.is_recording():
            enrich_span_from_context(span, request)
            span.record_exception(exc)
            span.set_status(trace.StatusCode.ERROR, str(exc))
            ctx = span.get_span_context()
            duration_ms = round((time.time() - span.start_time / 1e9) * 1000, 2) if span.start_time else 0
            log_span(span, f"{request.method} {request.url.path}",
                     duration_ms=duration_ms,
                     trace_id=format(ctx.trace_id, "032x"),
                     span_id=format(ctx.span_id, "016x"))
        raise exc


# --- Startup ---

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception", exc_info=(type(exc), exc, exc.__traceback__))
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.on_event("startup")
async def startup_db():
    global mongo_client, db
    mongo_client = AsyncIOMotorClient(MONGO_URL)
    db = mongo_client[DB_NAME]
    database.init_db(db)
    count = await database.count_users()
    if count == 0:
        await database.seed_users(MOCK_USERS)
        logger.info("Seeded mock users into MongoDB")


# --- Endpoints ---

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/signin")
async def signin(request: Request, body: SigninRequest):
    user_id = body.user_id
    org_id = body.org_id

    ctx = set_baggage("user_id", user_id)
    ctx = set_baggage("org_id", org_id, context=ctx)
    attach(ctx)
    request.state.user_id = user_id
    request.state.org_id = org_id
    logger.info("POST /signin received")
    user = await database.search_user(user_id, org_id)
    if not user:
        logger.info("POST /signin failed: user not found")
        raise HTTPException(status_code=401, detail="Invalid user_id or org_id")

    token = jwt.encode(
        {"user_id": user_id, "org_id": org_id}, SECRET_KEY, algorithm=ALGORITHM
    )
    logger.info("POST /signin successful")

    return {"access_token": token, "token_type": "bearer"}


@app.post("/form_A")
async def form_A(request: Request, _auth=Depends(require_auth)):
    form_id = "Form A"
    form_record_id = str(uuid.uuid4())

    ctx = set_baggage("form_record_id", form_record_id)
    ctx = set_baggage("form_id", form_id, context=ctx)
    attach(ctx)
    request.state.form_id = form_id
    request.state.form_record_id = form_record_id

    logger.info("POST /form_A received")
    await asyncio.sleep(1)

    await database.insert_form_record("Hi data form recorded", form_record_id, request.state.user_id)

    logger.info("POST /form_A completed")

    return {"status": "ok", "form_id": form_id, "form_record_id": form_record_id}


async def delay(seconds):
    await asyncio.sleep(seconds)


@app.post("/form_B")
async def form_B(request: Request, _auth=Depends(require_auth)):
    form_id = "Form B"
    form_record_id = str(uuid.uuid4())

    ctx = set_baggage("form_record_id", form_record_id)
    ctx = set_baggage("form_id", form_id, context=ctx)
    attach(ctx)
    request.state.form_id = form_id
    request.state.form_record_id = form_record_id

    logger.info("POST /form_B received")

    if random.random() < 0.5:
        logger.info("POST /form_B rejected")
        raise HTTPException(status_code=400, detail="Form B rejected")

    async with httpx.AsyncClient() as client:
        resp = await client.get("http://192.0.2.1:9999/unreachable", timeout=2.0)
    logger.info("POST /form_B got /dummy response", extra={"status_code": resp.status_code})

    await delay(1)

    await database.insert_form_record("Hi data form recorded", form_record_id, request.state.user_id)

    logger.info("POST /form_B completed")
    return {"status": "ok", "form_id": form_id, "form_record_id": form_record_id}
    
        


@app.post("/external")
async def external(request: Request, _auth=Depends(require_auth)):
    logger.info("POST /external received")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("http://httpstat.us/500", timeout=5.0)
        logger.info("External API returned error", extra={"status_code": resp.status_code})
        return {"status": "error", "upstream_status": resp.status_code}
    except httpx.HTTPError as e:
        logger.error("External API call failed", exc_info=sys.exc_info())
        raise HTTPException(status_code=502, detail="Upstream error")


@app.get("/dummy", include_in_schema=False)
async def dummy():
    logger.info("GET /dummy received")
    await delay(2)
    return {"status": "ok"}
 

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True, log_config=LOGGING_CONFIG)
