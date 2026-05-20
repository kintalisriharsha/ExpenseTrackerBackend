
import hashlib
import secrets
import string
import asyncio
from functools import partial


# ── OTP generation ─────────────────────────────────────────────────────────────

def generate_otp(length: int = 6) -> str:
    """
    Cryptographically secure OTP via secrets.choice.
    secrets uses os.urandom under the hood — safe for auth codes.
    This is fast enough to stay synchronous.
    """
    return "".join(secrets.choice(string.digits) for _ in range(length))


# ── Synchronous hashing (use only outside async contexts) ─────────────────────

def hash_otp(otp: str) -> str:
    """SHA-256 hash of a plain OTP.  Sync — do NOT call from an async route."""
    return hashlib.sha256(otp.encode()).hexdigest()


def verify_otp_hash(plain_otp: str, stored_hash: str) -> bool:
    """
    Constant-time comparison via secrets.compare_digest.
    Prevents timing-based side-channel attacks.
    Sync — do NOT call from an async route directly.
    """
    return secrets.compare_digest(hash_otp(plain_otp), stored_hash)


# ── Async wrappers (run CPU-bound hash in thread pool) ────────────────────────

async def hash_otp_async(otp: str) -> str:
    """
    Async-safe version of hash_otp.
    Offloads SHA-256 to the default ThreadPoolExecutor so the event loop
    stays unblocked while hashing.

    Usage:
        hashed = await hash_otp_async(otp)
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, hash_otp, otp)


async def verify_otp_hash_async(plain_otp: str, stored_hash: str) -> bool:
    """
    Async-safe version of verify_otp_hash.
    Offloads both the hash computation and the constant-time compare
    to the thread pool.

    Usage:
        is_valid = await verify_otp_hash_async(plain_otp, user.hashed_otp)
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        partial(verify_otp_hash, plain_otp, stored_hash)
    )