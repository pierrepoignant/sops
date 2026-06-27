from init_db import db
from flask_login import UserMixin
from datetime import datetime


ROLES = ('admin', 'staff')


class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256))
    role = db.Column(db.String(20), nullable=False, default='staff')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)
    oauth_provider = db.Column(db.String(50))
    oauth_id = db.Column(db.String(200))
    first_name = db.Column(db.String(100))
    last_name = db.Column(db.String(100))

    @property
    def is_admin(self):
        return self.role == 'admin'

    @property
    def display_name(self):
        if self.first_name and self.last_name:
            return f'{self.first_name} {self.last_name}'
        if self.first_name:
            return self.first_name
        return self.username

    def __repr__(self):
        return f'<User {self.username} ({self.role})>'


class EmailLoginCode(db.Model):
    """One-time 6-digit code emailed for passwordless login. Used by accounts
    whose email isn't on the Google-OAuth domain (e.g. boutique staff with a
    personal address)."""
    __tablename__ = 'email_login_codes'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), nullable=False, index=True)
    code_hash = db.Column(db.String(128), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    consumed_at = db.Column(db.DateTime, nullable=True)
    attempts = db.Column(db.SmallInteger, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    ip_address = db.Column(db.String(45))


class UserVisit(db.Model):
    __tablename__ = 'user_visits'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    visited_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    endpoint = db.Column(db.String(200))
    ip_address = db.Column(db.String(45))

    user = db.relationship('User', backref='visits')
