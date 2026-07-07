import re
import uuid
import unicodedata
from datetime import datetime

from flask import (render_template, request, jsonify, abort, Response,
                   redirect, url_for, flash)
from flask_login import login_required, current_user

from init_db import db
from media import media_bp, storage
from media.models import MediaAsset


def _slugify(value, fallback='file'):
    value = unicodedata.normalize('NFKD', value or '').encode('ascii', 'ignore').decode('ascii')
    value = re.sub(r'[^a-zA-Z0-9]+', '-', value).strip('-').lower()
    return value or fallback


def _unique_slug(base):
    base = _slugify(base)
    slug = base
    while MediaAsset.query.filter_by(slug=slug).first():
        slug = f'{base}-{uuid.uuid4().hex[:6]}'
    return slug


@media_bp.route('/')
@login_required
def index():
    assets = MediaAsset.query.order_by(MediaAsset.created_at.desc()).all()
    return render_template('media/index.html', assets=assets,
                           configured=storage.is_configured())


@media_bp.route('/api/list')
@login_required
def api_list():
    q = (request.args.get('q') or '').strip().lower()
    query = MediaAsset.query.order_by(MediaAsset.created_at.desc())
    if q:
        query = query.filter(db.func.lower(MediaAsset.filename).like(f'%{q}%'))
    assets = query.limit(200).all()
    return jsonify(assets=[
        {'id': a.id, 'slug': a.slug, 'filename': a.filename,
         'content_type': a.content_type, 'url': a.url,
         'is_image': a.is_image, 'is_video': a.is_video}
        for a in assets
    ])


@media_bp.route('/upload', methods=['POST'])
@login_required
def upload():
    if not storage.is_configured():
        return jsonify(error="Le stockage S3 n'est pas configuré."), 503
    files = request.files.getlist('file') or request.files.getlist('files')
    if not files:
        return jsonify(error='Aucun fichier.'), 400

    saved = []
    for f in files:
        if not f or not f.filename:
            continue
        filename = f.filename
        data = f.read()
        content_type = f.mimetype or 'application/octet-stream'
        slug = _unique_slug(filename.rsplit('.', 1)[0])
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'bin'
        key = f'{storage.MEDIA_PREFIX}{slug}.{ext}'
        try:
            storage.put_object(key, data, content_type)
        except Exception as e:
            return jsonify(error=f'Échec de l\'envoi: {e}'), 502
        asset = MediaAsset(
            slug=slug, filename=filename, content_type=content_type,
            s3_key=key, size=len(data), uploaded_by_id=current_user.id,
        )
        db.session.add(asset)
        db.session.flush()
        saved.append({'id': asset.id, 'slug': asset.slug, 'filename': asset.filename,
                      'url': asset.url, 'content_type': asset.content_type,
                      'is_image': asset.is_image, 'is_video': asset.is_video})
    db.session.commit()
    return jsonify(ok=True, assets=saved), 201


@media_bp.route('/file/<slug>')
@login_required
def serve_file(slug):
    """Proxy the object from S3. Open to any authenticated user so that media
    embedded in help articles renders for everyone."""
    asset = MediaAsset.query.filter_by(slug=slug).first()
    if not asset:
        abort(404)
    if not storage.is_configured():
        abort(503)
    try:
        data, content_type = storage.get_object_bytes(asset.s3_key)
    except Exception:
        abort(404)
    resp = Response(data, mimetype=content_type or asset.content_type)
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


@media_bp.route('/<int:asset_id>/delete', methods=['POST'])
@login_required
def delete(asset_id):
    asset = db.session.get(MediaAsset, asset_id)
    if not asset:
        return jsonify(error='Introuvable'), 404
    storage.delete_object(asset.s3_key)
    db.session.delete(asset)
    db.session.commit()
    if request.headers.get('Accept', '').startswith('application/json') or request.is_json:
        return jsonify(ok=True)
    flash('Média supprimé.', 'success')
    return redirect(url_for('media.index'))
