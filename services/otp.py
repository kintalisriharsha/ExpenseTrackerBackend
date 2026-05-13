"""
services/otp.py
───────────────
OTP generation, hashing, and email delivery.

generate_otp / hash_otp / verify_otp_hash are thin wrappers that delegate
to auth.auth so there is a single source of truth.  Email sending lives here.
"""

import hashlib
import secrets
import string
import aiosmtplib
import os
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

SMTP_HOST     = os.getenv("SMTP_HOST",     "smtp.gmail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER     = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
FROM_NAME     = os.getenv("FROM_NAME", "Expense Tracker")


# ── OTP helpers ────────────────────────────────────────────────────────────────

def generate_otp(length: int = 6) -> str:
    """
    Cryptographically secure OTP.
    Uses secrets.choice — NOT random.choices — so the output is
    unpredictable even if the attacker knows the current timestamp.
    """
    return "".join(secrets.choice(string.digits) for _ in range(length))


def hash_otp(otp: str) -> str:
    return hashlib.sha256(otp.encode()).hexdigest()


def verify_otp_hash(plain_otp: str, stored_hash: str) -> bool:
    """
    Constant-time comparison via secrets.compare_digest.
    Prevents timing-based side-channel attacks.
    """
    return secrets.compare_digest(hash_otp(plain_otp), stored_hash)


# ── HTML template ──────────────────────────────────────────────────────────────

def _build_otp_html(otp: str) -> str:
    digits = "".join(
        f'<td style="width:44px;height:56px;background:#ffffff;border:1px solid #E2E8F0;'
        f'border-radius:8px;text-align:center;vertical-align:middle;font-size:26px;'
        f'font-weight:600;color:#0F172A;font-family:\'Courier New\',monospace;padding:0;">'
        f'{ch}</td><td style="width:6px"></td>'
        for ch in otp
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Your sign-in code</title>
</head>
<body style="margin:0;padding:0;background:#F8FAFC;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#F8FAFC;padding:40px 16px">
    <tr><td align="center">
      <table width="520" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:16px;overflow:hidden;border:1px solid #E2E8F0">

        <!-- Header -->
        <tr>
          <td style="background:#1152D4;padding:32px 40px;text-align:center">
            <div style="font-size:20px;font-weight:600;color:#ffffff;margin:0">Expense Tracker</div>
            <div style="font-size:13px;color:rgba(255,255,255,0.70);margin-top:4px">Sign in to your account</div>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="padding:36px 40px">
            <p style="margin:0 0 6px;font-size:15px;color:#0F172A">Hi there,</p>
            <p style="margin:0 0 28px;font-size:14px;color:#64748B;line-height:1.6">
              Use the code below to sign in. It is valid for <strong style="color:#0F172A">10 minutes</strong>
              and can only be used once.
            </p>

            <p style="margin:0 0 10px;font-size:11px;font-weight:600;color:#94A3B8;letter-spacing:0.8px;text-transform:uppercase">
              Your one-time code
            </p>

            <table cellpadding="0" cellspacing="0" width="100%" style="background:#F1F5F9;border:1px solid #E2E8F0;border-radius:12px;padding:20px 24px;margin-bottom:24px">
              <tr>
                <td><table cellpadding="0" cellspacing="0"><tr>{digits}</tr></table></td>
                <td align="right" style="font-size:13px;color:#94A3B8;white-space:nowrap">&#x23F1; 10 min</td>
              </tr>
            </table>

            <table cellpadding="0" cellspacing="0" width="100%" style="margin-bottom:24px">
              <tr>
                <td width="3" style="background:#1152D4;border-radius:2px"></td>
                <td style="padding:12px 16px;background:#F1F5F9;border-radius:0 8px 8px 0;font-size:13px;color:#64748B;line-height:1.6">
                  If you didn&#39;t request this code, you can safely ignore this email. Your account won&#39;t be affected.
                </td>
              </tr>
            </table>

            <table cellpadding="0" cellspacing="0" width="100%" style="margin-bottom:20px">
              <tr><td style="border-top:1px solid #E2E8F0"></td></tr>
            </table>

            <p style="margin:0;font-size:12px;color:#94A3B8;line-height:1.7">
              For your security, never share this code with anyone, including Expense Tracker support.
            </p>
          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="padding:18px 40px;border-top:1px solid #E2E8F0">
            <table cellpadding="0" cellspacing="0" width="100%">
              <tr>
                <td style="font-size:13px;font-weight:500;color:#64748B">
                  <span style="display:inline-block;width:8px;height:8px;background:#1152D4;border-radius:50%;margin-right:6px;vertical-align:middle"></span>
                  Expense Tracker
                </td>
                <td align="right" style="font-size:12px;color:#CBD5E1">&copy; 2026</td>
              </tr>
            </table>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _build_otp_plain(otp: str) -> str:
    return (
        f"EXPENSE TRACKER — SIGN-IN CODE\n\n"
        f"Your one-time code: {otp}\n\n"
        f"Valid for 10 minutes. Do not share it with anyone.\n\n"
        f"If you didn't request this, ignore this email — your account is safe."
    )


# ── Email sender ───────────────────────────────────────────────────────────────

async def send_otp_email(to_email: str, otp: str) -> None:
    if not SMTP_USER or not SMTP_PASSWORD:
        raise RuntimeError("SMTP_USER and SMTP_PASSWORD must be set in .env")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"{otp} is your Expense Tracker code"
    msg["From"]    = f"{FROM_NAME} <{SMTP_USER}>"
    msg["To"]      = to_email

    msg.attach(MIMEText(_build_otp_plain(otp), "plain"))
    msg.attach(MIMEText(_build_otp_html(otp),  "html"))

    try:
        await aiosmtplib.send(
            msg,
            hostname  = SMTP_HOST,
            port      = SMTP_PORT,
            username  = SMTP_USER,
            password  = SMTP_PASSWORD,
            start_tls = True,
        )
        logger.warning(f"OTP email sent to {to_email}")
    except Exception as e:
        logger.error(f"SMTP failure for {to_email}: {e}")
        raise
