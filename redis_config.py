import os
import redis

REDIS_URL = os.getenv("REDIS_URL", "redis://polymarket_redis:6379/0")

# Single, shared Redis client connection instance
r = redis.from_url(REDIS_URL, decode_responses=True)