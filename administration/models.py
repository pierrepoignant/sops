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


class GroupModule(db.Model):
    __tablename__ = 'group_modules'

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('auth_groups.id', ondelete='CASCADE'),
                         nullable=False, index=True)
    module_id = db.Column(db.String(50), nullable=False)

    __table_args__ = (db.UniqueConstraint('group_id', 'module_id', name='uq_group_module'),)

    group = db.relationship('Group', backref=db.backref('modules', cascade='all, delete-orphan'))
