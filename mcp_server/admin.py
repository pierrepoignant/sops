"""Administration › Connexions IA: the assistants people have authorised,
what they read, and the button to revoke one. Same screen as DataSab's."""
from datetime import datetime

from flask import flash, redirect, render_template, url_for
from flask_login import login_required
from sqlalchemy import func

from administration import administration_bp
from administration.routes import admin_required
from init_db import db
from .models import McpCall, OAuthToken


@administration_bp.route('/mcp-connexions')
@login_required
@admin_required
def mcp_connections():
    tokens = OAuthToken.query.order_by(OAuthToken.revoked_at.is_(None).desc(), OAuthToken.created_at.desc()).all()
    counts = dict(db.session.query(McpCall.token_id, func.count(McpCall.id))
                  .group_by(McpCall.token_id).all())
    recent = McpCall.query.order_by(McpCall.created_at.desc()).limit(50).all()
    return render_template('administration/mcp_connections.html', tokens=tokens, counts=counts, recent=recent)


@administration_bp.route('/mcp-connexions/<int:token_id>/revoke', methods=['POST'])
@login_required
@admin_required
def mcp_connection_revoke(token_id):
    t = db.session.get(OAuthToken, token_id)
    if not t:
        flash('Connexion introuvable.', 'error')
    elif not t.revoked_at:
        t.revoked_at = datetime.utcnow()
        db.session.commit()
        flash(f'Connexion de {t.user.display_name if t.user else "?"} '
              f'({t.client.client_name if t.client else "?"}) révoquée.', 'success')
    return redirect(url_for('administration.mcp_connections'))
