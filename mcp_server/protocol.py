"""MCP Streamable HTTP endpoint: ``POST /mcp`` carries JSON-RPC 2.0.

Stateless: no session id, no server-to-client stream (``GET /mcp`` is 405,
which the spec allows). Every request is authenticated with the bearer
token issued by ``oauth.py``; the token's user becomes ``current_user`` for
the duration of the request, and the token's brand must be the brand of the
host — a host shows one brand only.
"""
import json
import os
import time
from datetime import datetime

from flask import current_app, g, jsonify, render_template, request, Response
from flask_login import login_required

from init_db import db
from . import mcp_bp
from .models import McpCall, OAuthToken
from .oauth import host_brand, issuer
from . import tools as toolbox

SERVER_VERSION = '1.0'
PROTOCOL_VERSIONS = ('2025-06-18', '2025-03-26', '2024-11-05')


def instructions(token):
    from brands import get_brand
    brand = (get_brand(token.brand) or {}).get('name', token.brand)
    return (
        f"SOP {brand} : les procédures de la maison (le manuel), rangées par département, "
        "puis par catégorie. Pour trouver une procédure : search_sops avec quelques mots, "
        "puis get_sop avec son slug pour la lire en entier. list_departments et "
        "list_categories donnent le plan. Chaque procédure est celle publiée ; la version "
        "et la date de dernière vérification disent si elle est à jour. "
        "Rien ne se modifie par ce canal.")


def _rpc_error(id_, code, message, data=None):
    err = {'code': code, 'message': message}
    if data is not None:
        err['data'] = data
    return {'jsonrpc': '2.0', 'id': id_, 'error': err}


def _rpc_result(id_, result):
    return {'jsonrpc': '2.0', 'id': id_, 'result': result}


def _unauthorized(description):
    resp = jsonify(error='unauthorized', error_description=description)
    resp.status_code = 401
    resp.headers['WWW-Authenticate'] = (
        f'Bearer resource_metadata="{issuer()}/.well-known/oauth-protected-resource", '
        f'error="invalid_token", error_description="{description}"')
    return _cors(resp)


def _cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type, Mcp-Protocol-Version, Mcp-Session-Id'
    resp.headers['Access-Control-Expose-Headers'] = 'WWW-Authenticate, Mcp-Protocol-Version'
    resp.headers['Cache-Control'] = 'no-store'
    return resp


def _authenticate():
    """(token_row, None) from the Authorization header, else (None, reason)."""
    auth = request.headers.get('Authorization', '')
    if not auth.lower().startswith('bearer '):
        return None, 'A bearer token is required.'
    row = OAuthToken.by_access(auth[7:].strip())
    if not row:
        return None, 'Token invalide, expiré ou révoqué.'
    if not row.user:
        return None, 'Utilisateur introuvable.'
    if not row.user.is_active:
        # Deactivated by the DataSab sync: the token outlives the account
        # (90 days of refresh), so it has to be refused here too.
        return None, 'Compte désactivé.'
    return row, None


def dispatch(msg, token):
    """One JSON-RPC request → response dict, or None for a notification."""
    if not isinstance(msg, dict) or msg.get('jsonrpc') != '2.0' or not isinstance(msg.get('method'), str):
        return _rpc_error(msg.get('id') if isinstance(msg, dict) else None, -32600, 'Invalid Request')
    method, id_, params = msg['method'], msg.get('id'), msg.get('params') or {}

    if method == 'initialize':
        asked = params.get('protocolVersion')
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return _rpc_result(id_, {
            'protocolVersion': version,
            'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': {'name': 'sops', 'title': 'SOP · Les Bonnes Choses', 'version': SERVER_VERSION},
            'instructions': instructions(token),
        })
    if method.startswith('notifications/') or 'id' not in msg:
        return None
    if method == 'ping':
        return _rpc_result(id_, {})
    if method == 'tools/list':
        return _rpc_result(id_, {'tools': toolbox.catalogue()})
    if method == 'tools/call':
        name = params.get('name')
        args = params.get('arguments') or {}
        if not isinstance(args, dict):
            return _rpc_error(id_, -32602, 'arguments must be an object')
        return _rpc_result(id_, call_tool(token, name, args))
    if method == 'resources/list':
        return _rpc_result(id_, {'resources': []})
    if method == 'resources/templates/list':
        return _rpc_result(id_, {'resourceTemplates': []})
    if method == 'prompts/list':
        return _rpc_result(id_, {'prompts': []})
    return _rpc_error(id_, -32601, f'Method not found: {method}')


