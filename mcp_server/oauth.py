"""OAuth 2.1 authorization server for MCP clients — the DataSab one, with the
brand of the host on the token.

What a client does, in order (all of it automatic on the client side):

1. ``GET /.well-known/oauth-protected-resource`` — learns that the
   authorization server is this same origin.
2. ``GET /.well-known/oauth-authorization-server`` — endpoints below.
3. ``POST /oauth/register`` — dynamic client registration (RFC 7591):
   gets a client_id for its redirect URI.
4. Sends the user's browser to ``/oauth/authorize`` with a PKCE challenge.
   Not logged in → the normal SOPs login (Google or e-mail code), then back
   here. The consent page names the client and the brand of this host; the
   user approves; the browser goes back to the client with a one-shot code.
5. ``POST /oauth/token`` — exchanges the code (plus the PKCE verifier) for
   an access token (1 h) and a refresh token (90 days, rotated on use).

A host shows one brand, so the token carries that brand and `/mcp` on another
brand's host refuses it. Any user who can log in on the host may connect: the
login already enforced the brand's e-mail domains.
"""
import base64
import hashlib
import secrets
from datetime import datetime
from urllib.parse import urlencode, urlparse

from flask import (abort, g, jsonify, redirect, render_template, request, session,
                   url_for)
from flask_login import current_user

from brands import brand_for_host, get_brand
from init_db import db
from . import mcp_bp
from .models import OAuthClient, OAuthCode, OAuthToken

SCOPES = ('sops:read',)
DEFAULT_SCOPE = 'sops:read'


def issuer():
    """https://sops.sablesienne.com — from the request (ProxyFix), so the
    devserver issues for its own host."""
    return request.url_root.rstrip('/')


def resource_url():
    return issuer() + '/mcp'


def host_brand():
    """The brand this host shows — the one every token issued here carries."""
    return getattr(g, 'brand', None) or brand_for_host(request.host)


def _no_store(resp):
    resp.headers['Cache-Control'] = 'no-store'
    resp.headers['Pragma'] = 'no-cache'
    return resp


def _cors(resp):
    """Browser-based MCP clients (the MCP Inspector, web IDEs) preflight the
    discovery and token endpoints; nothing here relies on cookies."""
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type, Mcp-Protocol-Version'
    return resp


def _error(code, description, status=400):
    return _cors(_no_store(jsonify(error=code, error_description=description))), status


# ---------------------------------------------------------------- discovery

@mcp_bp.route('/.well-known/oauth-protected-resource', methods=['GET', 'OPTIONS'])
@mcp_bp.route('/.well-known/oauth-protected-resource/mcp', methods=['GET', 'OPTIONS'])
def protected_resource_metadata():
    """RFC 9728: which authorization server protects /mcp."""
    if request.method == 'OPTIONS':
        return _cors(jsonify())
    brand = get_brand(host_brand()) or {}
    return _cors(jsonify({
        'resource': resource_url(),
        'authorization_servers': [issuer()],
        'scopes_supported': list(SCOPES),
        'bearer_methods_supported': ['header'],
        'resource_name': 'SOP · %s' % brand.get('name', ''),
        'resource_documentation': issuer() + '/mcp/docs',
    }))


@mcp_bp.route('/.well-known/oauth-authorization-server', methods=['GET', 'OPTIONS'])
def authorization_server_metadata():
    """RFC 8414."""
    if request.method == 'OPTIONS':
        return _cors(jsonify())
    base = issuer()
    return _cors(jsonify({
        'issuer': base,
        'authorization_endpoint': base + '/oauth/authorize',
        'token_endpoint': base + '/oauth/token',
        'registration_endpoint': base + '/oauth/register',
        'revocation_endpoint': base + '/oauth/revoke',
        'scopes_supported': list(SCOPES),
        'response_types_supported': ['code'],
        'response_modes_supported': ['query'],
        'grant_types_supported': ['authorization_code', 'refresh_token'],
        'token_endpoint_auth_methods_supported': ['none'],
        'revocation_endpoint_auth_methods_supported': ['none'],
        'code_challenge_methods_supported': ['S256'],
        'service_documentation': base + '/mcp/docs',
    }))


