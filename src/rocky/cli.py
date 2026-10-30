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
    commands.add_parser("plans", help="List long-running plans and their steps")
    commands.add_parser("schedules", help="List scheduled and recurring tasks")
    commands.add_parser(
        "status", help="Show service health: worker, Temporal, tasks, disk, backups"
    )
    backup = commands.add_parser("backup", help="Back up the databases and task workspaces now")
    backup.add_argument(
        "--to", type=Path, help="Folder for the backup (default: the backup folder)"
    )
    commands.add_parser("backups", help="List backups")
    restore = commands.add_parser(
        "restore", help="Restore a backup; stop the service first. Current data is moved aside"
    )
    restore.add_argument("file", type=Path)
    commands.add_parser(
        "rotate-token", help="Replace the owner API token; restart the service afterwards"
    )
    commands.add_parser(
        "browser-install", help="Download Playwright's Chromium for the browse tool (no Chrome)"
    )
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
    if args.command in {"backup", "backups", "restore", "rotate-token", "browser-install"}:
        raise SystemExit(_local_command(args, settings))
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
            elif args.command in {"plans", "schedules"}:
                response = client.get(f"/{args.command}")
            elif args.command == "status":
                response = client.get("/status")
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


def _local_command(args, settings: Settings) -> int:
    """Owner maintenance that works on local files, with or without the service."""
    from rocky.backup import (
        BackupError,
        create_backup,
        default_backup_dir,
        list_backups,
        restore_backup,
    )
    from rocky.supervisor import service_paths

    run_dir, token_path = service_paths(settings)
    try:
        if args.command == "backup":
            path = create_backup(settings, args.to, keep=None if args.to else settings.backup_keep)
            print(path)
        elif args.command == "backups":
            for item in list_backups(default_backup_dir(settings)):
                print(f"{item['created_at']}  {item['bytes'] / 1_048_576:8.1f} MB  {item['path']}")
        elif args.command == "restore":
            aside = restore_backup(settings, args.file, run_dir=run_dir)
            print(f"Restored {args.file.name}. The previous data was moved to {aside}")
        elif args.command == "rotate-token":
            from rocky.supervisor import rotate_token

            rotate_token(token_path)
            print("A new API token was written. Restart the Rocky service to use it.")
        elif args.command == "browser-install":
            import subprocess

            return subprocess.call([sys.executable, "-m", "playwright", "install", "chromium"])
    except BackupError as exc:
        print(f"Rocky: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    main()
