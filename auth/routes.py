import hashlib
import secrets
from datetime import datetime, timedelta

from flask import (render_template, redirect, url_for, flash, session, request,
                   g, current_app)
from flask_login import login_user, logout_user, login_required, current_user
from . import auth_bp
from .models import User, EmailLoginCode
from .email_sender import send_login_code_email
from init_db import db
from brands import (get_brand, provider_for_brand, allowed_domains_for_brand)

CODE_TTL_MINUTES = 10
MAX_ATTEMPTS = 5
MIN_SECONDS_BETWEEN_REQUESTS = 45


def _brand():
    return getattr(g, 'brand', None) or 'sablesienne'


def _hash_code(code):
    return hashlib.sha256(code.encode('utf-8')).hexdigest()


def _email_allowed(email, brand=None):
    domains = allowed_domains_for_brand(brand or _brand())
    if not domains:
        return True
    return email.rsplit('@', 1)[-1].lower() in {d.lower() for d in domains}


def _default_group():
    """Attach new self-provisioned users to the default 'staff' group."""
    from administration.models import Group
    grp = Group.query.filter_by(name='staff').first()
    if not grp:
        grp = Group(name='staff', description='Utilisateurs')
        db.session.add(grp)
    return grp


@auth_bp.route('/login')
def login():
    if current_user.is_authenticated:
        return redirect(url_for('home'))
    return render_template('auth/login.html')


@auth_bp.route('/login/google')
def login_google():
    oauth = current_app.extensions.get('oauth')
    provider = provider_for_brand(_brand())
    client = oauth.create_client(f'google_{provider}') if oauth else None
    if not client:
        flash('Google OAuth not configured', 'error')
        return redirect(url_for('auth.login'))
    redirect_uri = url_for('auth.google_callback', _external=True)
    return client.authorize_redirect(redirect_uri)


@auth_bp.route('/login/google/callback')
def google_callback():
    brand_id = _brand()
    brand = get_brand(brand_id)
    oauth = current_app.extensions.get('oauth')
    provider = provider_for_brand(brand_id)
    client = oauth.create_client(f'google_{provider}') if oauth else None
    if not client:
        flash('Google OAuth not configured', 'error')
        return redirect(url_for('auth.login'))

    try:
        token = client.authorize_access_token()
    except Exception:
        flash("Erreur d'authentification Google. Veuillez réessayer.", 'error')
        return redirect(url_for('auth.login'))
    user_info = token.get('userinfo')
    if not user_info:
        flash('Failed to get user info from Google', 'error')
        return redirect(url_for('auth.login'))

    email = user_info['email'].strip().lower()

    # Restrict access to this brand's allowed email domain(s).
    if not _email_allowed(email, brand_id):
        flash(f"Accès réservé aux comptes {brand['name']}.", 'error')
        return redirect(url_for('auth.login'))

    user = User.query.filter(db.func.lower(User.email) == email).first()

    if not user:
        # Auto-provision: anyone with an allowed-domain email may log in. New
        # accounts are simple users (staff) and join the default group. Only
        # when the system has no users at all does the very first account
        # bootstrap as admin (so a fresh install isn't locked out).
        is_first = User.query.count() == 0
        base = (user_info.get('name') or email.split('@')[0]).replace(' ', '').lower()
        username = base
        suffix = 1
        while User.query.filter_by(username=username).first():
            suffix += 1
            username = f'{base}{suffix}'
        user = User(
            username=username,
            email=email,
            role='admin' if is_first else 'staff',
            oauth_provider='google',
            oauth_id=user_info.get('sub'),
            first_name=user_info.get('given_name'),
            last_name=user_info.get('family_name'),
        )
        db.session.add(user)
        if not is_first:
            user.groups.append(_default_group())
    else:
        # Existing user — fill in Google identity the first time they connect.
        if not user.oauth_id:
            user.oauth_provider = 'google'
            user.oauth_id = user_info.get('sub')
        if not user.first_name and user_info.get('given_name'):
            user.first_name = user_info.get('given_name')
        if not user.last_name and user_info.get('family_name'):
            user.last_name = user_info.get('family_name')

    user.last_login = datetime.utcnow()
    db.session.commit()
    login_user(user)
    return redirect(url_for('home'))


