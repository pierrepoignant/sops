"""Module-level permissions.

Each feature blueprint in the app is a "module". Users inherit access to
modules through Group membership. Admin users (is_admin=True) bypass checks.

To add a module:
  1. Create a blueprint whose name matches the module id (e.g. `stores`).
  2. Register it in create_app().
  3. Add an entry below with a label + Font Awesome icon.
  4. Add a sidebar section in templates/base.html guarded by has_module('<id>').
The before-request hook then gates the whole blueprint automatically.
"""
from functools import wraps
from flask import request, abort, redirect, url_for
from flask_login import current_user


# Canonical module registry. Keys match blueprint names one-for-one so that
# the before-request hook can infer the module id from `request.blueprint`.
MODULES = {
    'media': {'label': 'Médias', 'icon': 'fa-photo-video'},
    'administration': {'label': 'Administration', 'icon': 'fa-shield-alt'},
}

# Blueprints that shouldn't be gated (login flows, static files, and the help
# center — which is available to every authenticated user).
_OPEN_BLUEPRINTS = {'auth', 'static', 'help'}

# Specific endpoints that do their own authentication. Media files are served
# to any authenticated user so images embedded in help articles always render,
# even for users without the `media` management module. The stats page does
# its own check because department owners (not module holders) may see their
# department's numbers.
_OPEN_ENDPOINTS = {'media.serve_file', 'administration.stats'}


def user_has_module_access(user, module_id):
    if not user or not user.is_authenticated:
        return False
    if getattr(user, 'is_admin', False):
        return True
    for g in getattr(user, 'groups', []) or []:
        for gm in g.modules:
            if gm.module_id == module_id:
                return True
    return False


def user_modules(user):
    """Return the set of module ids this user has access to (admins get all)."""
    if not user or not user.is_authenticated:
        return set()
    if getattr(user, 'is_admin', False):
        return set(MODULES.keys())
    ids = set()
    for g in getattr(user, 'groups', []) or []:
        for gm in g.modules:
            ids.add(gm.module_id)
    return ids


def require_module(module_id):
    """Decorator — for cross-module endpoints that don't belong to a blueprint
    whose name matches the module id."""
    def deco(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for('auth.login'))
            if not user_has_module_access(current_user, module_id):
                abort(403)
            return f(*args, **kwargs)
        return wrapped
    return deco


def enforce_module_access():
    """Flask before_request hook. Gates requests based on the current
    blueprint name when it maps to a known module."""
    ep = request.endpoint
    if not ep:
        return None
    if ep in _OPEN_ENDPOINTS:
        return None
    bp = request.blueprint
    if bp is None or bp in _OPEN_BLUEPRINTS:
        return None
    if bp not in MODULES:
        return None  # unmapped blueprint, let it through
    if not current_user.is_authenticated:
        return redirect(url_for('auth.login'))
    if user_has_module_access(current_user, bp):
        return None
    abort(403)
