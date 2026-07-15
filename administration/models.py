from init_db import db


user_group_members = db.Table(
    'user_group_members',
    db.Column('user_id', db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
    db.Column('group_id', db.Integer, db.ForeignKey('auth_groups.id', ondelete='CASCADE'), primary_key=True),
)


class Group(db.Model):
    __tablename__ = 'auth_groups'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    description = db.Column(db.String(300))

    members = db.relationship(
        'User',
        secondary=user_group_members,
        backref=db.backref('groups', lazy='joined'),
    )


class AppSetting(db.Model):
    """Brand-scoped key/value configuration, edited on the admin Configuration
    screen. Known keys:

        sop_approver_mode     'owner' (department owner, default) | 'user'
        sop_approver_user_id  users.id as a string, when mode is 'user'
    """
    __tablename__ = 'app_settings'

    id = db.Column(db.Integer, primary_key=True)
    brand = db.Column(db.String(40), nullable=False, index=True)
    key = db.Column(db.String(80), nullable=False)
    value = db.Column(db.String(300), nullable=True)

    __table_args__ = (db.UniqueConstraint('brand', 'key', name='uq_setting_brand_key'),)

    @staticmethod
    def get(brand, key, default=None):
        row = AppSetting.query.filter_by(brand=brand, key=key).first()
        return row.value if row and row.value is not None else default

    @staticmethod
    def set(brand, key, value):
        row = AppSetting.query.filter_by(brand=brand, key=key).first()
        if not row:
            row = AppSetting(brand=brand, key=key)
            db.session.add(row)
        row.value = value


class GroupModule(db.Model):
    __tablename__ = 'group_modules'

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('auth_groups.id', ondelete='CASCADE'),
                         nullable=False, index=True)
    module_id = db.Column(db.String(50), nullable=False)

    __table_args__ = (db.UniqueConstraint('group_id', 'module_id', name='uq_group_module'),)

    group = db.relationship('Group', backref=db.backref('modules', cascade='all, delete-orphan'))
