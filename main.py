import asyncio
import json
import logging
import os
import sys
import time
import traceback
import jwt
import uvicorn
import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from motor.motor_asyncio import AsyncIOMotorClient
from pythonjsonlogger import jsonlogger
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.baggage import set_baggage, get_baggage
from opentelemetry.context import attach


app = FastAPI()

trace.set_tracer_provider(TracerProvider())
tracer = trace.get_tracer(__name__)

HTTPXClientInstrumentor().instrument()


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log("Unhandled exception", exc_info=sys.exc_info())
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


def log(event: str, exc_info=None, **kwargs):
    current_span = trace.get_current_span()
    span_context = current_span.get_span_context()
    kwargs["trace_id"] = format(span_context.trace_id, "032x")
    kwargs["span_id"] = format(span_context.span_id, "016x")
    kwargs["user_id"] = get_baggage("user_id") or ""
    kwargs["org_id"] = get_baggage("org_id") or ""
    if exc_info:
        logger.error(event, extra=kwargs, exc_info=exc_info)
    else:
        logger.info(event, extra=kwargs)


class ErrorExtractionFilter(logging.Filter):
    def filter(self, record):
        if record.levelno >= logging.ERROR and record.exc_info:
            exc_type, exc_value, exc_tb = record.exc_info
            frames = traceback.extract_tb(exc_tb)
            if frames:
                last_frame = frames[-1]
                location = f"{last_frame.filename}:{last_frame.lineno} in {last_frame.name}"
            else:
                location = ""
            record.error = {
                "type": exc_type.__name__ if exc_type else "",
                "message": str(exc_value) if exc_value else "",
                "location": location,
                "stack_trace": "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
            }
            record.exc_info = None
            record.exc_text = None
        else:
            record.error = None
        return True


formatter = jsonlogger.JsonFormatter(
    fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
    rename_fields={"levelname": "level", "asctime": "timestamp"},
)

handler = logging.StreamHandler()
handler.setFormatter(formatter)
handler.addFilter(ErrorExtractionFilter())

logger = logging.getLogger("app")
logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False

LOGGING_CONFIG = json.loads(open("logging_config.json").read())

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "dev-secret-change-in-prod")
ALGORITHM = "HS256"
security = HTTPBearer(auto_error=False)

MONGO_URL = os.getenv("MONGO_URL", "mongodb://mongodb:27017")
DB_NAME = "observability"
USERS_COLLECTION = "users"

mongo_client: AsyncIOMotorClient = None
db = None

MOCK_USERS = [
    {"user_id": "alice", "org_id": "acme"},
    {"user_id": "bob", "org_id": "acme"},
    {"user_id": "charlie", "org_id": "globex"},
    {"user_id": "diana", "org_id": "globex"},
]


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


@app.middleware("http")
async def tracing_middleware(request: Request, call_next):
    ctx = trace.get_current_span().get_span_context()
    with tracer.start_as_current_span(
        f"{request.method} {request.url.path}",
        context=ctx,
    ) as span:
        response = await call_next(request)

        user_id = get_baggage("user_id") or ""
        org_id = get_baggage("org_id") or ""
        form_record_id = get_baggage("form_record_id") or ""

        if user_id:
            span.set_attribute("user_id", user_id)
        if org_id:
            span.set_attribute("org_id", org_id)
        if form_record_id:
            span.set_attribute("form_record_id", form_record_id)

        span.set_status(
            trace.StatusCode.OK if response.status_code < 500
            else trace.StatusCode.ERROR
        )

        log(
            "span_completed",
            span_name=f"{request.method} {request.url.path}",
            start_time=span.start_time / 1e9 if span.start_time else 0,
            end_time=time.time(),
            duration_ms=round((time.time() - (span.start_time / 1e9)) * 1000, 2) if span.start_time else 0,
            status_code=response.status_code,
            span_status=str(span.status.status_code.name),
            attributes=dict(span.attributes) if span.attributes else {},
            events=[
                {"name": e.name, "timestamp": e.timestamp / 1e9 if e.timestamp else 0}
                for e in (span.events or [])
            ],
        )
        return response


@app.on_event("startup")
async def startup_db():
    global mongo_client, db
    mongo_client = AsyncIOMotorClient(MONGO_URL)
    db = mongo_client[DB_NAME]
    count = await db[USERS_COLLECTION].count_documents({})
    if count == 0:
        await db[USERS_COLLECTION].insert_many(MOCK_USERS)
        log("Seeded mock users into MongoDB")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/signin")
async def signin(body: SigninRequest):
    user_id = body.user_id
    org_id = body.org_id
    user = await db[USERS_COLLECTION].find_one({"user_id": user_id, "org_id": org_id})
    if not user:
        raise HTTPException(status_code=401, detail="Invalid user_id or org_id")

    token = jwt.encode(
        {"user_id": user_id, "org_id": org_id}, SECRET_KEY, algorithm=ALGORITHM
    )
    log("POST /signin successful", user_id=user_id, org_id=org_id)
    return {"access_token": token, "token_type": "bearer"}


@app.post("/form_A/{form_id}")
async def form_A(form_id: str, request: Request, _auth=Depends(require_auth)):
    log("POST /form_A received", form_id=form_id)
    await asyncio.sleep(1)
    ctx = set_baggage("form_record_id", form_id)
    attach(ctx)
    log("POST /form_A completed", form_id=form_id)
    return {"status": "ok", "form_id": form_id}


@app.post("/form_B/{form_id}")
async def form_B(form_id: str, request: Request, _auth=Depends(require_auth)):
    log("POST /form_B received", form_id=form_id)
    await asyncio.sleep(1)
    ctx = set_baggage("form_record_id", form_id)
    attach(ctx)
    log("POST /form_B completed", form_id=form_id)
    return {"status": "ok", "form_id": form_id}


@app.post("/external")
async def external(request: Request, _auth=Depends(require_auth)):
    log("POST /external received")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("http://httpstat.us/500", timeout=5.0)
        log("External API returned error", status_code=resp.status_code)
        return {"status": "error", "upstream_status": resp.status_code}
    except httpx.HTTPError as e:
        log("External API call failed", exc_info=sys.exc_info())
        raise HTTPException(status_code=502, detail="Upstream error")


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True, log_config=LOGGING_CONFIG)
