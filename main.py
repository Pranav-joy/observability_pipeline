import asyncio
import json
import logging
import os
import time
import uuid
from contextvars import ContextVar
from typing import Optional

import jwt
import uvicorn
import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from motor.motor_asyncio import AsyncIOMotorClient
from pythonjsonlogger import jsonlogger
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor


app = FastAPI()
HTTPXClientInstrumentor().instrument()
FastAPIInstrumentor.instrument_app(app)

trace.set_tracer_provider(TracerProvider())
tracer = trace.get_tracer(__name__)

# --- Auth configuration ---
SECRET_KEY = os.getenv("JWT_SECRET_KEY", "dev-secret-change-in-prod")
ALGORITHM = "HS256"
security = HTTPBearer(auto_error=False)

_current_user_id: ContextVar[Optional[str]] = ContextVar("current_user_id", default=None)

# --- MongoDB ---
MONGO_URL = os.getenv("MONGO_URL", "mongodb://mongodb:27017")
DB_NAME = "observability"
USERS_COLLECTION = "users"

mongo_client: AsyncIOMotorClient = None
db = None

MOCK_USERS = [
    {"user_id": "admin"},
    {"user_id": "user1"},
    {"user_id": "user2"},
]


@app.on_event("startup")
async def startup_db():
    global mongo_client, db
    mongo_client = AsyncIOMotorClient(MONGO_URL)
    db = mongo_client[DB_NAME]
    count = await db[USERS_COLLECTION].count_documents({})
    if count == 0:
        await db[USERS_COLLECTION].insert_many(MOCK_USERS)
        log("Seeded mock users into MongoDB", user_id="system")




#  Main Observabiltiy Features Implemented:


# --- Logging ---
class UserIdFilter(logging.Filter):
    def filter(self, record):
        record.user_id = _current_user_id.get() or ""
        return True

formatter = jsonlogger.JsonFormatter(
    fmt="%(asctime)s %(levelname)s %(name)s %(message)s %(user_id)s",
    rename_fields={"levelname": "level", "asctime": "timestamp"},
)

handler = logging.StreamHandler()
handler.setFormatter(formatter)
handler.addFilter(UserIdFilter())

logger = logging.getLogger("app")
logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False

LOGGING_CONFIG = json.loads(open("logging_config.json").read())


def log(event: str, **kwargs):
    current_span = trace.get_current_span()
    span_context = current_span.get_span_context()
    kwargs["trace_id"] = format(span_context.trace_id, "032x")
    kwargs["span_id"] = format(span_context.span_id, "016x")
    logger.info(event, extra=kwargs)


# --- Auth dependency ---
async def require_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    # No token + localhost → internal call, allow
    if not credentials and request.client and request.client.host in ("127.0.0.1", "localhost"):
        _current_user_id.set("internal")
        request.state.user_id = "internal"
        return

    # No token + not localhost → reject
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Has token → validate it
    token = credentials.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    user_id = payload.get("user_id")
    if user_id is None:
        raise HTTPException(status_code=401, detail="Token missing user_id")

    request.state.user_id = user_id
    _current_user_id.set(user_id)


# --- Sign-in endpoint ---
class SigninRequest(BaseModel):
    user_id: str



# --- Internal functions ---
async def function_c(request_id: str):
    with tracer.start_as_current_span("function_c"):
        log("function_c started", request_id=request_id)
        await asyncio.sleep(1)
        log("function_c completed", request_id=request_id)
        return {"status": "processed"}


async def function_b(request_id: str):
    with tracer.start_as_current_span("function_b"):
        log("function_b started", request_id=request_id)
        await asyncio.sleep(1)
        log("function_b completed", request_id=request_id)
        headers = {"X-Request-ID": request_id}
        async with httpx.AsyncClient() as client:
            resp = await client.post("http://localhost:8000/process", headers=headers)
        return resp.json()


async def function_a(request_id: str):
    with tracer.start_as_current_span("function_a"):
        log("function_a started", request_id=request_id)
        await asyncio.sleep(1)
        log("function_a completed", request_id=request_id)
        return {"result": "a_done"}


async def start_process(request_id: str):
    log("start_process started", request_id=request_id)
    log("calling function_a", request_id=request_id)
    a_result = await function_a(request_id)
    log("calling function_b", request_id=request_id)
    b_result = await function_b(request_id)
    log("start_process completed", request_id=request_id)
    return {"a": a_result, "b": b_result}

@app.post("/signin")
async def signin(body: SigninRequest):
    user_id = body.user_id

    user = await db[USERS_COLLECTION].find_one({"user_id": user_id})
    if not user:
        raise HTTPException(status_code=401, detail="Invalid user_id")

    token = jwt.encode({"user_id": user_id}, SECRET_KEY, algorithm=ALGORITHM)
    log("POST /signin successful", user_id=user_id)
    return {"access_token": token, "token_type": "bearer"}

@app.post("/start")
async def start(request: Request, _auth=Depends(require_auth)):
    with tracer.start_as_current_span("start_endpoint"):
        request_id = str(uuid.uuid4())
        log("POST /start received", request_id=request_id)
        t0 = time.time()

        result = await start_process(request_id)

        elapsed = round(time.time() - t0, 3)
        log("POST /start finished", request_id=request_id, elapsed_s=elapsed)
        return {"request_id": request_id, "elapsed_s": elapsed, "result": result}

@app.post("/process")
async def process(request: Request, _auth=Depends(require_auth)):
    with tracer.start_as_current_span("process_endpoint"):
        trace.get_current_span()
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        log("POST /process received", request_id=request_id)
        t0 = time.time()
        result = await function_c(request_id)
        elapsed = round(time.time() - t0, 3)
        log("POST /process finished", request_id=request_id, elapsed_s=elapsed)
        return {"result": result, "elapsed_s": elapsed}

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True, log_config=LOGGING_CONFIG)
