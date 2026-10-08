"""OAuth clients / codes / tokens issued to MCP clients, and the call log.

Tokens are opaque random strings; only their SHA-256 is stored. A token
belongs to one user **and one brand**: a host shows one brand only, so the
brand is the one of the host the consent was given on, and `/mcp` on another
brand's host refuses it. Same tables, same names as DataSab and the PIM.
"""
import hashlib
import json
import secrets
from datetime import datetime, timedelta

from init_db import db

ACCESS_TOKEN_TTL = timedelta(hours=1)
REFRESH_TOKEN_TTL = timedelta(days=90)
CODE_TTL = timedelta(minutes=10)


def _hash(raw):
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _now():
    return datetime.utcnow()


class OAuthClient(db.Model):
    """A registered MCP client (claude.ai, Claude Desktop, Claude Code…),
    created through dynamic client registration. Public: PKCE, no secret."""
    __tablename__ = 'oauth_clients'

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.String(64), unique=True, nullable=False, index=True)
    client_name = db.Column(db.String(200))
    client_uri = db.Column(db.String(500))
    redirect_uris_json = db.Column(db.Text, nullable=False, default='[]')
    created_at = db.Column(db.DateTime, default=_now, nullable=False)

    @property
    def redirect_uris(self):
        try:
            return json.loads(self.redirect_uris_json or '[]')
        except ValueError:
            return []

    def allows_redirect(self, uri):
        return uri in self.redirect_uris

    @classmethod
    def register(cls, name, redirect_uris, client_uri=None):
        c = cls(client_id='mcp_' + secrets.token_urlsafe(24), client_name=(name or '')[:200],
                client_uri=(client_uri or None), redirect_uris_json=json.dumps(list(redirect_uris)))
        db.session.add(c)
        db.session.commit()
        return c


class OAuthCode(db.Model):
    """Authorization code: single use, ten minutes, bound to the PKCE
    challenge, the redirect URI and the brand it was issued for."""
    __tablename__ = 'oauth_codes'

    id = db.Column(db.Integer, primary_key=True)
    code_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    client_id = db.Column(db.String(64), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    brand = db.Column(db.String(40), nullable=False)
    redirect_uri = db.Column(db.String(1000), nullable=False)
    code_challenge = db.Column(db.String(128), nullable=False)
    scope = db.Column(db.String(200))
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime)

    @classmethod
    def issue(cls, client_id, user_id, brand, redirect_uri, code_challenge, scope):
        raw = secrets.token_urlsafe(32)
        db.session.add(cls(code_hash=_hash(raw), client_id=client_id, user_id=user_id, brand=brand,
                           redirect_uri=redirect_uri, code_challenge=code_challenge,
                           scope=scope, expires_at=_now() + CODE_TTL))
        db.session.commit()
        return raw

    @classmethod
    def consume(cls, raw):
        """The code row if it is valid and unused (marks it used), else None."""
        row = cls.query.filter_by(code_hash=_hash(raw or '')).first()
        if not row or row.used_at or row.expires_at < _now():
            return None
        row.used_at = _now()
        db.session.commit()
        return row


class OAuthToken(db.Model):
    """One access/refresh token pair for one user, one brand, one client.
    Refreshing rotates both in place; revoking ends the pair."""
    __tablename__ = 'oauth_tokens'

    id = db.Column(db.Integer, primary_key=True)
    access_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    refresh_hash = db.Column(db.String(64), unique=True, index=True)
    client_id = db.Column(db.String(64), db.ForeignKey('oauth_clients.client_id', ondelete='CASCADE'),
                          nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    brand = db.Column(db.String(40), nullable=False, index=True)
    scope = db.Column(db.String(200))
    access_expires_at = db.Column(db.DateTime, nullable=False)
    refresh_expires_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=_now, nullable=False)
    last_used_at = db.Column(db.DateTime)
    revoked_at = db.Column(db.DateTime)

    client = db.relationship('OAuthClient', lazy='joined')
    user = db.relationship('User', lazy='joined')

    @property
    def active(self):
        return self.revoked_at is None

    @property
    def lifetime(self):
        return max(0, int((self.access_expires_at - _now()).total_seconds()))

    @staticmethod
    def _pair():
        return 'mcp_at_' + secrets.token_urlsafe(32), 'mcp_rt_' + secrets.token_urlsafe(32)

    @classmethod
    def issue(cls, client_id, user_id, brand, scope):
        """Returns (token_row, raw_access, raw_refresh)."""
        access, refresh = cls._pair()
        t = cls(access_hash=_hash(access), refresh_hash=_hash(refresh), client_id=client_id,
                user_id=user_id, brand=brand, scope=scope, access_expires_at=_now() + ACCESS_TOKEN_TTL,
                refresh_expires_at=_now() + REFRESH_TOKEN_TTL)
        db.session.add(t)
        db.session.commit()
        return t, access, refresh

    def rotate(self):
        """New access + refresh on the same row (the old refresh dies)."""
        access, refresh = self._pair()
        self.access_hash, self.refresh_hash = _hash(access), _hash(refresh)
        self.access_expires_at = _now() + ACCESS_TOKEN_TTL
        self.refresh_expires_at = _now() + REFRESH_TOKEN_TTL
        db.session.commit()
        return access, refresh

    @classmethod
    def by_access(cls, raw):
        """Live token for a bearer string, else None (unknown, expired, revoked)."""
        if not raw:
            return None
        t = cls.query.filter_by(access_hash=_hash(raw)).first()
        if not t or t.revoked_at or t.access_expires_at < _now():
            return None
        return t

    @classmethod
    def by_refresh(cls, raw):
        if not raw:
            return None
        t = cls.query.filter_by(refresh_hash=_hash(raw)).first()
        if not t or t.revoked_at or (t.refresh_expires_at and t.refresh_expires_at < _now()):
            return None
        return t

    @classmethod
    def by_any(cls, raw):
        """For revocation: match either half of the pair."""
        if not raw:
            return None
        h = _hash(raw)
        return cls.query.filter((cls.access_hash == h) | (cls.refresh_hash == h)).first()


class McpCall(db.Model):
    """Every tools/call: who, which tool, with what, how long, outcome."""
    __tablename__ = 'mcp_calls'

    id = db.Column(db.Integer, primary_key=True)
    token_id = db.Column(db.Integer, db.ForeignKey('oauth_tokens.id', ondelete='SET NULL'), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), index=True)
    brand = db.Column(db.String(40), nullable=False, index=True)
    tool = db.Column(db.String(80), nullable=False, index=True)
    arguments = db.Column(db.Text)
    ok = db.Column(db.Boolean, nullable=False, default=True)
    error = db.Column(db.String(300))
    duration_ms = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=_now, nullable=False, index=True)

    user = db.relationship('User', lazy='joined')
