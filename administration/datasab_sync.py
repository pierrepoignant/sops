"""Sync SOP users from DataSab (data.sablesienne.com).

DataSab is the source of truth for who works here. It runs the same admin stack
as this app on the same OVH MySQL server, and its ``/administration/users`` API
is session-protected (Google sign-in), so a server-to-server sync cannot call
it. The sync therefore reads DataSab's ``users`` table directly, read-only, over
a second connection.

The table is introspected rather than assumed: ``email`` is the only column the
sync requires. A name is taken from ``first_name``/``last_name`` or from a
single ``name`` column, a department from ``department`` and an activity flag
from ``is_active`` or ``active`` — whichever of those exist.

Users that have disappeared from DataSab are **deactivated, never deleted**: a
deletion would take their reading acknowledgements, quiz attempts and the visas
they signed with it, which is exactly the traceability a quality platform has to
keep. Three groups are never deactivated:

    * the ``lesbonneschoses.io`` domain — holding-company staff, who are not in
      the Sablésienne directory but must stay in the SOP users;
    * administrators, who are managed by hand (otherwise a sync could lock the
      platform's own admins out);
    * whoever is running the sync.

Config: section ``database-datasab`` (host, user, password, name, port) — see
config/env_loader.py.
"""
import unicodedata

from flask import current_app
from sqlalchemy import create_engine, inspect as sqla_inspect, text

from init_db import db

# Email domains kept in the SOP users whatever DataSab says.
KEEP_DOMAINS = {'lesbonneschoses.io'}

USERS_TABLE = 'users'
_engine = None


class DatasabSyncError(Exception):
    """User-displayable sync failure."""


def _cfg():
    return current_app.config.get('database-datasab') or {}


def is_configured():
    cfg = _cfg()
    return bool(cfg.get('host') and cfg.get('user') and cfg.get('name'))


def engine():
    """Lazily built, cached read-only connection to DataSab's database. Mirrors
    the main OVH connection: pooled, pre-pinged, TLS without hostname checks."""
    global _engine
    if _engine is not None:
        return _engine
    cfg = _cfg()
    if not is_configured():
        raise DatasabSyncError("DataSab n'est pas configuré "
                               '(DATABASE_DATASAB__HOST / __USER / __PASSWORD '
                               '/ __NAME).')
    uri = (f"mysql+pymysql://{cfg['user']}:{cfg.get('password', '')}"
           f"@{cfg['host']}:{cfg.get('port', 3306)}/{cfg['name']}")
    options = {'pool_size': 2, 'max_overflow': 2, 'pool_recycle': 3600,
               'pool_pre_ping': True, 'pool_timeout': 20}
    if cfg.get('host') not in ('localhost', '127.0.0.1'):
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        options['connect_args'] = {'ssl': ctx}
    _engine = create_engine(uri, **options)
    return _engine


def _fold(s):
    return ''.join(c for c in unicodedata.normalize('NFD', s or '')
                   if not unicodedata.combining(c)).lower().strip()


def fetch_users():
    """[{email, first_name, last_name, department, active}] from DataSab.

    Only the columns DataSab actually has are selected, so a schema that drifts
    from this app's does not break the sync — it just carries less."""
    try:
        eng = engine()
        insp = sqla_inspect(eng)
        if USERS_TABLE not in insp.get_table_names():
            raise DatasabSyncError(
                f"La base DataSab n'a pas de table « {USERS_TABLE} ».")
        available = {c['name'] for c in insp.get_columns(USERS_TABLE)}
    except DatasabSyncError:
        raise
    except Exception as e:
        raise DatasabSyncError(f'Impossible de joindre la base DataSab : {e}')

    if 'email' not in available:
        raise DatasabSyncError("La table DataSab « users » n'a pas de colonne "
                               '« email ».')
    optional = [c for c in ('first_name', 'last_name', 'name', 'department',
                            'is_active', 'active') if c in available]
    columns = ', '.join(f'`{c}`' for c in ['email'] + optional)

    try:
        with eng.connect() as conn:
            rows = conn.execute(text(f'SELECT {columns} FROM `{USERS_TABLE}`')
                                ).mappings().all()
    except Exception as e:
        raise DatasabSyncError(f'Lecture de la base DataSab impossible : {e}')

    users = []
    for r in rows:
        first = r.get('first_name')
        last = r.get('last_name')
        if not (first or last) and r.get('name'):
            first, _, last = str(r['name']).strip().partition(' ')
        flag = r.get('is_active') if 'is_active' in r else r.get('active')
        users.append({
            'email': (r.get('email') or '').strip().lower(),
            'first_name': (first or None),
            'last_name': (last or None),
            'department': (r.get('department') or None),
            # No flag column at all means "everyone listed is active".
            'active': True if flag is None else bool(flag),
        })
    return users


