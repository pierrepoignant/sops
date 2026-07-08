"""
Environment Variable Loader for Stores Configuration

Loads secret configuration values from environment variables following the
naming convention: SECTION__KEY (double underscore separator).

Example: the section 'database-ovh' key 'password' is read from the env var
DATABASE_OVH__PASSWORD; the section 'google-auth-sablesienne' key 'client_id'
from GOOGLE_AUTH_SABLESIENNE__CLIENT_ID.

To add a new secret, register its section + keys in ENV_VAR_KEYS below, then
read it at runtime via app.config['<section>']['<key>'].
"""

import os
from typing import Optional


def section_to_env_prefix(section: str) -> str:
    return section.replace('-', '_').upper()


def get_env_var(section: str, key: str) -> Optional[str]:
    prefix = section_to_env_prefix(section)
    env_name = f"{prefix}__{key.upper()}"
    return os.environ.get(env_name)


def get_env_var_name(section: str, key: str) -> str:
    prefix = section_to_env_prefix(section)
    return f"{prefix}__{key.upper()}"


# Registry of secret keys per section.
ENV_VAR_KEYS = {
    # Application databases (MySQL on OVH). Pick one with run.py -d <name>.
    'database-production': ['host', 'user', 'password', 'name', 'port'],
    'database-ovh': ['host', 'user', 'password', 'name', 'port'],
    'database-local': ['host', 'user', 'password', 'name', 'port'],

    # Google OAuth (Sign in with Google). One section per brand; the active
    # provider is selected per request from the host's brand (see brands.py).
    'google-auth-sablesienne': ['client_id', 'client_secret'],
    'google-auth-lesbonneschoses': ['client_id', 'client_secret'],

    # OVH Object Storage (S3-compatible) — media library
    'ovh': ['endpoint_url', 'bucket', 'region', 'access_key', 'secret_key'],

    # SendGrid (SMTP relay) — email login codes
    'sendgrid': ['api_key', 'from_email', 'from_name'],

    # Anthropic Claude API — AI-generated training quizzes
    'anthropic': ['api_key'],

    # Cadence (staff planning app) — employee directory sync
    'cadence': ['api_key', 'api_url'],
}
