"""Rocky service and owner CLI."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

from rocky.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="rocky")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("api", help="Run the owner HTTP API")
    commands.add_parser("worker", help="Run the workflow worker and outbox dispatcher")
    commands.add_parser("serve", help="Supervise Temporal, the API and the worker as one service")
    submit = commands.add_parser("submit", help="Submit a goal contract JSON file")
    submit.add_argument("file", type=Path)
    submit.add_argument("--idempotency-key")
    commands.add_parser("list", help="List recent goals")
    for name in ("get", "cancel", "wake", "events"):
        item = commands.add_parser(name)
        item.add_argument("goal_id")
        if name == "wake":
            item.add_argument("message")
    args = parser.parse_args()
    settings = Settings()
    if args.command == "api":
        import uvicorn

        from rocky.api import create_app

        # Local access checks need the actual socket peer and scheme, rather
        # than values supplied by forwarding headers from a loopback proxy.
        # The desktop client polls every few seconds; an access log of those
        # requests would grow without bound in a long-running service.
        uvicorn.run(
            create_app(settings),
            host=settings.host,
            port=settings.port,
            proxy_headers=False,
            access_log=False,
        )
        return
    if args.command == "worker":
        from rocky.service import run_worker

        asyncio.run(run_worker(settings))
        return
    if args.command == "serve":
        from rocky.supervisor import serve

        raise SystemExit(serve(settings))
    if settings.auth_mode == "token" and not settings.api_token:
        parser.error("Set ROCKY_API_TOKEN for owner API access")
    base_url = os.environ.get("ROCKY_API_URL", "http://127.0.0.1:8000")
    headers = (
        {"Authorization": f"Bearer {settings.api_token.get_secret_value()}"}
        if settings.api_token and settings.api_token.get_secret_value()
        else {}
    )
    try:
        with httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=20,
            trust_env=settings.auth_mode != "local",
        ) as client:
            if args.command == "submit":
                data = json.loads(args.file.read_text())
                extra = {"Idempotency-Key": args.idempotency_key} if args.idempotency_key else {}
                response = client.post("/goals", json=data, headers=extra)
            elif args.command == "list":
                response = client.get("/goals")
            elif args.command == "cancel":
                response = client.post(f"/goals/{args.goal_id}/cancel")
            elif args.command == "wake":
                response = client.post(
                    f"/goals/{args.goal_id}/wake", json={"message": args.message}
                )
            else:
                suffix = "/events" if args.command == "events" else ""
                response = client.get(f"/goals/{args.goal_id}{suffix}")
            if response.is_error:
                print(f"Rocky API returned HTTP {response.status_code}", file=sys.stderr)
                raise SystemExit(1)
            print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    except (httpx.HTTPError, OSError, ValueError):
        print("Rocky request failed; check the API connection and input file", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
