"""Outbound email over the existing Gmail mailbox. No new vendor."""
import smtplib
import ssl
from email.message import EmailMessage

from .config import settings


def send_email(to_addr: str, subject: str, html_body: str, text_body: str = "") -> None:
    msg = EmailMessage()
    msg["From"] = f"{settings.mail_from_name} <{settings.gmail_user}>"
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(text_body or "Please open this message in an HTML-capable client.")
    msg.add_alternative(html_body, subtype="html")

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
        server.login(settings.gmail_user, settings.gmail_app_password)
        server.send_message(msg)