def call_tool(token, name, args):
    started = time.monotonic()
    log = McpCall(token_id=token.id, user_id=token.user_id, brand=token.brand, tool=(name or '')[:80],
                  arguments=json.dumps(args, ensure_ascii=False)[:4000])
    try:
        result = toolbox.call(token, name, args)
        text = json.dumps(result, ensure_ascii=False, default=str)
        out = {'content': [{'type': 'text', 'text': text}], 'isError': False}
        if isinstance(result, dict):
            out['structuredContent'] = result
    except toolbox.ToolError as e:
        log.ok, log.error = False, str(e)[:300]
        out = {'content': [{'type': 'text', 'text': f'Erreur : {e}'}], 'isError': True}
    except Exception as e:  # a bug in a tool must not kill the session
        db.session.rollback()
        current_app.logger.exception('[mcp] tool %s failed', name)
        log.ok, log.error = False, f'{type(e).__name__}: {e}'[:300]
        out = {'content': [{'type': 'text', 'text': f'Erreur interne dans {name} : {type(e).__name__}: {e}'}],
               'isError': True}
    log.duration_ms = int((time.monotonic() - started) * 1000)
    try:
        db.session.add(log)
        db.session.commit()
    except Exception:
        db.session.rollback()
    return out


@mcp_bp.route('/mcp', methods=['POST', 'GET', 'DELETE', 'OPTIONS'])
def endpoint():
    if request.method == 'OPTIONS':
        return _cors(jsonify())
    token, why = _authenticate()
    if not token:
        return _unauthorized(why)
    # A token opens one brand; this host shows one brand. They must agree —
    # a second lock after the token, visible from outside.
    if token.brand != host_brand():
        resp = jsonify(error='forbidden',
                       error_description=f"Ce jeton ouvre {token.brand} ; cet hôte ne sert que {host_brand()}.")
        resp.status_code = 403
        return _cors(resp)
    if request.method != 'POST':
        resp = jsonify(error='method_not_allowed',
                       error_description='This server is stateless: POST JSON-RPC to /mcp.')
        resp.status_code = 405
        return _cors(resp)

    body = request.get_json(silent=True, force=True)
    if body is None:
        resp = jsonify(_rpc_error(None, -32700, 'Parse error'))
        resp.status_code = 400
        return _cors(resp)

    # The token's user is the request's user: Flask-Login reads g._login_user
    # before it looks at the session cookie.
    g._login_user = token.user
    g.mcp_token = token
    token.last_used_at = datetime.utcnow()
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()

    messages = body if isinstance(body, list) else [body]
    responses = [r for r in (dispatch(m, token) for m in messages) if r is not None]
    if not responses:
        return _cors(Response(status=202))
    resp = jsonify(responses if isinstance(body, list) else responses[0])
    resp.headers['Mcp-Protocol-Version'] = PROTOCOL_VERSIONS[0]
    return _cors(resp)


@mcp_bp.route('/mcp/docs')
@login_required
def docs():
    """`MCP.md`, rendered — the address the discovery documents point to."""
    path = os.path.join(current_app.root_path, 'MCP.md')
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except OSError:
        text = '# MCP\n\nLa documentation ne fait pas partie de cette version.'
    html = None
    try:
        import markdown
        html = markdown.markdown(text, extensions=['tables', 'fenced_code'])
    except ImportError:
        pass
    return render_template('mcp/docs.html', text=text, html=html)
