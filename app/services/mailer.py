"""
Outgoing email: account activation, rejection, and new-registration notices.

Optional by design. With SMTP_HOST unset nothing is sent — each message is
logged instead — so the approval workflow behaves identically without a mail
server, and the log shows what would have gone out.

Sending happens on a daemon thread: an SMTP server can take seconds (or time
out), and an administrator clicking "Approve" must not wait for it. A failure
is logged, never raised — the account change already happened and is what
matters; the email is a courtesy.
"""
from __future__ import annotations

import os
import smtplib
import ssl
import threading
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from app import config
from app.utils.logging import SharedLogger

logger = SharedLogger().get_logger()


def _app_url(path: str) -> str | None:
    """An absolute link into the web app, or None when SPA_ROOT_URL is unset."""
    root = os.getenv('SPA_ROOT_URL', '').strip().rstrip('/')
    if not root.startswith(('http://', 'https://')):
        return None
    return f"{root}{path}"


def _deliver(message: EmailMessage) -> bool:
    try:
        if config.SMTP_SECURITY == 'ssl':
            server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT,
                                      timeout=config.SMTP_TIMEOUT_SECONDS,
                                      context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT,
                                  timeout=config.SMTP_TIMEOUT_SECONDS)
        with server:
            if config.SMTP_SECURITY == 'starttls':
                server.starttls(context=ssl.create_default_context())
            if config.SMTP_USERNAME:
                server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
            server.send_message(message)
        logger.info(f"Email sent: {message['Subject']!r} to {message['To']}")
        return True
    except Exception as e:
        logger.warning(f"Email {message['Subject']!r} to {message['To']} failed: "
                       f"{e.__class__.__name__}: {e}")
        return False


def send_mail(to: list[str], subject: str, body: str, *, background: bool = True) -> bool:
    """Send a plain-text email. Returns False when not configured (or, in the
    foreground, when sending failed)."""
    recipients = [addr for addr in to if addr]
    if not recipients:
        return False
    if not config.SMTP_HOST or not config.MAIL_FROM:
        logger.info(f"Email not configured (SMTP_HOST/MAIL_FROM unset); would send "
                    f"{subject!r} to {', '.join(recipients)}")
        return False

    message = EmailMessage()
    message['Subject'] = subject
    message['From'] = formataddr((config.MAIL_APP_NAME, config.MAIL_FROM))
    message['To'] = ', '.join(recipients)
    message['Message-ID'] = make_msgid(domain=config.MAIL_FROM.rpartition('@')[2] or None)
    message.set_content(body)

    if background:
        threading.Thread(target=_deliver, args=(message,), name='mailer', daemon=True).start()
        return True
    return _deliver(message)


# ── Messages ───────────────────────────────────────────────────

def _sign_off() -> str:
    return f"\n\n— {config.MAIL_APP_NAME}\n(Automated message; replies are not monitored.)"


def send_account_activated(email: str, username: str) -> bool:
    """The activation email: the account is approved and can now sign in."""
    login = _app_url('/login')
    body = (
        f"Hello {username},\n\n"
        f"Your {config.MAIL_APP_NAME} account has been approved and is now active."
        + (f"\n\nSign in here: {login}" if login else "\n\nYou can now sign in.")
        + f"\nUsername: {username}"
        + _sign_off()
    )
    return send_mail([email], f"Your {config.MAIL_APP_NAME} account is active", body)


def send_account_rejected(email: str, username: str, reason: str | None = None) -> bool:
    body = (
        f"Hello {username},\n\n"
        f"Your request for a {config.MAIL_APP_NAME} account was not approved."
        + (f"\n\nReason: {reason}" if reason else "")
        + "\n\nIf you think this is a mistake, contact the administrator who manages access."
        + _sign_off()
    )
    return send_mail([email], f"Your {config.MAIL_APP_NAME} account request", body)


def send_new_registration(username: str, email: str) -> bool:
    """Tell the administrators someone is waiting for approval."""
    admins = sorted(config.admin_emails())
    review = _app_url('/admin/users')
    body = (
        f"A new account is waiting for approval.\n\n"
        f"Username: {username}\nEmail: {email}"
        + (f"\n\nReview it here: {review}" if review else "")
        + _sign_off()
    )
    return send_mail(admins, f"{config.MAIL_APP_NAME}: new account awaiting approval", body)