# ------------------------------------------------- dynamic client registration

def redirect_uri_ok(uri):
    """https anywhere, or plain http on the loopback (Claude Code, the
    Inspector and desktop apps listen on localhost for the callback)."""
    try:
        p = urlparse(uri)
    except ValueError:
        return False
    if p.scheme == 'https' and p.netloc:
        return True
    if p.scheme == 'http' and p.hostname in ('localhost', '127.0.0.1', '::1'):
        return True
    return False


@mcp_bp.route('/oauth/register', methods=['POST', 'OPTIONS'])
def register_client():
    """RFC 7591. Open — the spec has clients register unauthenticated — and
    harmless: a client_id only lets a client *ask* a user for access."""
    if request.method == 'OPTIONS':
        return _cors(jsonify())
    meta = request.get_json(silent=True)
    if not isinstance(meta, dict):
        return _error('invalid_client_metadata', 'A JSON object is required.')
    uris = meta.get('redirect_uris')
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) and redirect_uri_ok(u) for u in uris):
        return _error('invalid_redirect_uri',
                      'redirect_uris must be a non-empty list of https URIs (or http on localhost).')
    grant_types = meta.get('grant_types') or ['authorization_code']
    if any(gt not in ('authorization_code', 'refresh_token') for gt in grant_types):
        return _error('invalid_client_metadata', 'Only authorization_code and refresh_token are supported.')
    auth_method = meta.get('token_endpoint_auth_method') or 'none'
    if auth_method != 'none':
        return _error('invalid_client_metadata',
                      f'Unsupported token_endpoint_auth_method {auth_method!r}: clients are public (PKCE).')

    client = OAuthClient.register(name=meta.get('client_name') or urlparse(uris[0]).netloc,
                                  redirect_uris=uris, client_uri=meta.get('client_uri'))
    body = {
        'client_id': client.client_id,
        'client_id_issued_at': int(client.created_at.timestamp()),
        'client_name': client.client_name,
        'redirect_uris': client.redirect_uris,
        'grant_types': ['authorization_code', 'refresh_token'],
        'response_types': ['code'],
        'token_endpoint_auth_method': 'none',
        'scope': ' '.join(SCOPES),
    }
    if client.client_uri:
        body['client_uri'] = client.client_uri
    resp = _cors(_no_store(jsonify(body)))
    resp.status_code = 201
    return resp


# --------------------------------------------------------------- authorize

def _redirect_with(uri, **params):
    sep = '&' if urlparse(uri).query else '?'
    return redirect(uri + sep + urlencode({k: v for k, v in params.items() if v is not None}))


@mcp_bp.route('/oauth/authorize', methods=['GET'])
def authorize():
    a = request.args
    client = OAuthClient.query.filter_by(client_id=a.get('client_id', '')).first()
    redirect_uri = a.get('redirect_uri', '')
    # Never redirect to a URI the client did not register: answer in place.
    if not client or not redirect_uri or not client.allows_redirect(redirect_uri):
        return render_template('mcp/authorize_error.html',
                               message="Client inconnu ou adresse de retour non enregistrée."), 400
    state = a.get('state')
    if a.get('response_type') != 'code':
        return _redirect_with(redirect_uri, error='unsupported_response_type', state=state)
    challenge = a.get('code_challenge', '')
    if not challenge or a.get('code_challenge_method', 'plain') != 'S256':
        return _redirect_with(redirect_uri, error='invalid_request',
                              error_description='PKCE with S256 is required.', state=state)
    scope = a.get('scope') or DEFAULT_SCOPE
    if any(s not in SCOPES for s in scope.split()):
        return _redirect_with(redirect_uri, error='invalid_scope', state=state)

    if not current_user.is_authenticated:
        # Round trip through the normal login; auth.login stores `next`.
        return redirect(url_for('auth.login', next=request.full_path))

    brand = get_brand(host_brand())
    # Consent form; the nonce ties the POST to this browser session.
    nonce = secrets.token_urlsafe(16)
    session['mcp_consent_nonce'] = nonce
    return render_template('mcp/authorize.html', client=client, brand=brand,
                           redirect_host=urlparse(redirect_uri).netloc,
                           params={'client_id': client.client_id, 'redirect_uri': redirect_uri, 'state': state or '',
                                   'code_challenge': challenge, 'scope': scope},
                           nonce=nonce)


