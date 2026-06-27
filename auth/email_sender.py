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


def send_login_code_email(email, code, brand_id='sablesienne'):
    from brands import get_brand
    brand = get_brand(brand_id)
    sender_name = f"SOP — {brand['name']}"

    cfg = current_app.config.get('sendgrid', {}) or {}
    api_key = cfg.get('api_key')
    sender_email = cfg.get('from_email') or brand.get('from_email') or DEFAULT_SENDER_EMAIL
    sender_name = cfg.get('from_name') or sender_name

    body = (
        f"Bonjour,\n\n"
        f"Votre code de connexion à l'espace SOP {brand['name']} est : {code}\n\n"
        f"Ce code est valable 10 minutes. Si vous n'êtes pas à l'origine de "
        f"cette demande, vous pouvez ignorer cet e-mail.\n\n"
        f"— {sender_name}"
    )

    if not api_key:
        log.warning('SENDGRID__API_KEY not configured; login code for %s is %s', email, code)
        print(f'[DEV] SOP login code for {email}: {code}', flush=True)
        return

    msg = MIMEMultipart('mixed')
    msg['From'] = f'{sender_name} <{sender_email}>'
    msg['To'] = email
    msg['Subject'] = f'Votre code de connexion SOP {brand["name"]}'
    msg.attach(MIMEText(body, 'plain', 'utf-8'))

    with smtplib.SMTP(SENDGRID_HOST, SENDGRID_PORT, timeout=15) as s:
        s.starttls()
        s.login(SENDGRID_USER, api_key)
        s.sendmail(sender_email, email, msg.as_string())
