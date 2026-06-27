"""Central brand registry for the multi-tenant SOP platform.

One deployment serves three brands, each on its own host:

    sops.gourmiz.fr      -> gourmiz       (Google OAuth: Les Bonnes Choses)
    sops.essenciagua.fr  -> essenciagua   (Google OAuth: Les Bonnes Choses)
    sops.sablesienne.com -> sablesienne   (Google OAuth: Sablésienne)

The active brand is derived from the request host (see ``brand_for_host``) and
forced — it is never user-selectable. Every SOP query is scoped to the active
brand, the look is themed per brand (static/design-tokens.css, keyed by
``<body data-brand="...">``), and the Google OAuth provider + allowed email
domains are chosen per brand.

The colors here mirror static/design-tokens.css (kept in sync by hand).
"""
import os

# OAuth provider keys -> config section (see config/env_loader.py). The OAuth
# clients are registered once under these short keys in auth/__init__.py.
PROVIDERS = {
    'lbc': 'google-auth-lesbonneschoses',
    'sab': 'google-auth-sablesienne',
}

# Brand registry. Keys are the canonical brand ids used everywhere (DB columns,
# data-brand attribute, logo folder static/brand/<id>/logo.png).
BRANDS = {
    'gourmiz': {
        'id': 'gourmiz',
        'name': 'Gourmiz',
        'host': 'sops.gourmiz.fr',
        'provider': 'lbc',
        'allowed_domains': ['gourmiz.fr', 'gourmiz.bio', 'lesbonneschoses.io'],
        'primary': '#A3C73F',
        'on_primary': '#1f2a07',
        'site': 'https://www.gourmiz.bio/',
        'from_email': 'contact@gourmiz.fr',
    },
    'essenciagua': {
        'id': 'essenciagua',
        'name': 'Essenciagua',
        'host': 'sops.essenciagua.fr',
        'provider': 'lbc',
        'allowed_domains': ['essenciagua.fr', 'essenciagua.com', 'lesbonneschoses.io'],
        'primary': '#012B4B',
        'on_primary': '#ffffff',
        'site': 'https://www.essenciagua.fr/',
        'from_email': 'contact@essenciagua.fr',
    },
    'sablesienne': {
        'id': 'sablesienne',
        'name': 'La Sablésienne',
        'host': 'sops.sablesienne.com',
        'provider': 'sab',
        'allowed_domains': ['sablesienne.com'],
        'primary': '#891358',
        'on_primary': '#ffffff',
        'site': 'https://www.sablesienne.com/',
        'from_email': 'administratif@sablesienne.com',
    },
}

# host (lower, no port) -> brand id
DOMAIN_BRAND_MAP = {b['host']: bid for bid, b in BRANDS.items()}

# Brand used when the host isn't a known brand host (local dev). Override with
# SOPS_DEFAULT_BRAND so each brand can be exercised locally.
DEFAULT_BRAND = os.environ.get('SOPS_DEFAULT_BRAND', 'sablesienne')


def brand_for_host(host):
    """Return the brand id for a request host, falling back to DEFAULT_BRAND."""
    if host:
        hostname = host.split(':', 1)[0].strip().lower()
        if hostname in DOMAIN_BRAND_MAP:
            return DOMAIN_BRAND_MAP[hostname]
    return DEFAULT_BRAND if DEFAULT_BRAND in BRANDS else 'sablesienne'


def get_brand(brand_id):
    return BRANDS.get(brand_id) or BRANDS['sablesienne']


def provider_for_brand(brand_id):
    return get_brand(brand_id)['provider']


def allowed_domains_for_brand(brand_id):
    return get_brand(brand_id)['allowed_domains']