@mcp_bp.route('/oauth/authorize', methods=['POST'])
def authorize_decision():
    f = request.form
    if not current_user.is_authenticated:
        abort(403)
    if not f.get('nonce') or f.get('nonce') != session.pop('mcp_consent_nonce', None):
        abort(400)
    client = OAuthClient.query.filter_by(client_id=f.get('client_id', '')).first()
    redirect_uri = f.get('redirect_uri', '')
    if not client or not client.allows_redirect(redirect_uri):
        abort(400)
    state = f.get('state') or None
    if f.get('decision') != 'approve':
        return _redirect_with(redirect_uri, error='access_denied', state=state)
    code = OAuthCode.issue(client.client_id, current_user.id, host_brand(), redirect_uri,
                           f.get('code_challenge', ''), f.get('scope') or DEFAULT_SCOPE)
    return _redirect_with(redirect_uri, code=code, state=state)


# ------------------------------------------------------------------- token

def _client_from_request():
    """(client, error_response). Clients are public: client_id in the body,
    or as the HTTP Basic username with no password."""
    client_id = request.form.get('client_id')
    if request.authorization and request.authorization.type == 'basic':
        client_id = request.authorization.username or client_id
    client = OAuthClient.query.filter_by(client_id=client_id or '').first()
    if not client:
        return None, _error('invalid_client', 'Unknown client_id.', 401)
    return client, None


def pkce_ok(verifier, challenge):
    if not challenge or not verifier or not (43 <= len(verifier) <= 128):
        return False
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii') == challenge


def _token_response(row, access, refresh):
    return _cors(_no_store(jsonify({
        'access_token': access,
        'token_type': 'Bearer',
        'expires_in': row.lifetime,
        'refresh_token': refresh,
        'scope': row.scope or DEFAULT_SCOPE,
        'brand': row.brand,
    })))


@mcp_bp.route('/oauth/token', methods=['POST', 'OPTIONS'])
def token():
    if request.method == 'OPTIONS':
        return _cors(jsonify())
    client, err = _client_from_request()
    if err:
        return err
    grant = request.form.get('grant_type')

    if grant == 'authorization_code':
        code = OAuthCode.consume(request.form.get('code'))
        if not code or code.client_id != client.client_id:
            return _error('invalid_grant', 'Code invalide, expiré ou déjà utilisé.')
        if code.redirect_uri != (request.form.get('redirect_uri') or code.redirect_uri):
            return _error('invalid_grant', 'redirect_uri mismatch.')
        if not pkce_ok(request.form.get('code_verifier', ''), code.code_challenge):
            return _error('invalid_grant', 'PKCE verification failed.')
        row, access, refresh = OAuthToken.issue(client.client_id, code.user_id, code.brand, code.scope)
        return _token_response(row, access, refresh)

    if grant == 'refresh_token':
        row = OAuthToken.by_refresh(request.form.get('refresh_token'))
        if not row or row.client_id != client.client_id:
            return _error('invalid_grant', 'Refresh token invalide ou expiré.')
        access, refresh = row.rotate()
        return _token_response(row, access, refresh)

    return _error('unsupported_grant_type', 'Use authorization_code or refresh_token.')


@mcp_bp.route('/oauth/revoke', methods=['POST', 'OPTIONS'])
def revoke():
    """RFC 7009: the client gives back a token; 200 either way."""
    if request.method == 'OPTIONS':
        return _cors(jsonify())
    client, err = _client_from_request()
    if err:
        return err
    row = OAuthToken.by_any(request.form.get('token'))
    if row and row.client_id == client.client_id and not row.revoked_at:
        row.revoked_at = datetime.utcnow()
        db.session.commit()
    return _cors(_no_store(jsonify({})))
