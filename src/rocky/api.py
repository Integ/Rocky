"""Single-owner HTTP API; submissions and signals are committed before acknowledgement."""

import asyncio
import ipaddress
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import Field
from sqlalchemy.exc import IntegrityError

from rocky.config import Settings
from rocky.inference import ModelSettings, model_catalog
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
_IPV6_AUTHORITY = re.compile(r"\[([0-9a-fA-F:.]+)\](?::([0-9]{1,5}))?\Z")


def _loopback_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped.is_loopback
    return address.is_loopback


def _local_authority(value: str) -> tuple[str, int | None] | None:
    """Validate an HTTP Host authority without DNS, URL repair or proxy headers."""
    if not value or any(
        ord(character) <= 32 or ord(character) == 127 or character == "\\" for character in value
    ):
        return None
    if value.startswith("["):
        match = _IPV6_AUTHORITY.fullmatch(value)
        if match is None:
            return None
        host, port = match.groups()
        try:
            if ipaddress.ip_address(host).version != 6:
                return None
        except ValueError:
            return None
    else:
        if value.count(":") > 1:
            return None
        host, separator, port = value.partition(":")
        if not separator:
            port = None
        elif not port or not port.isascii() or not port.isdecimal():
            return None
    if port is not None and (len(port) > 5 or not 1 <= int(port) <= 65535):
        return None
    if host.lower() == "localhost":
        normalized = "localhost"
    elif _loopback_address(host):
        normalized = str(ipaddress.ip_address(host))
    else:
        return None
    return normalized, int(port) if port is not None else None


def _local_request(request: Request) -> bool:
    # The server must leave proxy_headers disabled: ASGI's client/scheme are
    # the connection boundary, never X-Forwarded-For or Forwarded assertions.
    if request.client is None or not _loopback_address(request.client.host):
        return False
    headers = request.scope.get("headers", ())

    def values(name: bytes) -> list[bytes]:
        return [value for key, value in headers if key.lower() == name]

    hosts = values(b"host")
    if len(hosts) != 1:
        return False
    try:
        authority = hosts[0].decode("ascii")
    except UnicodeDecodeError:
        return False
    host_parts = _local_authority(authority)
    if host_parts is None:
        return False
    origins = values(b"origin")
    if origins:
        if len(origins) != 1:
            return False
        try:
            origin = origins[0].decode("ascii")
        except UnicodeDecodeError:
            return False
        if any(
            ord(character) <= 32 or ord(character) == 127 or character == "\\"
            for character in origin
        ):
            return False
        origin_scheme, separator, origin_authority = origin.partition("://")
        origin_scheme = origin_scheme.lower()
        scheme = request.scope.get("scheme", "").lower()
        if not separator or origin_scheme not in {"http", "https"} or origin_scheme != scheme:
            return False
        origin_parts = _local_authority(origin_authority)
        if origin_parts is None:
            return False
        default_port = 443 if scheme == "https" else 80
        if (host_parts[0], host_parts[1] or default_port) != (
            origin_parts[0],
            origin_parts[1] or default_port,
        ):
            return False
    sites = values(b"sec-fetch-site")
    if len(sites) > 1:
        return False
    if sites:
        site = sites[0].strip().lower()
        if site == b"cross-site":
            # A deliberate click on a localhost console link may originate in
            # an external page. Permit only that top-level static entry; never
            # apply this navigation exception to API data or operations.
            return (
                request.method == "GET"
                and request.scope.get("path") in {"/", "/console-assets/index.html"}
                and values(b"sec-fetch-mode") == [b"navigate"]
                and values(b"sec-fetch-dest") == [b"document"]
                and values(b"sec-fetch-user") == [b"?1"]
            )
        if site not in {b"same-origin", b"same-site", b"none"}:
            return False
    return True


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
        local_denied = settings.auth_mode == "local" and not _local_request(request)
        response = (
            JSONResponse(status_code=403, content={"detail": "Local access denied"})
            if local_denied
            else await call_next(request)
        )
        path = request.scope.get("path", "")
        if (
            local_denied
            or path in ("/", "/console-info", "/console-assets")
            or path.startswith("/console-assets/")
        ):
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

    auth = [] if settings.auth_mode == "local" else [Depends(authorize)]

    @app.get("/", include_in_schema=False)
    def console():
        # Serve a static shell under the configured access boundary. Task data
        # stays in the JSON API, and credentials are never embedded in HTML.
        return FileResponse(_WEB_ROOT / "index.html", media_type="text/html")

    app.mount(
        "/console-assets",
        StaticFiles(directory=_WEB_ROOT, check_dir=False),
        name="console-assets",
    )

    @app.get("/console-info", dependencies=auth)
    def console_info():
        current = get_default_model_settings()
        return {
            "model": current.model,
            "reasoning_effort": current.reasoning_effort,
            "models": model_catalog(settings.model),
            "version": app.version,
            "tools": list(_TOOL_NAMES),
            "polling_seconds": 3,
            "auth_mode": settings.auth_mode,
        }

    def get_default_model_settings() -> ModelSettings:
        return database.get_model_settings(
            ModelSettings(model=settings.model, reasoning_effort=settings.reasoning_effort)
        )

    @app.get("/model-settings", response_model=ModelSettings, dependencies=auth)
    def get_model_settings():
        return get_default_model_settings()

    @app.put("/model-settings", response_model=ModelSettings, dependencies=auth)
    def put_model_settings(value: ModelSettings):
        if value.model not in {item["id"] for item in model_catalog(settings.model)}:
            raise HTTPException(422, "Select a model from the configured model catalog")
        return database.set_model_settings(value)

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
            return database.create_goal(goal_id, contract, get_default_model_settings())
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
