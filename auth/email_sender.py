"""Send transactional email via SendGrid's SMTP relay.

Config from the `sendgrid` section (SENDGRID__API_KEY, SENDGRID__FROM_EMAIL,
SENDGRID__FROM_NAME). If no API key is configured the code is logged instead of
sent, so local dev works without credentials.
"""
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from flask import current_app

log = logging.getLogger(__name__)

SENDGRID_HOST = 'smtp.sendgrid.net'
SENDGRID_PORT = 587
SENDGRID_USER = 'apikey'
DEFAULT_SENDER_EMAIL = 'administratif@sablesienne.com'


def send_email(to_emails, subject, body, brand_id='sablesienne'):
    """Send one plain-text email to one or many recipients via SendGrid.
    Without an API key the message is logged instead, so local dev works.
    Returns the number of recipients actually sent to."""
    from brands import get_brand
    brand = get_brand(brand_id)

    if isinstance(to_emails, str):
        to_emails = [to_emails]
    to_emails = [e for e in to_emails if e]
    if not to_emails:
        return 0

    cfg = current_app.config.get('sendgrid', {}) or {}
    api_key = cfg.get('api_key')
    sender_email = cfg.get('from_email') or brand.get('from_email') or DEFAULT_SENDER_EMAIL
    sender_name = cfg.get('from_name') or f"SOP — {brand['name']}"

    if not api_key:
        log.warning('SENDGRID__API_KEY not configured; would send %r to %s',
                    subject, to_emails)
        print(f'[DEV] SOP email to {to_emails}: {subject}\n{body}', flush=True)
        return 0

    sent = 0
    with smtplib.SMTP(SENDGRID_HOST, SENDGRID_PORT, timeout=15) as s:
        s.starttls()
        s.login(SENDGRID_USER, api_key)
        for email in to_emails:
            msg = MIMEMultipart('mixed')
            msg['From'] = f'{sender_name} <{sender_email}>'
            msg['To'] = email
            msg['Subject'] = subject
            msg.attach(MIMEText(body, 'plain', 'utf-8'))
            try:
                s.sendmail(sender_email, email, msg.as_string())
                sent += 1
            except smtplib.SMTPException:
                log.exception('Failed to send %r to %s', subject, email)
    return sent


def send_login_code_email(email, code, brand_id='sablesienne'):
    from brands import get_brand
    brand = get_brand(brand_id)
    sender_name = f"SOP — {brand['name']}"

    cfg = current_app.config.get('sendgrid', {}) or {}
    if not cfg.get('api_key'):
        log.warning('SENDGRID__API_KEY not configured; login code for %s is %s', email, code)
        print(f'[DEV] SOP login code for {email}: {code}', flush=True)
        return

    body = (
        f"Bonjour,\n\n"
        f"Votre code de connexion à l'espace SOP {brand['name']} est : {code}\n\n"
        f"Ce code est valable 10 minutes. Si vous n'êtes pas à l'origine de "
        f"cette demande, vous pouvez ignorer cet e-mail.\n\n"
        f"— {cfg.get('from_name') or sender_name}"
    )
    send_email(email, f'Votre code de connexion SOP {brand["name"]}', body,
               brand_id=brand_id)
