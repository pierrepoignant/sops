"""Sync SOP users from the Cadence employee directory.

Cadence (the staff-planning app) is the source of truth for who works here
and in which team. `GET /api/v1/employees` returns the active staff; their
Cadence team maps onto a SOP department:

    stores     -> the department whose name starts with "boutique"
    operations -> the department whose name starts with "operation"
    siege      -> no department (users are still created; allocate manually)

Config: CADENCE__API_URL (e.g. https://cadence.sablesienne.com/api/v1) and
CADENCE__API_KEY (see config/env_loader.py).
"""
import unicodedata

import requests
from flask import current_app

from init_db import db

# Cadence team -> (department name-root to match, name/icon used if the
# department has to be created).
TEAM_TO_DEPT = {
    'stores': ('boutique', 'Boutiques', 'fa-store'),
    'operations': ('operation', 'Opérations', 'fa-industry'),
}


class CadenceSyncError(Exception):
    """User-displayable sync failure."""


def _cfg():
    return current_app.config.get('cadence') or {}


def is_configured():
    return bool(_cfg().get('api_key') and _cfg().get('api_url'))


def _fold(s):
    return ''.join(c for c in unicodedata.normalize('NFD', s or '')
                   if not unicodedata.combining(c)).lower().strip()


def fetch_employees():
    """Active employees from Cadence. Raises CadenceSyncError on failure."""
    cfg = _cfg()
    if not is_configured():
        raise CadenceSyncError("Cadence n'est pas configuré "
                               "(CADENCE__API_URL / CADENCE__API_KEY).")
    base = cfg['api_url'].rstrip('/')
    if not base.endswith('/api/v1'):
        base += '/api/v1'
    url = base + '/employees'
    try:
        resp = requests.get(url, params={'active': 'true'},
                            headers={'X-API-Key': cfg['api_key']}, timeout=20)
    except requests.RequestException as e:
        raise CadenceSyncError(f'Impossible de joindre Cadence : {e}')
    if resp.status_code == 401:
        raise CadenceSyncError('Clé API Cadence invalide ou révoquée.')
    if resp.status_code != 200:
        raise CadenceSyncError(f'Cadence a répondu {resp.status_code}.')
    try:
        data = resp.json()
    except ValueError:
        raise CadenceSyncError('Réponse Cadence illisible.')
    if not isinstance(data, list):
        raise CadenceSyncError('Réponse Cadence inattendue.')
    return data


def _dept_for_team(team, brand, cache, created_names):
    """Resolve (and create if needed) the SOP department for a Cadence team.
    Returns a SopDepartment or None (e.g. for 'siege')."""
    from help.models import SopDepartment
    if team in cache:
        return cache[team]
    mapping = TEAM_TO_DEPT.get(team)
    if not mapping:
        cache[team] = None
        return None
    root, create_name, icon = mapping
    dept = None
    for d in SopDepartment.query.filter_by(brand=brand).all():
        if _fold(d.name).startswith(root) or _fold(d.slug).startswith(root):
            dept = d
            break
    if dept is None:
        nxt = (db.session.query(
            db.func.coalesce(db.func.max(SopDepartment.sort_order), -1))
            .filter_by(brand=brand).scalar() or -1) + 1
        dept = SopDepartment(brand=brand, slug=_fold(create_name).replace(' ', '-'),
                             name=create_name, icon=icon, sort_order=nxt)
        db.session.add(dept)
        db.session.flush()
        created_names.append(create_name)
    cache[team] = dept
    return dept


def _unique_username(base):
    from auth.models import User
    base = (base or 'user').split('@', 1)[0].replace(' ', '').lower() or 'user'
    username = base
    suffix = 1
    while User.query.filter_by(username=username).first():
        suffix += 1
        username = f'{base}{suffix}'
    return username


def sync_users(brand):
    """Fetch Cadence employees and upsert SOP users. Returns a stats dict.

    - New employees (with an email) become 'staff' users allocated to the
      mapped department.
    - Existing users get their department updated when the mapping yields
      one; roles and manual allocations of unmapped teams are never touched.
    - Employees without an email, and duplicate emails (shared mailboxes),
      are skipped and counted.
    """
    from auth.models import User
    employees = fetch_employees()

    dept_cache = {}
    seen_emails = set()
    stats = {'created': 0, 'updated': 0, 'unchanged': 0,
             'no_email': 0, 'duplicates': 0, 'created_departments': []}

    for e in employees:
        email = (e.get('email') or '').strip().lower()
        name = (e.get('name') or '').strip()
        team = (e.get('team') or '').strip().lower()
        if not email or '@' not in email:
            stats['no_email'] += 1
            continue
        if email in seen_emails:
            stats['duplicates'] += 1
            continue
        seen_emails.add(email)

        dept = _dept_for_team(team, brand, dept_cache,
                              stats['created_departments'])
        dept_slug = dept.slug if dept else None

        user = User.query.filter(db.func.lower(User.email) == email).first()
        if user is None:
            first, _, last = name.partition(' ')
            user = User(username=_unique_username(email), email=email,
                        first_name=first or None, last_name=last or None,
                        role='staff', department=dept_slug)
            db.session.add(user)
            stats['created'] += 1
            continue

        changed = False
        if dept_slug and user.department != dept_slug:
            user.department = dept_slug
            changed = True
        if not user.first_name and name:
            first, _, last = name.partition(' ')
            user.first_name = first or None
            user.last_name = user.last_name or (last or None)
            changed = True
        stats['updated' if changed else 'unchanged'] += 1

    db.session.commit()
    return stats
