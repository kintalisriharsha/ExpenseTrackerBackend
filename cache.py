# """
# cache.py
# ────────
# Redis cache layer using Upstash (serverless Redis).
# Falls back gracefully if Redis is unavailable — never breaks the app.

# TTLs:
#     home_data     → 5  minutes  (changes when user adds expense)
#     settings_data → 15 minutes  (changes only when user saves settings)
#     goals_data    → 10 minutes  (changes when user adds/updates goal)
# """

# import json
# import logging
# import os
# from typing import Any, Optional

# import redis.asyncio as aioredis
# from dotenv import load_dotenv

# load_dotenv()
# logger = logging.getLogger(__name__)

# # ── TTLs (seconds) ─────────────────────────────────────────────────────────────

# TTL_HOME     = 5  * 60   # 5  minutes
# TTL_SETTINGS = 15 * 60   # 15 minutes
# TTL_GOALS    = 10 * 60   # 10 minutes
# TTL_EXPENSES = 3  * 60   # 3  minutes (changes frequently)

# # ── Redis client ───────────────────────────────────────────────────────────────

# _redis: Optional[aioredis.Redis] = None


# async def get_redis() -> Optional[aioredis.Redis]:
#     """
#     Returns Redis client, or None if unavailable.
#     Never raises — cache failures are always silent.
#     """
#     global _redis
#     if _redis is None:
#         url = os.getenv("REDIS_URL")
#         if not url:
#             return None
#         try:
#             _redis = aioredis.from_url(
#                 url,
#                 encoding        = "utf-8",
#                 decode_responses = True,
#                 socket_timeout  = 2,          # fail fast if Redis is slow
#             )
#             await _redis.ping()
#             logger.warning("Redis connected")
#         except Exception as e:
#             logger.warning(f"Redis unavailable: {e} — running without cache")
#             _redis = None
#     return _redis


# async def close_redis():
#     """Call on app shutdown."""
#     global _redis
#     if _redis:
#         await _redis.close()
#         _redis = None


# # ── Cache key builders ─────────────────────────────────────────────────────────
# # Format: "resource:user_id"
# # Easy to invalidate all keys for a user: scan "home:7", "settings:7" etc.

# def key_home(user_id: int)     -> str: return f"home:{user_id}"
# def key_settings(user_id: int) -> str: return f"settings:{user_id}"
# def key_goals(user_id: int)    -> str: return f"goals:{user_id}"
# def key_expenses(user_id: int) -> str: return f"expenses:{user_id}"


# # ── Core helpers ───────────────────────────────────────────────────────────────

# async def cache_get(key: str) -> Optional[Any]:
#     """
#     Returns deserialized value or None if miss / Redis down.
#     """
#     r = await get_redis()
#     if not r:
#         return None
#     try:
#         data = await r.get(key)
#         return json.loads(data) if data else None
#     except Exception as e:
#         logger.warning(f"Cache GET failed [{key}]: {e}")
#         return None


# async def cache_set(key: str, value: Any, ttl: int) -> bool:
#     """
#     Serializes and stores value. Returns True on success.
#     """
#     r = await get_redis()
#     if not r:
#         return False
#     try:
#         await r.setex(key, ttl, json.dumps(value, default=str))
#         return True
#     except Exception as e:
#         logger.warning(f"Cache SET failed [{key}]: {e}")
#         return False


# async def cache_delete(*keys: str) -> None:
#     """
#     Invalidate one or more cache keys.
#     Call this whenever the underlying data changes.
#     """
#     r = await get_redis()
#     if not r:
#         return
#     try:
#         await r.delete(*keys)
#     except Exception as e:
#         logger.warning(f"Cache DELETE failed {keys}: {e}")


# async def cache_delete_user(user_id: int) -> None:
#     """
#     Invalidate ALL cache entries for a user.
#     Call on logout.
#     """
#     await cache_delete(
#         key_home(user_id),
#         key_settings(user_id),
#         key_goals(user_id),
#         key_expenses(user_id),
#     )