# --- Email code login (for accounts not on the Google OAuth domain) ---

@auth_bp.route('/login/email', methods=['GET', 'POST'])
def login_email_request():
    if current_user.is_authenticated:
        return redirect(url_for('home'))
    if request.method == 'GET':
        return render_template('auth/login_email.html')

    email = (request.form.get('email') or '').strip().lower()
    if not email or '@' not in email:
        flash('Adresse e-mail invalide.', 'error')
        return render_template('auth/login_email.html', email=email)

    # Only known accounts can request a code. Don't reveal whether it exists.
    user = User.query.filter(db.func.lower(User.email) == email).first()
    if user:
        recent = (EmailLoginCode.query.filter_by(email=email)
                  .order_by(EmailLoginCode.created_at.desc()).first())
        if recent and (datetime.utcnow() - recent.created_at).total_seconds() < MIN_SECONDS_BETWEEN_REQUESTS:
            flash('Un code vient déjà d’être envoyé. Patientez quelques instants.', 'info')
            return redirect(url_for('auth.login_email_verify', e=email))
        code = f'{secrets.randbelow(1_000_000):06d}'
        db.session.add(EmailLoginCode(
            email=email,
            code_hash=_hash_code(code),
            expires_at=datetime.utcnow() + timedelta(minutes=CODE_TTL_MINUTES),
            ip_address=request.remote_addr,
        ))
        db.session.commit()
        try:
            send_login_code_email(email, code, _brand())
        except Exception:
            flash("Impossible d'envoyer l'e-mail pour le moment. Réessayez plus tard.", 'error')
            return render_template('auth/login_email.html', email=email)

    flash('Si un compte existe pour cette adresse, un code vient d’être envoyé.', 'info')
    return redirect(url_for('auth.login_email_verify', e=email))


@auth_bp.route('/login/email/verify', methods=['GET', 'POST'])
def login_email_verify():
    if current_user.is_authenticated:
        return redirect(url_for('home'))
    email = (request.values.get('email') or request.args.get('e') or '').strip().lower()

    if request.method == 'GET':
        return render_template('auth/login_verify.html', email=email)

    code_input = (request.form.get('code') or '').strip()
    rec = (EmailLoginCode.query.filter_by(email=email)
           .filter(EmailLoginCode.consumed_at.is_(None))
           .order_by(EmailLoginCode.created_at.desc()).first())

    if not rec or rec.expires_at < datetime.utcnow():
        flash('Code expiré ou introuvable. Demandez un nouveau code.', 'error')
        return redirect(url_for('auth.login_email_request'))
    if rec.attempts >= MAX_ATTEMPTS:
        rec.consumed_at = datetime.utcnow()
        db.session.commit()
        flash('Trop de tentatives. Demandez un nouveau code.', 'error')
        return redirect(url_for('auth.login_email_request'))
    if not secrets.compare_digest(rec.code_hash, _hash_code(code_input)):
        rec.attempts += 1
        db.session.commit()
        flash(f'Code incorrect. {MAX_ATTEMPTS - rec.attempts} tentative(s) restante(s).', 'error')
        return render_template('auth/login_verify.html', email=email)

    rec.consumed_at = datetime.utcnow()
    user = User.query.filter(db.func.lower(User.email) == email).first()
    if not user:
        db.session.commit()
        flash('Compte introuvable.', 'error')
        return redirect(url_for('auth.login_email_request'))
    user.last_login = datetime.utcnow()
    db.session.commit()
    login_user(user)
    return redirect(url_for('home'))


@auth_bp.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        current_user.first_name = request.form.get('first_name', '').strip()
        current_user.last_name = request.form.get('last_name', '').strip()
        db.session.commit()
        flash('Profil mis à jour.', 'success')
        return redirect(url_for('auth.profile'))
    return render_template('auth/profile.html')


@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('auth.login'))
