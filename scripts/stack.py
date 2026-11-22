"""Shared plumbing for scripts that drive the dev stack from the host.

The stack is `make dev` (docker-compose.yml): the API on :8000, the PSP
simulator on :8001, Postgres on :5432. Owner credentials come from .env, the
same file compose reads, so the scripts see exactly the database the stack
uses.
"""

import asyncio
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

API_URL = os.environ.get("IPPS_API_URL", "http://localhost:8000")
SIM_URL = os.environ.get("IPPS_SIM_URL", "http://localhost:8001")


def dotenv(path: str = ".env") -> dict[str, str]:
    values: dict[str, str] = {}
    file = Path(path)
    if not file.exists():
        return values
    for line in file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def owner_url() -> str:
    """The stack's database, as its owner, from the host."""
    if os.environ.get("IPPS_OWNER_URL"):
        return os.environ["IPPS_OWNER_URL"]
    env = dotenv()
    user = env.get("POSTGRES_USER", "postgres")
    password = env.get("POSTGRES_PASSWORD", "postgres")
    database = env.get("POSTGRES_DB", "payments_db")
    return f"postgresql+asyncpg://{user}:{password}@localhost:5432/{database}"


def webhook_secret() -> str:
    return dotenv().get("PSP_WEBHOOK_SECRET", "local-dev-webhook-secret")


class Stack:
    def __init__(self) -> None:
        self.engine = create_async_engine(owner_url(), pool_size=4)
        self.factory = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def close(self) -> None:
        await self.engine.dispose()

    async def platform_api_key(self) -> str:
        """A new key for the platform (default) tenant; reconciliation needs one."""
        from app.cli import create_api_key
        from app.models.tenant import DEFAULT_TENANT_ID

        async with self.factory() as db:
            return await create_api_key(db, DEFAULT_TENANT_ID)

    async def scalar(self, sql: str, **params: Any) -> Any:
        from sqlalchemy import text

        async with self.factory() as db:
            return (await db.execute(text(sql), params)).scalar()

    async def rows(self, sql: str, **params: Any) -> list[Any]:
        from sqlalchemy import text

        async with self.factory() as db:
            return list((await db.execute(text(sql), params)).all())


async def wait_healthy(seconds: float = 60.0) -> None:
    deadline = time.monotonic() + seconds
    last = "no answer"
    async with httpx.AsyncClient(timeout=3.0) as http:
        while time.monotonic() < deadline:
            try:
                api = await http.get(f"{API_URL}/health")
                sim = await http.get(f"{SIM_URL}/health")
                if api.status_code == 200 and sim.status_code == 200:
                    return
                last = f"api {api.status_code}, simulator {sim.status_code}"
            except httpx.HTTPError as exc:
                last = repr(exc)
            await asyncio.sleep(1.0)
    raise SystemExit(f"the stack is not up ({last}). Start it with: make dev")


def compose(*args: str) -> str:
    result = subprocess.run(
        ["docker", "compose", *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker compose {' '.join(args)}: {result.stderr}")
    return result.stdout


def hardware() -> dict[str, str]:
    """What the numbers were measured on: every latency needs this next to it."""
    info = {
        "os": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "cpus": str(os.cpu_count()),
    }
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        for line in cpuinfo.read_text().splitlines():
            if line.startswith("model name"):
                info["cpu"] = line.split(":", 1)[1].strip()
                break
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        kb = int(meminfo.read_text().split()[1])
        info["memory"] = f"{kb / 1024 / 1024:.1f} GiB"
    if "microsoft" in platform.release().lower():
        info["os"] += " (WSL2)"
    try:
        info["docker"] = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    except OSError:
        pass
    return info
