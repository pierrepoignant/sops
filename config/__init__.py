from flask import Blueprint

config_bp = Blueprint('config', __name__, url_prefix='/config')


def initialize_config(app):
    """Initialize configuration from environment variables."""
    from .env_loader import get_env_var, ENV_VAR_KEYS

    for section, keys in ENV_VAR_KEYS.items():
        if section not in app.config:
            app.config[section] = {}
        for key in keys:
            env_val = get_env_var(section, key)
            if env_val is not None:
                app.config[section][key] = env_val
