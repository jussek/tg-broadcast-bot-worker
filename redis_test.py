import os

from dotenv import load_dotenv
from upstash_redis import Redis


load_dotenv()

url = os.getenv("UPSTASH_REDIS_REST_URL")
token = os.getenv("UPSTASH_REDIS_REST_TOKEN")

if not url or not token:
    raise RuntimeError(
        "Не найдены UPSTASH_REDIS_REST_URL "
        "или UPSTASH_REDIS_REST_TOKEN"
    )


redis = Redis(
    url=url,
    token=token
)


redis.set(
    "telegram_bot:test",
    "Работает!"
)

value = redis.get(
    "telegram_bot:test"
)

print("Redis ответил:")
print(value)