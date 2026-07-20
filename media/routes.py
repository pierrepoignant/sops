import re
import uuid
import unicodedata
from datetime import datetime

from flask import (render_template, request, jsonify, abort, Response,
                   redirect, url_for, flash, g)
from flask_login import login_required, current_user

from init_db import db
from media import media_bp, storage
from media.models import MediaAsset


def _brand():
    """Active brand id for this request (set by the app before_request hook)."""
    return getattr(g, 'brand', None) or 'sablesienne'


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
    assets = (MediaAsset.query.filter_by(brand=_brand())
              .order_by(MediaAsset.created_at.desc()).all())
    return render_template('media/index.html', assets=assets,
                           configured=storage.is_configured())


@media_bp.route('/api/list')
@login_required
def api_list():
    q = (request.args.get('q') or '').strip().lower()
    query = (MediaAsset.query.filter_by(brand=_brand())
             .order_by(MediaAsset.created_at.desc()))
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
            brand=_brand(),
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


def _asset_etag(asset):
    stamp = asset.updated_at or asset.created_at
    return f'"{asset.id}-{asset.size}-{int(stamp.timestamp())}"'


@media_bp.route('/file/<slug>')
@login_required
def serve_file(slug):
    """Proxy the object from S3. Open to any authenticated user so that media
    embedded in help articles renders for everyone. ETag + no-cache instead of
    a long max-age so in-place edits (image editor) show up immediately while
    unchanged assets still answer 304 without hitting S3."""
    asset = MediaAsset.query.filter_by(slug=slug).first()
    if not asset:
        abort(404)
    if not storage.is_configured():
        abort(503)
    etag = _asset_etag(asset)
    if etag in (request.headers.get('If-None-Match') or ''):
        resp = Response(status=304)
    else:
        try:
            data, content_type = storage.get_object_bytes(asset.s3_key)
        except Exception:
            abort(404)
        resp = Response(data, mimetype=content_type or asset.content_type)
    resp.headers['ETag'] = etag
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@media_bp.route('/file/<slug>/replace', methods=['POST'])
@login_required
def replace_file(slug):
    """Overwrite an image's bytes in place (image editor save). The slug, URL
    and S3 key stay the same, so every SOP embedding the image shows the
    edited version."""
    asset = MediaAsset.query.filter_by(slug=slug).first()
    if not asset:
        abort(404)
    if not asset.is_image:
        return jsonify(error="Ce média n'est pas une image."), 400
    if not storage.is_configured():
        return jsonify(error="Le stockage S3 n'est pas configuré."), 503
    f = request.files.get('file')
    if not f:
        return jsonify(error='Aucun fichier.'), 400
    data = f.read()
    if not data or len(data) > 25 * 1024 * 1024:
        return jsonify(error='Image vide ou trop volumineuse (max 25 Mo).'), 400
    # The editor sends a typed canvas blob; anything untyped keeps the
    # asset's existing image content-type.
    content_type = f.mimetype if (f.mimetype or '').startswith('image/') \
        else asset.content_type
    try:
        storage.put_object(asset.s3_key, data, content_type)
    except Exception as e:
        return jsonify(error=f"Échec de l'envoi: {e}"), 502
    asset.content_type = content_type
    asset.size = len(data)
    asset.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify(ok=True, url=asset.url, size=asset.size)


@media_bp.route('/<int:asset_id>/delete', methods=['POST'])
@login_required
def delete(asset_id):
    asset = db.session.get(MediaAsset, asset_id)
    if not asset or asset.brand != _brand():
        return jsonify(error='Introuvable'), 404
    storage.delete_object(asset.s3_key)
    db.session.delete(asset)
    db.session.commit()
    if request.headers.get('Accept', '').startswith('application/json') or request.is_json:
        return jsonify(ok=True)
    flash('Média supprimé.', 'success')
    return redirect(url_for('media.index'))
