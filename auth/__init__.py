from flask import Blueprint

auth_bp = Blueprint('auth', __name__, url_prefix='/auth',
                    template_folder='templates')


def init_oauth(app):
    """Register one Google OAuth client per provider (lbc, sab).

    Both clients are registered up-front; the active one is chosen per request
    from the host's brand (see brands.py). gourmiz + essenciagua use the Les
    Bonnes Choses client; sablesienne uses the Sablésienne client."""
    from authlib.integrations.flask_client import OAuth
    from brands import PROVIDERS

    oauth = OAuth(app)
    for short_key, config_key in PROVIDERS.items():
        cfg = app.config.get(config_key, {}) or {}
        if cfg.get('client_id'):
            oauth.register(
                name=f'google_{short_key}',
                client_id=cfg.get('client_id'),
                client_secret=cfg.get('client_secret'),
                server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
                client_kwargs={'scope': 'openid email profile'},
            )

    app.extensions['oauth'] = oauth
    return oauth


from . import routes  # noqa: E402, F401
