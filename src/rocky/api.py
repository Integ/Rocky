"""Single-owner HTTP API; submissions and signals are committed before acknowledgement."""

import asyncio
import ipaddress
import logging
import os
import re
import secrets
import sqlite3
import tarfile
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field
from sqlalchemy.exc import IntegrityError

from rocky import __version__
from rocky.approval_policy import ApprovalRule, ApprovalSettings
from rocky.backup import BackupError, create_backup, default_backup_dir, list_backups
from rocky.config import Settings
from rocky.contract_check import ContractWarning, contract_warnings
from rocky.credentials import CredentialError, CredentialStore
from rocky.inference import ModelSettings, model_catalog
from rocky.models import (
    DraftedContract,
    ExecutionLimits,
    GoalContract,
    GoalDraft,
    GoalView,
    Model,
)
from rocky.monitoring import prometheus, service_status
from rocky.planner import OpenAIPlanner, PlannerError
from rocky.providers import PRESETS, ProviderUpdate, credential_id, provider_model_catalog
from rocky.store import MemoryConflictError, Store, equivalent_contracts
from rocky.tools import ToolError, WorkspaceTools

log = logging.getLogger(__name__)
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
        if site not in {b"same-origin", b"same-site", b"none"}:
            return False
    return True


class ContractReview(Model):
    contract: GoalContract
    warnings: list[ContractWarning] = Field(default_factory=list)
    # Tokens the drafting call used; not charged to the task.
    drafting_tokens: int | None = None


class FollowUpRequest(Model):
    message: str = Field(min_length=1, max_length=2000)


class WakeRequest(Model):
    message: str = Field(min_length=1, max_length=4000)


class KnowledgeRequest(Model):
    content: str = Field(min_length=1, max_length=16000)
    key: str | None = Field(default=None, min_length=1, max_length=128)
    priority: int = Field(default=0, ge=-100, le=100)


class MemoryRequest(Model):
    kind: Literal["preference", "decision", "correction", "pitfall"]
    content: str = Field(min_length=1, max_length=8000)
    keywords: str = Field(default="", max_length=2000)
    source: str = Field(default="", max_length=2000)
    priority: int = Field(default=0, ge=-100, le=100)
    expected_revision: int | None = Field(default=None, ge=0)


class ProposalDecision(Model):
    # The owner may edit a suggestion before saving it.
    content: str | None = Field(default=None, min_length=1, max_length=8000)


ApprovalScope = Literal["action", "task", "always", "deny", "deferred", "dismiss"]


class ApprovalRequest(Model):
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    # "task" also approves later simple commands with the pending command
    # prefix, or HTTP requests with the pending method and origin; "always"
    # saves that as a rule for every task. "deny" declines the pending action.
    # "deferred" approves an action declined after a timeout; "dismiss" drops it.
    scope: ApprovalScope = "action"


class BatchApprovalItem(ApprovalRequest):
    goal_id: str = Field(min_length=1, max_length=128)


class BatchApprovalRequest(Model):
    items: list[BatchApprovalItem] = Field(min_length=1, max_length=50)


def _approval_payload(request: ApprovalRequest) -> str:
    return (
        request.fingerprint
        if request.scope == "action"
        else f"{request.scope}:{request.fingerprint}"
    )


