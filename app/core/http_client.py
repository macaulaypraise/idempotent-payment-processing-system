import httpx
from app.config import get_settings

settings = get_settings()

async def create_http_client() -> httpx.AsyncClient:
    client = httpx.AsyncClient(
        base_url=settings.PAYMENT_PROVIDER_URL,
        timeout=30.0
    )
    return client

async def close_http_client(client: httpx.AsyncClient) -> None:
    await client.aclose()
