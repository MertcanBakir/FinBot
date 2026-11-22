import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

class EmailService:
    def __init__(self):
        self.user = settings.EMAIL_USER
        self.password = settings.EMAIL_APP_PASSWORD
        self.to = settings.EMAIL_TO
        self.host = settings.SMTP_HOST
        self.port = settings.SMTP_PORT

    def is_configured(self) -> bool:
        return bool(self.user and self.password and self.to)

    def send_email(
        self,
        subject: str,
        body_text: str,
        warnings: Optional[List[str]] = None,
        raw_data_summary: Optional[str] = None,
        fetch_time: Optional[str] = None,
        *,
        body_html: Optional[str] = None,
    ) -> bool:
        if not self.is_configured():
            logger.warning("SMTP email service is not configured (EMAIL_USER, EMAIL_APP_PASSWORD, or EMAIL_TO missing). Skipping email delivery.")
            return False

        full_body = [body_text]

        if fetch_time or (warnings and len(warnings) > 0) or raw_data_summary:
            full_body.append("\n" + "=" * 40)

        if fetch_time:
            full_body.append(f"\nVeri Çekilme Saati: {fetch_time}")

        if warnings and len(warnings) > 0:
            full_body.append("\n⚠️ UYARILAR:")
            for w in warnings:
                full_body.append(f"- {w}")

        if raw_data_summary:
            full_body.append("\n📊 HAM PİYASA VERİLERİ:")
            full_body.append(raw_data_summary)

        content = "\n".join(full_body)

        try:
            if body_html is not None:
                msg = MIMEMultipart("alternative")
                msg["From"] = self.user
                msg["To"] = self.to
                msg["Subject"] = subject
                msg.attach(MIMEText(content, "plain", "utf-8"))
                msg.attach(MIMEText(body_html, "html", "utf-8"))
            else:
                msg = MIMEMultipart()
                msg["From"] = self.user
                msg["To"] = self.to
                msg["Subject"] = subject
                msg.attach(MIMEText(content, "plain", "utf-8"))

            with smtplib.SMTP(self.host, self.port) as server:
                server.starttls()
                server.login(self.user, self.password)
                server.send_message(msg)

            logger.info(f"Email successfully sent to {self.to} with subject: '{subject}'")
            return True
        except Exception as e:
            logger.error(f"Failed to send email: {e}")
            return False
