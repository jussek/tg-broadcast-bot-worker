import os
import aiohttp


def _get_worker_url():
    url = os.getenv("TELEGRAM_WORKER_URL")
    if not url:
        raise RuntimeError("Missing TELEGRAM_WORKER_URL")
    return url.rstrip("/")


def _get_worker_secret():
    secret = os.getenv("WORKER_SECRET")
    if not secret:
        raise RuntimeError("Missing WORKER_SECRET")
    return secret


TIMEOUT = aiohttp.ClientTimeout(total=int(os.getenv("WORKER_TIMEOUT_SECONDS", "90")))


async def _request(method: str, path: str, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["X-Worker-Secret"] = _get_worker_secret()
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async with session.request(method, f"{_get_worker_url()}{path}", headers=headers, **kwargs) as response:
            data = await response.json(content_type=None)
            if response.status >= 400 or not data.get("ok", False):
                raise RuntimeError(data.get("error", f"Worker HTTP {response.status}"))
            return data


async def get_chats():
    data = await _request("GET", "/chats")
    return data.get("chats", [])


async def send_message(chat_ids, message: str):
    return await _request("POST", "/send", json={"chat_ids": list(chat_ids), "message": message})
