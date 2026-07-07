from datetime import datetime
from init_db import db


class MediaAsset(db.Model):
    """One uploaded file. Bytes live in S3 (object storage); this row is just
    metadata + the S3 key. Served via GET /media/file/<slug>."""
    __tablename__ = 'media_assets'

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(80), unique=True, nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False)
    content_type = db.Column(db.String(120), nullable=False, default='application/octet-stream')
    s3_key = db.Column(db.String(512), nullable=False)
    size = db.Column(db.Integer, default=0)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    is_seed = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    uploaded_by = db.relationship('User')

    @property
    def url(self):
        return f'/media/file/{self.slug}'

    @property
    def is_image(self):
        return (self.content_type or '').startswith('image/')

    @property
    def is_video(self):
        return (self.content_type or '').startswith('video/')
