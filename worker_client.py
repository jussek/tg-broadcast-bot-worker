import os
import aiohttp

WORKER_URL = os.environ["TELEGRAM_WORKER_URL"].rstrip("/")
WORKER_SECRET = os.environ["WORKER_SECRET"]
TIMEOUT = aiohttp.ClientTimeout(total=int(os.getenv("WORKER_TIMEOUT_SECONDS", "90")))


async def _request(method: str, path: str, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["X-Worker-Secret"] = WORKER_SECRET
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async with session.request(method, f"{WORKER_URL}{path}", headers=headers, **kwargs) as response:
            data = await response.json(content_type=None)
            if response.status >= 400 or not data.get("ok", False):
                raise RuntimeError(data.get("error", f"Worker HTTP {response.status}"))
            return data


async def get_chats():
    data = await _request("GET", "/chats")
    return data.get("chats", [])


async def send_message(chat_ids, message: str):
    return await _request("POST", "/send", json={"chat_ids": list(chat_ids), "message": message})
