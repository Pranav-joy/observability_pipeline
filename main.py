import asyncio
import logging
import json
import os
import random
import sys
import time
import uuid
import jwt
import uvicorn
import httpx
from dotenv import load_dotenv
load_dotenv()
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from motor.motor_asyncio import AsyncIOMotorClient

from opentelemetry import trace
from opentelemetry.baggage import set_baggage
from opentelemetry.context import attach
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from tracing import (
    init_telemetry, traced, rebuild_baggage_from_request,
)
import db as database


app = FastAPI()

# --- Logging ---

otel_handler = init_telemetry()
FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics")

logger = logging.getLogger("app")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    logger.addHandler(otel_handler)


# --- Middleware ---

BAGGAGE_FIELDS = set(json.loads(os.getenv("BAGGAGE_FIELDS")))
# ["user_id","org_id","form_id","form_record_id"]

@app.middleware("http")
async def baggage_middleware(request: Request, call_next):
    body = await request.body()
    request._body = body
    if body:
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                ctx = None
                for key, value in data.items():
                    if key in BAGGAGE_FIELDS:
                        setattr(request.state, key, str(value))
                        if ctx is None:
                            ctx = set_baggage(key, str(value))
                        else:
                            ctx = set_baggage(key, str(value), context=ctx)
                if ctx:
                    attach(ctx)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    return await call_next(request)


# --- Config ---

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "dev-secret-change-in-prod")
ALGORITHM = "HS256"
MONGO_URL = os.getenv("MONGO_URL", "mongodb://mongodb:27017")
DB_NAME = "observability"
USERS_COLLECTION = "users"


MOCK_USERS = [
    {"user_id": "alice", "org_id": "acme"},
    {"user_id": "bob", "org_id": "acme"},
    {"user_id": "charlie", "org_id": "globex"},
    {"user_id": "diana", "org_id": "globex"},
]

mongo_client: AsyncIOMotorClient = None
db = None


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


# --- Startup ---

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    rebuild_baggage_from_request(request, fields=BAGGAGE_FIELDS)
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

    async with httpx.AsyncClient() as client:
        resp = await client.get("http://localhost:8000/dummy", timeout=5.0)
    logger.info("POST /form_A got /dummy response", extra={"status_code": resp.status_code})


    await database.insert_form_record("Hi data form recorded", form_record_id, request.state.user_id)

    logger.info("POST /form_A completed")

    return {"status": "ok", "form_id": form_id, "form_record_id": form_record_id}


@traced("simulate_work")
async def simulate_work(seconds):
    logger.info("simulate_work started", extra={"seconds": seconds})
    await asyncio.sleep(seconds)
    logger.info("simulate_work done")


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

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("http://192.0.2.1:9999/unreachable", timeout=2.0)
    except httpx.HTTPError:
        logger.error("Upstream call failed", exc_info=sys.exc_info())
        raise HTTPException(status_code=502, detail="Upstream error")

    await simulate_work(1)

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
    ctx = set_baggage("dummy", "dummy")
    attach(ctx)
    logger.info("GET /dummy received")
    await simulate_work(2)
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
