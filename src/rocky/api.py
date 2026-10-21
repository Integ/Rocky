"""Single-owner HTTP API; submissions and signals are committed before acknowledgement."""

import asyncio
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import Field
from sqlalchemy.exc import IntegrityError

from rocky.config import Settings
from rocky.models import GoalContract, GoalView, Model
from rocky.store import Store

_WEB_ROOT = Path(__file__).parent / "web"
_CONSOLE_CSP = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    )
)
_TOOL_NAMES = ("read_file", "write_file", "list_files", "fetch_url")


class WakeRequest(Model):
    message: str = Field(min_length=1, max_length=4000)


def create_app(settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    settings = settings or Settings()
    database = store or Store(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await asyncio.to_thread(database.initialize)
        yield
        if store is None:
            database.close()

    app = FastAPI(
        title="Rocky",
        version="0.1.0",
        lifespan=lifespan,
        description="Durable, bounded execution of long-running goals.",
    )
    app.state.store = database
    bearer = HTTPBearer(auto_error=False)

    @app.middleware("http")
    async def response_security(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path in ("/", "/console-info", "/console-assets") or path.startswith("/console-assets/"):
            response.headers["Content-Security-Policy"] = _CONSOLE_CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        content_type = response.headers.get("content-type", "")
        if (
            path == "/"
            or content_type.startswith("application/json")
            or (path.startswith("/console-assets/") and content_type.startswith("text/html"))
        ):
            response.headers["Cache-Control"] = "no-store"
        return response

    def authorize(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if settings.api_token is None or not settings.api_token.get_secret_value():
            raise HTTPException(503, "Configure ROCKY_API_TOKEN before using the API")
        if credentials is None or not secrets.compare_digest(
            credentials.credentials.encode("utf-8"),
            settings.api_token.get_secret_value().encode("utf-8"),
        ):
            raise HTTPException(401, "Invalid bearer token", headers={"WWW-Authenticate": "Bearer"})

    auth = [Depends(authorize)]

    @app.get("/", include_in_schema=False)
    def console():
        # This is a static public shell. Task data and all credentials remain
        # behind the authenticated JSON API or in the owner's browser memory.
        return FileResponse(_WEB_ROOT / "index.html", media_type="text/html")

    app.mount(
        "/console-assets",
        StaticFiles(directory=_WEB_ROOT, check_dir=False),
        name="console-assets",
    )

    @app.get("/console-info", dependencies=auth)
    def console_info():
        return {
            "model": settings.model,
            "version": app.version,
            "tools": list(_TOOL_NAMES),
            "polling_seconds": 3,
        }

    def require_goal(goal_id: str) -> GoalView:
        result = database.get_goal(goal_id)
        if result is None:
            raise HTTPException(404, "Goal not found")
        return result

    @app.get("/health")
    def health():
        # This is liveness. Queued goals can remain pending while Temporal is offline.
        return {"status": "ok", "service": "rocky"}

    @app.post("/goals", response_model=GoalView, status_code=202, dependencies=auth)
    def submit_goal(
        contract: GoalContract,
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ):
        goal_id = (
            str(uuid5(NAMESPACE_URL, f"rocky:{idempotency_key}"))
            if idempotency_key
            else str(uuid4())
        )
        try:
            return database.create_goal(goal_id, contract)
        except ValueError as exc:
            raise HTTPException(409, "Idempotency key already used for a different goal") from exc
        except IntegrityError:
            # Concurrent retries may race on the unique goal_id insert.
            existing = database.get_goal(goal_id)
            if existing and existing.contract == contract:
                return existing
            raise HTTPException(409, "Conflicting concurrent goal submission") from None

    @app.get("/goals", response_model=list[GoalView], dependencies=auth)
    def list_goals(limit: Annotated[int, Query(ge=1, le=100)] = 50):
        return database.list_goals(limit)

    @app.get("/goals/{goal_id}", response_model=GoalView, dependencies=auth)
    def get_goal(goal_id: str):
        return require_goal(goal_id)

    @app.get("/goals/{goal_id}/events", dependencies=auth)
    def get_events(goal_id: str, after: Annotated[int, Query(ge=0)] = 0):
        require_goal(goal_id)
        return database.events(goal_id, after=after)

    def command(goal_id: str, kind: str, payload: str = ""):
        require_goal(goal_id)
        try:
            command_id = database.enqueue_command(goal_id, kind, payload)
        except ValueError as exc:
            raise HTTPException(409, "Goal has already ended") from exc
        return {"goal_id": goal_id, "command_id": command_id, "status": "queued"}

    @app.post("/goals/{goal_id}/cancel", status_code=202, dependencies=auth)
    def cancel_goal(goal_id: str):
        return command(goal_id, "cancel")

    @app.post("/goals/{goal_id}/wake", status_code=202, dependencies=auth)
    def wake_goal(goal_id: str, request: WakeRequest):
        return command(goal_id, "wake", request.message)

    return app
