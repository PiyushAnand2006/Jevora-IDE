"""Email delivery for authentication flows.

SMTP is used when configured. In non-production environments without SMTP, the
message is retained in an in-memory outbox and the OTP is logged so local
development and tests can exercise the complete flow without a mail provider.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage

from backend.config import Settings

log = logging.getLogger(__name__)


@dataclass
class EmailOutboxMessage:
    recipient: str
    subject: str
    body: str


@dataclass
class EmailService:
    settings: Settings
    outbox: list[EmailOutboxMessage] = field(default_factory=list)

    async def send_password_reset_otp(self, recipient: str, otp: str, ttl_minutes: int) -> None:
        subject = "Your Jevora IDE password reset code"
        body = (
            "We received a request to reset your Jevora IDE password.\n\n"
            f"Your one-time verification code is: {otp}\n\n"
            f"This code expires in {ttl_minutes} minutes. If you did not request this, you can ignore this email."
        )
        if self.settings.smtp_host:
            await asyncio.to_thread(self._send_smtp, recipient, subject, body)
            return
        if self.settings.environment == "production":
            raise RuntimeError("SMTP is not configured for password reset email")
        self.outbox.append(EmailOutboxMessage(recipient, subject, body))
        log.info("Password reset email queued for %s (development outbox)", recipient)
        log.info("Development password reset OTP for %s: %s", recipient, otp)

    def _send_smtp(self, recipient: str, subject: str, body: str) -> None:
        host = self.settings.smtp_host
        if not host:
            raise RuntimeError("SMTP is not configured")
        message = EmailMessage()
        message["From"] = f"{self.settings.smtp_from_name} <{self.settings.smtp_from_email}>"
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(body)
        smtp_client = smtplib.SMTP_SSL if self.settings.smtp_use_ssl else smtplib.SMTP
        with smtp_client(host, self.settings.smtp_port, timeout=20) as server:
            if self.settings.smtp_use_tls and not self.settings.smtp_use_ssl:
                server.starttls()
            if self.settings.smtp_username:
                server.login(self.settings.smtp_username, self.settings.smtp_password.get_secret_value()
                             if self.settings.smtp_password else "")
            server.send_message(message)