def create_app(
    settings: Settings | None = None,
    store: Store | None = None,
    credential_store: CredentialStore | None = None,
) -> FastAPI:
    settings = settings or Settings()
    database = store or Store(settings.database_url)
    credentials = credential_store or CredentialStore(settings.credential_path)
    planner = OpenAIPlanner(settings, defer_client=True, credential_store=credentials)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await asyncio.to_thread(database.initialize)
        try:
            yield
        finally:
            try:
                await planner.close()
            finally:
                # A model client that fails to close must not leak the database.
                if store is None:
                    database.close()

    app = FastAPI(
        title="Rocky",
        version=__version__,
        lifespan=lifespan,
        description="Durable execution of long-running goals.",
        docs_url=None,
        redoc_url=None,
    )
    app.state.store = database
    bearer = HTTPBearer(auto_error=False)

    @app.exception_handler(RequestValidationError)
    async def safe_validation_error(request: Request, error: RequestValidationError):
        if request.url.path == "/provider-settings":
            # Pydantic's normal errors include the invalid input. A key must
            # never be reflected into responses, error monitoring or logs.
            return JSONResponse(
                status_code=422,
                content={
                    "detail": "Invalid provider settings; check the API URL, model and key format"
                },
            )
        from fastapi.exception_handlers import request_validation_exception_handler

        return await request_validation_exception_handler(request, error)

    @app.middleware("http")
    async def response_security(request: Request, call_next):
        local_denied = settings.auth_mode == "local" and not _local_request(request)
        response = (
            JSONResponse(status_code=403, content={"detail": "Local access denied"})
            if local_denied
            else await call_next(request)
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("application/json"):
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

    def get_default_model_settings() -> ModelSettings:
        return database.get_model_settings(
            ModelSettings(model=settings.model, reasoning_effort=settings.reasoning_effort)
        )

    @app.get("/model-settings", response_model=ModelSettings, dependencies=auth)
    def get_model_settings():
        return get_default_model_settings()

    @app.put("/model-settings", response_model=ModelSettings, dependencies=auth)
    def put_model_settings(value: ModelSettings):
        if (
            value.provider is not None
            or value.base_url is not None
            or value.credential_id is not None
        ):
            raise HTTPException(422, "Use provider settings to configure a third-party model")
        if value.model not in {item["id"] for item in model_catalog(settings.model)}:
            raise HTTPException(422, "Select a model from the configured model catalog")
        return database.set_model_settings(value)

    def provider_view(current: ModelSettings):
        provider = current.provider or "openai"
        base_url = current.base_url or "https://api.openai.com/v1"
        account = current.credential_id or credential_id(provider, base_url)
        storage_error = None
        try:
            has_key = bool(credentials.get(account)) if current.credential_id else False
        except CredentialError as exc:
            has_key = False
            storage_error = str(exc)
        if not has_key and provider == "openai" and base_url == "https://api.openai.com/v1":
            has_key = bool(os.environ.get("OPENAI_API_KEY"))
        return {
            "provider": provider,
            "base_url": base_url,
            "model": current.model,
            "reasoning_effort": current.reasoning_effort,
            "has_api_key": has_key,
            "credential_storage": credentials.storage,
            "credential_error": storage_error,
            "providers": PRESETS,
            "models": provider_model_catalog(provider, base_url, current.model),
        }

    @app.get("/provider-settings", dependencies=auth)
    def get_provider_settings():
        return provider_view(get_default_model_settings())

    @app.put("/provider-settings", dependencies=auth)
    def put_provider_settings(value: ProviderUpdate):
        snapshot = value.snapshot()
        try:
            key = value.api_key.get_secret_value() if value.api_key else ""
            if key:
                credentials.set(snapshot.credential_id, key)
            elif value.clear_api_key:
                credentials.set(snapshot.credential_id, None)
        except CredentialError as exc:
            raise HTTPException(503, str(exc)) from None
        return provider_view(database.set_model_settings(snapshot))

    def require_goal(goal_id: str) -> GoalView:
        result = database.get_goal(goal_id)
        if result is None:
            raise HTTPException(404, "Goal not found")
        return result

    @app.get("/health")
    def health():
        # This is liveness. Queued goals can remain pending while Temporal is offline.
        return {"status": "ok", "service": "rocky"}

    @app.get("/status", dependencies=auth)
    def status():
        """Readiness and the numbers worth watching; see rocky.monitoring."""
        return service_status(settings, database)

    @app.get("/metrics", dependencies=auth)
    def metrics():
        return PlainTextResponse(
            prometheus(service_status(settings, database)),
            media_type="text/plain; version=0.0.4",
        )

    @app.get("/backups", dependencies=auth)
    def get_backups():
        directory = default_backup_dir(settings)
        return {
            "directory": str(directory),
            "interval_hours": settings.backup_interval_hours,
            "keep": settings.backup_keep,
            "backups": list_backups(directory),
        }

    @app.post("/backups", status_code=201, dependencies=auth)
    def post_backup():
        try:
            path = create_backup(settings, keep=settings.backup_keep)
        except BackupError as exc:
            raise HTTPException(409, str(exc)) from None
        except (OSError, sqlite3.Error, tarfile.TarError):
            log.exception("Backup failed")
            raise HTTPException(500, "Backup failed; see the API log") from None
        return next(item for item in list_backups(path.parent) if item["path"] == str(path))

    def check_folders(contract: GoalContract | GoalDraft) -> None:
        try:
            tools = WorkspaceTools(settings)
            tools.granted_folders(contract)
            tools.attached_files(contract)
        except ToolError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/goal-contract", response_model=ContractReview, dependencies=auth)
    async def prepare_goal_contract(draft: GoalDraft):
        """Draft a contract for owner review; it is submitted separately."""
        check_folders(draft)
        try:
            prepared = await planner.prepare_contract(draft, get_default_model_settings())
        except PlannerError:
            raise HTTPException(
                502, "Could not assess task requirements and budget; retry submission"
            ) from None
        concerns = prepared.concerns if isinstance(prepared, DraftedContract) else []
        contract = prepared.contract() if isinstance(prepared, DraftedContract) else prepared
        return ContractReview(
            contract=contract,
            warnings=contract_warnings(contract, concerns),
            drafting_tokens=prepared.drafting_tokens
            if isinstance(prepared, DraftedContract)
            else None,
        )

    @app.post("/goal-contract/check", response_model=ContractReview, dependencies=auth)
    def check_goal_contract(contract: GoalContract):
        """Budget warnings for a contract the owner edited before submitting."""
        check_folders(contract)
        return ContractReview(contract=contract, warnings=contract_warnings(contract))

    @app.post("/goals", response_model=GoalView, status_code=202, dependencies=auth)
    def submit_goal(
        contract: GoalContract,
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ):
        check_folders(contract)
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
            if existing and equivalent_contracts(existing.contract, contract):
                return existing
            raise HTTPException(409, "Conflicting concurrent goal submission") from None

    @app.get("/goals", response_model=list[GoalView], dependencies=auth)
    def list_goals(
        response: Response,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        archived: bool = False,
        query: Annotated[str, Query(max_length=200)] = "",
        if_none_match: Annotated[str | None, Header()] = None,
    ):
        # Read the tag before the list: a write in between yields a newer list
        # under an older tag, so the next poll refetches instead of missing it.
        query = query.strip()
        tag = f'"{database.list_goals_tag(limit, archived=archived, query=query)}"'
        if if_none_match and tag in {
            value.strip().removeprefix("W/") for value in if_none_match.split(",")
        }:
            return Response(status_code=304, headers={"ETag": tag, "Cache-Control": "no-store"})
        response.headers["ETag"] = tag
        return database.list_goals(limit, archived=archived, query=query)

    @app.get("/memories", dependencies=auth)
    def get_memories(
        query: Annotated[str, Query(max_length=8000)] = "",
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        include_inactive: bool = False,
    ):
        return database.memories(query, limit, include_inactive=include_inactive)

    @app.put("/memories/{key}", dependencies=auth)
    def put_memory(key: str, request: MemoryRequest):
        try:
            return database.put_memory(key, **request.model_dump())
        except MemoryConflictError as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.delete("/memories/{key}", dependencies=auth)
    def retract_memory(key: str, expected_revision: Annotated[int, Query(ge=1)]):
        try:
            return database.archive_memory(key, expected_revision)
        except KeyError:
            raise HTTPException(404, "Memory not found") from None
        except MemoryConflictError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/memory-proposals", dependencies=auth)
    def get_memory_proposals(goal_id: str | None = None):
        return database.memory_proposals(goal_id)

    @app.post("/memory-proposals/{proposal_id}/save", dependencies=auth)
    def save_memory_proposal(proposal_id: str, request: ProposalDecision | None = None):
        return _resolve_proposal(proposal_id, save=True, content=request and request.content)

    @app.post("/memory-proposals/{proposal_id}/dismiss", dependencies=auth)
    def dismiss_memory_proposal(proposal_id: str):
        return _resolve_proposal(proposal_id, save=False)

    def _resolve_proposal(proposal_id: str, *, save: bool, content: str | None = None):
        try:
            return database.resolve_memory_proposal(proposal_id, save=save, content=content)
        except KeyError:
            raise HTTPException(404, "Suggestion not found") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/goals/{goal_id}", response_model=GoalView, dependencies=auth)
    def get_goal(goal_id: str):
        return require_goal(goal_id)

    def archive_goal(goal_id: str, archived: bool):
        try:
            return database.set_archived(goal_id, archived)
        except KeyError:
            raise HTTPException(404, "Goal not found") from None
        except ValueError:
            raise HTTPException(409, "Only completed goals can be archived or restored") from None

    @app.post("/goals/{goal_id}/archive", response_model=GoalView, dependencies=auth)
    def archive_completed_goal(goal_id: str):
        return archive_goal(goal_id, True)

    @app.post("/goals/{goal_id}/restore", response_model=GoalView, dependencies=auth)
    def restore_completed_goal(goal_id: str):
        return archive_goal(goal_id, False)

    @app.post(
        "/goals/{goal_id}/recover", response_model=GoalView, status_code=202, dependencies=auth
    )
    def recover_failed_goal(goal_id: str):
        try:
            return database.recover_goal(goal_id)
        except KeyError:
            raise HTTPException(404, "Goal not found") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post(
        "/goals/{goal_id}/extend", response_model=GoalView, status_code=202, dependencies=auth
    )
    def extend_goal(goal_id: str, limits: ExecutionLimits):
        """Continue a goal that ran out of budget or time, from its saved progress."""
        try:
            return database.extend_goal(goal_id, limits)
        except KeyError:
            raise HTTPException(404, "Goal not found") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post(
        "/goals/{goal_id}/follow-up", response_model=GoalView, status_code=202, dependencies=auth
    )
    def follow_up_goal(goal_id: str, request: FollowUpRequest):
        """Continue an ended goal in place with a follow-up request."""
        try:
            return database.follow_up_goal(goal_id, request.message)
        except KeyError:
            raise HTTPException(404, "Goal not found") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.delete("/goals/{goal_id}", dependencies=auth)
    def delete_completed_goal(goal_id: str):
        try:
            database.delete_goal(goal_id)
        except KeyError:
            raise HTTPException(404, "Goal not found") from None
        except ValueError:
            raise HTTPException(409, "Only completed goals can be deleted") from None
        try:
            WorkspaceTools(settings).delete_goal_files(goal_id)
        except (OSError, ToolError):
            # The record is gone either way; leftover files only cost disk space.
            log.warning("Could not remove a deleted goal's files")
        return {"goal_id": goal_id, "deleted": True}

    @app.get("/goals/{goal_id}/events", dependencies=auth)
    def get_events(goal_id: str, after: Annotated[int, Query(ge=0)] = 0):
        require_goal(goal_id)
        return database.events(goal_id, after=after)

    @app.get("/goals/{goal_id}/messages", dependencies=auth)
    def get_messages(goal_id: str):
        require_goal(goal_id)
        return database.goal_messages(goal_id)

    @app.get("/goals/{goal_id}/conversation", dependencies=auth)
    def get_conversation(goal_id: str):
        require_goal(goal_id)
        return database.conversation(goal_id)

    @app.get("/goals/{goal_id}/steps", dependencies=auth)
    def get_execution_steps(
        goal_id: str,
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ):
        require_goal(goal_id)
        return database.execution_steps(goal_id, after=after, limit=limit)

    @app.get("/goals/{goal_id}/knowledge", dependencies=auth)
    def get_knowledge(goal_id: str):
        require_goal(goal_id)
        return database.knowledge(goal_id)

    @app.get("/goals/{goal_id}/execution-events", dependencies=auth)
    def get_execution_events(
        goal_id: str,
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ):
        require_goal(goal_id)
        return database.execution_events(goal_id, after=after, limit=limit)

    @app.put("/goals/{goal_id}/knowledge", dependencies=auth)
    def put_knowledge(goal_id: str, request: KnowledgeRequest):
        require_goal(goal_id)
        try:
            return database.put_knowledge(
                goal_id, request.content, key=request.key, priority=request.priority
            )
        except KeyError:
            raise HTTPException(404, "Goal not found") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/goals/{goal_id}/artifacts/content", dependencies=auth)
    def artifact_content(goal_id: str, path: Annotated[str, Query(min_length=1, max_length=240)]):
        goal = require_goal(goal_id)
        if path not in {item.path for item in goal.contract.deliverables}:
            raise HTTPException(404, "Artifact is not a declared deliverable")
        try:
            content = WorkspaceTools(settings).read_artifact(goal_id, path)
        except (OSError, ToolError, UnicodeError):
            raise HTTPException(404, "Artifact is missing, unsafe or not readable text") from None
        limit = 100_000
        return {"path": path, "content": content[:limit], "truncated": len(content) > limit}

    @app.get("/goals/{goal_id}/artifacts/download", dependencies=auth)
    def artifact_download(goal_id: str, path: Annotated[str, Query(min_length=1, max_length=240)]):
        goal = require_goal(goal_id)
        if path not in {item.path for item in goal.contract.deliverables}:
            raise HTTPException(404, "Artifact is not a declared deliverable")
        try:
            content = WorkspaceTools(settings).read_artifact_bytes(goal_id, path)
        except (OSError, ToolError):
            raise HTTPException(404, "Artifact is missing, unsafe or inaccessible") from None
        filename = quote(path.rsplit("/", 1)[-1], safe="")
        return Response(
            content,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
                "Cache-Control": "no-store",
            },
        )

    def command(goal_id: str, kind: str, payload: str = ""):
        require_goal(goal_id)
        try:
            command_id = database.enqueue_command(goal_id, kind, payload)
        except KeyError:
            raise HTTPException(404, "Goal not found") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"goal_id": goal_id, "command_id": command_id, "status": "queued"}

    @app.post("/goals/{goal_id}/cancel", status_code=202, dependencies=auth)
    def cancel_goal(goal_id: str):
        return command(goal_id, "cancel")

    @app.post("/goals/{goal_id}/wake", status_code=202, dependencies=auth)
    def wake_goal(goal_id: str, request: WakeRequest):
        return command(goal_id, "wake", request.message)

    @app.post("/goals/{goal_id}/approve", status_code=202, dependencies=auth)
    def approve_goal(goal_id: str, request: ApprovalRequest):
        return command(goal_id, "approve", _approval_payload(request))

    def default_approval_settings() -> ApprovalSettings:
        return ApprovalSettings(
            read_only_commands=settings.auto_approve_read_only,
            timeout_minutes=settings.approval_timeout_minutes,
        )

    @app.get("/approvals", dependencies=auth)
    def list_approvals():
        """Pending and timed-out approval requests across unfinished tasks."""
        return database.approvals()

    @app.post("/approvals/batch", status_code=202, dependencies=auth)
    def batch_approvals(request: BatchApprovalRequest):
        results = []
        for item in request.items:
            try:
                command_id = database.enqueue_command(
                    item.goal_id, "approve", _approval_payload(item)
                )
                results.append(
                    {"goal_id": item.goal_id, "command_id": command_id, "status": "queued"}
                )
            except KeyError:
                results.append(
                    {"goal_id": item.goal_id, "status": "error", "detail": "Goal not found"}
                )
            except ValueError as exc:
                results.append({"goal_id": item.goal_id, "status": "error", "detail": str(exc)})
        return {"results": results}

    @app.get("/approval-policy", dependencies=auth)
    def get_approval_policy():
        return {
            "settings": database.get_approval_settings(default_approval_settings()),
            "rules": database.approval_rules(),
            "allowlist": settings.auto_approve_commands,
        }

    @app.put("/approval-policy", dependencies=auth)
    def put_approval_policy(value: ApprovalSettings):
        database.set_approval_settings(value)
        return get_approval_policy()

    @app.post("/approval-rules", status_code=201, dependencies=auth)
    def create_approval_rule(rule: ApprovalRule):
        try:
            return database.add_approval_rule(
                rule.model_copy(update={"rule_id": "", "created_at": ""})
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.delete("/approval-rules/{rule_id}", status_code=204, dependencies=auth)
    def delete_approval_rule(rule_id: str):
        try:
            database.delete_approval_rule(rule_id)
        except KeyError:
            raise HTTPException(404, "Approval rule not found") from None
        return Response(status_code=204)

    return app
