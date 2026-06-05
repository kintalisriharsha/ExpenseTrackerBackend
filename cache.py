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


"""
cache.py
────────
Redis cache layer using Upstash (HTTP-based, no persistent TCP connection).

NO circuit breaker — just clean try/except on every operation.
If Redis fails, the caller falls through to the DB. Simple and honest.

ENV VARS required in .env:
    UPSTASH_REDIS_REST_URL   = https://xxxx.upstash.io
    UPSTASH_REDIS_REST_TOKEN = AXxx...

──────────────────────────────────────────────────────────────────────────
KEY MAP
──────────────────────────────────────────────────────────────────────────
blacklist:{jti}                → token revocation   TTL = remaining token life
otp:{email}                    → OTP payload        TTL = 10 min
ratelimit:otp:{email}          → OTP resend gate    TTL = 60 s
ratelimit:login:{ip}           → login brute-force  TTL = 15 min
home:{user_id}                 → HomeResponse       TTL = 5 min
settings:{user_id}             → SettingsResponse   TTL = 15 min
analytics:summary:{uid}:{m}:{y}→ AnalyticsSummary  TTL = 5 min
analytics:total:{uid}:{m}:{y}  → TotalSpent        TTL = 5 min
analytics:categories:{u}:{m}:{y}→ CategoryBreakdown TTL = 5 min
analytics:trend:{uid}:{months} → MonthlyTrend       TTL = 30 min
analytics:heatmap:{uid}:{m}:{y}→ Heatmap            TTL = 5 min
goals:all:{user_id}            → GoalListResponse   TTL = 10 min
goals:active:{user_id}         → active goals list  TTL = 10 min
me:{user_id}                   → UserResponse       TTL = 5 min
──────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from dotenv import load_dotenv
from upstash_redis.asyncio import Redis   # pip install upstash-redis

load_dotenv()
logger = logging.getLogger(__name__)

# ── TTLs (seconds) ─────────────────────────────────────────────────────────────

TTL_HOME              = 5  * 60
TTL_SETTINGS          = 15 * 60
TTL_ANALYTICS         = 5  * 60
TTL_ANALYTICS_TREND   = 30 * 60   # trend is expensive + changes slowly
TTL_GOALS             = 10 * 60
TTL_ME                = 5  * 60
TTL_OTP               = 10 * 60
TTL_RATE_OTP          = 60
TTL_RATE_LOGIN        = 15 * 60

# ── Redis client (lazy singleton) ─────────────────────────────────────────────

_redis: Optional[Redis] = None


def _get_redis() -> Optional[Redis]:
    global _redis
    if _redis is not None:
        return _redis
    url   = os.getenv("REDIS_URL")
    token = os.getenv("REDIS_TOKEN")
    if not url or not token:
        logger.warning("Upstash Redis not configured — cache disabled")
        return None
    _redis = Redis(url=url, token=token)
    return _redis


async def close_redis() -> None:
    """No-op for Upstash HTTP client — kept for interface compatibility."""
    pass


# ── Cache key builders ─────────────────────────────────────────────────────────

def blacklist_key(jti: str)                          -> str: return f"blacklist:{jti}"
def otp_key(email: str)                              -> str: return f"otp:{email}"
def rate_otp_key(email: str)                         -> str: return f"ratelimit:otp:{email}"
def rate_login_key(ip: str)                          -> str: return f"ratelimit:login:{ip}"
def home_key(user_id: int)                           -> str: return f"home:{user_id}"
def settings_key(user_id: int)                       -> str: return f"settings:{user_id}"
def analytics_summary_key(uid: int, m: int, y: int) -> str: return f"analytics:summary:{uid}:{m}:{y}"
def analytics_total_key(uid: int, m: int, y: int)   -> str: return f"analytics:total:{uid}:{m}:{y}"
def analytics_categories_key(uid: int, m: int, y: int)->str: return f"analytics:categories:{uid}:{m}:{y}"
def analytics_trend_key(uid: int, months: int)       -> str: return f"analytics:trend:{uid}:{months}"
def analytics_heatmap_key(uid: int, m: int, y: int) -> str: return f"analytics:heatmap:{uid}:{m}:{y}"
def goals_all_key(user_id: int)                      -> str: return f"goals:all:{user_id}"
def goals_active_key(user_id: int)                   -> str: return f"goals:active:{user_id}"
def me_key(user_id: int)                             -> str: return f"me:{user_id}"


# ── Core GET / SET / DELETE ────────────────────────────────────────────────────

async def cache_get(key: str) -> Optional[Any]:
    """Return deserialised value, or None on miss / any error."""
    r = _get_redis()
    if not r:
        return None
    try:
        data = await r.get(key)
        return json.loads(data) if data else None
    except Exception as e:
        logger.warning(f"cache_get failed [{key}]: {e}")
        return None


async def cache_set(key: str, value: Any, ttl: int) -> bool:
    """Serialise and store. Returns True on success, False on any error."""
    r = _get_redis()
    if not r:
        return False
    try:
        await r.setex(key, ttl, json.dumps(value, default=str))
        return True
    except Exception as e:
        logger.warning(f"cache_set failed [{key}]: {e}")
        return False


async def cache_delete(*keys: str) -> None:
    """Delete one or more keys. Silent on any error."""
    r = _get_redis()
    if not r or not keys:
        return
    try:
        await r.delete(*keys)
    except Exception as e:
        logger.warning(f"cache_delete failed {keys}: {e}")


async def cache_delete_all_analytics(user_id: int) -> None:
    """
    Bust every analytics key for a user.
    Called when an expense is added / edited / deleted.
    Uses SCAN to find all analytics:{user_id}:* keys so we don't need to
    know every month/year combination that was cached.
    """
    r = _get_redis()
    if not r:
        return
    try:
        pattern = f"analytics:*:{user_id}:*"
        cursor  = 0
        keys_to_delete: list[str] = []
        while True:
            cursor, batch = await r.scan(cursor, match=pattern, count=100)
            keys_to_delete.extend(batch)
            if cursor == 0:
                break
        if keys_to_delete:
            await r.delete(*keys_to_delete)
    except Exception as e:
        logger.warning(f"cache_delete_all_analytics failed [user_id={user_id}]: {e}")


async def cache_delete_user(user_id: int) -> None:
    """Wipe all cached data for a user. Called on logout."""
    await cache_delete(
        home_key(user_id),
        settings_key(user_id),
        goals_all_key(user_id),
        goals_active_key(user_id),
        me_key(user_id),
    )
    await cache_delete_all_analytics(user_id)


# ══════════════════════════════════════════════════════════════════════════════
# TOKEN BLACKLIST
# ══════════════════════════════════════════════════════════════════════════════

async def blacklist_token_redis(jti: str, ttl_seconds: int) -> bool:
    """Store revoked JTI in Redis with TTL. Returns True on success."""
    r = _get_redis()
    if not r:
        return False
    try:
        await r.setex(blacklist_key(jti), ttl_seconds, "1")
        return True
    except Exception as e:
        logger.warning(f"blacklist_token_redis failed [{jti}]: {e}")
        return False


async def is_token_blacklisted_redis(jti: str) -> Optional[bool]:
    """
    True  → blacklisted (Redis hit)
    False → not blacklisted (Redis hit, key absent)
    None  → Redis unavailable → caller must check PG
    """
    r = _get_redis()
    if not r:
        return None
    try:
        val = await r.exists(blacklist_key(jti))
        return val > 0
    except Exception as e:
        logger.warning(f"is_token_blacklisted_redis failed [{jti}]: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# OTP STORAGE
# ══════════════════════════════════════════════════════════════════════════════

async def otp_store(email: str, hashed_otp: str) -> bool:
    r = _get_redis()
    if not r:
        return False
    try:
        payload = json.dumps({"hash": hashed_otp, "attempts": 0})
        await r.setex(otp_key(email), TTL_OTP, payload)
        return True
    except Exception as e:
        logger.warning(f"otp_store failed [{email}]: {e}")
        return False


async def otp_get(email: str) -> Optional[dict]:
    """Returns {"hash": str, "attempts": int} or None."""
    raw = await cache_get(otp_key(email))
    if not raw:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    return raw


async def otp_increment_attempts(email: str) -> int:
    """Atomically increment attempt counter. Returns new count, or -1 on error."""
    r = _get_redis()
    if not r:
        return -1
    key = otp_key(email)
    try:
        raw = await r.get(key)
        if not raw:
            return -1
        data = json.loads(raw)
        data["attempts"] = data.get("attempts", 0) + 1
        ttl = await r.ttl(key)
        await r.setex(key, max(ttl, 1), json.dumps(data))
        return data["attempts"]
    except Exception as e:
        logger.warning(f"otp_increment_attempts failed [{email}]: {e}")
        return -1


async def otp_clear(email: str) -> None:
    await cache_delete(otp_key(email))


# ══════════════════════════════════════════════════════════════════════════════
# RATE LIMITING
# ══════════════════════════════════════════════════════════════════════════════

async def rate_limit_otp_check(email: str) -> tuple[bool, int]:
    """
    Check 60-second OTP resend gate.
    Returns (is_rate_limited, seconds_to_wait).
    Returns (False, 0) if Redis is unavailable — fail open.
    """
    r = _get_redis()
    if not r:
        return False, 0
    key = rate_otp_key(email)
    try:
        ttl = await r.ttl(key)
        if ttl > 0:
            return True, ttl
        await r.setex(key, TTL_RATE_OTP, "1")
        return False, 0
    except Exception as e:
        logger.warning(f"rate_limit_otp_check failed [{email}]: {e}")
        return False, 0


async def rate_limit_login_check(ip: str, max_attempts: int = 10) -> tuple[bool, int]:
    """
    Sliding counter for login attempts per IP (15-min window).
    Returns (is_blocked, attempt_count).
    Returns (False, 0) if Redis is unavailable — fail open.
    """
    r = _get_redis()
    if not r:
        return False, 0
    key = rate_login_key(ip)
    try:
        count = await r.incr(key)
        if count == 1:
            await r.expire(key, TTL_RATE_LOGIN)
        return count >= max_attempts, count
    except Exception as e:
        logger.warning(f"rate_limit_login_check failed [{ip}]: {e}")
        return False, 0


async def rate_limit_login_reset(ip: str) -> None:
    """Clear login counter after successful auth."""
    await cache_delete(rate_login_key(ip))