"""Single-owner HTTP API; submissions and signals are committed before acknowledgement."""

import asyncio
import secrets
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field
from sqlalchemy.exc import IntegrityError

from rocky.config import Settings
from rocky.models import GoalContract, GoalView, Model
from rocky.store import Store


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

    def authorize(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if settings.api_token is None or not settings.api_token.get_secret_value():
            raise HTTPException(503, "Configure ROCKY_API_TOKEN before using the API")
        if credentials is None or not secrets.compare_digest(
            credentials.credentials.encode("utf-8"),
            settings.api_token.get_secret_value().encode("utf-8"),
        ):
            raise HTTPException(401, "Invalid bearer token", headers={"WWW-Authenticate": "Bearer"})

    auth = [Depends(authorize)]

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