def _department_slug(value, brand, cache):
    """Map a DataSab department onto an existing SOP department, by slug or by
    name. Unknown values are ignored rather than creating a department: the two
    apps do not necessarily share a taxonomy, and inventing departments here
    would pollute the SOP tree."""
    if not value:
        return None
    key = _fold(str(value))
    if key in cache:
        return cache[key]
    from help.models import SopDepartment
    slug = None
    for d in SopDepartment.query.filter_by(brand=brand).all():
        if _fold(d.slug) == key or _fold(d.name) == key:
            slug = d.slug
            break
    cache[key] = slug
    return slug


def _unique_username(base):
    from auth.models import User
    base = (base or 'user').split('@', 1)[0].replace(' ', '').lower() or 'user'
    username = base
    suffix = 1
    while User.query.filter_by(username=username).first():
        suffix += 1
        username = f'{base}{suffix}'
    return username


def _protected(user, actor_id):
    """True when the sync must leave this account active regardless of DataSab."""
    domain = (user.email or '').rsplit('@', 1)[-1].lower()
    return (domain in KEEP_DOMAINS or user.is_admin or user.id == actor_id)


def _revoke_ai_connections(user):
    """Revoke the user's MCP tokens (Administration › Connexions IA). An access
    token lives an hour and its refresh 90 days, so losing the login is not
    enough to cut an assistant off."""
    from datetime import datetime
    try:
        from mcp_server.models import OAuthToken
    except Exception:
        return
    (OAuthToken.query
     .filter(OAuthToken.user_id == user.id, OAuthToken.revoked_at.is_(None))
     .update({OAuthToken.revoked_at: datetime.utcnow()},
             synchronize_session=False))


def sync_users(brand, actor_id=None):
    """Make the SOP users match DataSab. Returns a stats dict."""
    from auth.models import User
    directory = fetch_users()

    dept_cache = {}
    # ``handled`` dedupes the directory (shared mailboxes appear twice);
    # ``active_emails`` is what grants access — an account flagged inactive in
    # DataSab must fall into the deactivation pass, not be shielded by it.
    handled, active_emails = set(), set()
    stats = {'created': 0, 'updated': 0, 'unchanged': 0, 'reactivated': 0,
             'deactivated': 0, 'protected': 0, 'no_email': 0, 'duplicates': 0}

    for entry in directory:
        email = entry['email']
        if not email or '@' not in email:
            stats['no_email'] += 1
            continue
        if email in handled:
            stats['duplicates'] += 1
            continue
        handled.add(email)
        if not entry['active']:
            continue  # left as-is here; the deactivation pass revokes access
        active_emails.add(email)

        dept_slug = _department_slug(entry['department'], brand, dept_cache)
        user = User.query.filter(db.func.lower(User.email) == email).first()
        if user is None:
            db.session.add(User(
                username=_unique_username(email), email=email,
                first_name=entry['first_name'], last_name=entry['last_name'],
                role='staff', department=dept_slug, is_active=True))
            stats['created'] += 1
            continue

        changed = False
        if not user.is_active:
            user.is_active = True
            stats['reactivated'] += 1
            changed = True
        if dept_slug and user.department != dept_slug:
            user.department = dept_slug
            changed = True
        if not user.first_name and entry['first_name']:
            user.first_name = entry['first_name']
            user.last_name = user.last_name or entry['last_name']
            changed = True
        if changed:
            stats['updated'] += 1
        else:
            stats['unchanged'] += 1

    # Anyone active here but no longer listed (or listed as inactive) in DataSab
    # loses access — except the protected groups, which keep it.
    for user in User.query.filter(User.is_active.is_(True)).all():
        if (user.email or '').strip().lower() in active_emails:
            continue
        if _protected(user, actor_id):
            stats['protected'] += 1
            continue
        user.is_active = False
        _revoke_ai_connections(user)
        stats['deactivated'] += 1

    db.session.commit()
    return stats
