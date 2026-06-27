from datetime import datetime
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from init_db import db


# Large-text columns: MEDIUMTEXT on MySQL (up to 16 MB, room for tables +
# inline content), plain TEXT elsewhere (e.g. SQLite in local dev).
_BODY = db.Text().with_variant(MEDIUMTEXT(), 'mysql')


class SopDepartment(db.Model):
    """Top level of the SOP hierarchy, per brand:
    brand -> department -> L1 category -> L2 category -> SOP.

    Each brand has its own departments (Boutique, Production, …). The first one
    is Boutique for Sablésienne. Within a department the SOPs are organized by
    the two-level HelpCategory tree."""
    __tablename__ = 'sop_departments'

    id = db.Column(db.Integer, primary_key=True)
    brand = db.Column(db.String(40), nullable=False, index=True)
    slug = db.Column(db.String(80), nullable=False, index=True)
    name = db.Column(db.String(160), nullable=False)
    icon = db.Column(db.String(40), nullable=False, default='fa-folder-open')
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    __table_args__ = (
        db.UniqueConstraint('brand', 'slug', name='uq_dept_brand_slug'),
    )


class HelpCategory(db.Model):
    """Managed category, up to two levels, scoped to a (brand, department). L1 =
    ``parent_id`` is NULL; L2 points at an L1 via ``parent_id``. Articles
    reference a category by its name string (unique within brand+department)."""
    __tablename__ = 'help_categories'

    id = db.Column(db.Integer, primary_key=True)
    brand = db.Column(db.String(40), nullable=False, index=True)
    department = db.Column(db.String(80), nullable=False, index=True)
    name = db.Column(db.String(160), nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    parent_id = db.Column(db.Integer, db.ForeignKey('help_categories.id'),
                          nullable=True, index=True)
    children = db.relationship(
        'HelpCategory',
        backref=db.backref('parent', remote_side=[id]),
        order_by='HelpCategory.sort_order',
        lazy='selectin')

    __table_args__ = (
        db.UniqueConstraint('brand', 'department', 'name',
                            name='uq_cat_brand_dept_name'),
    )

    @property
    def is_sub(self):
        return self.parent_id is not None


class HelpArticle(db.Model):
    """One SOP. Scoped to a (brand, department); placed in the category tree by
    its ``category`` name."""
    __tablename__ = 'help_articles'

    id = db.Column(db.Integer, primary_key=True)
    brand = db.Column(db.String(40), nullable=False, index=True)
    department = db.Column(db.String(80), nullable=False, index=True)
    slug = db.Column(db.String(160), unique=True, nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    category = db.Column(db.String(160), nullable=False, default='Général', index=True)
    body_html = db.Column(_BODY, nullable=False, default='')
    # Plain-text projection of body_html (+ title) used for searching.
    search_text = db.Column(_BODY, nullable=False, default='')
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    is_published = db.Column(db.Boolean, nullable=False, default=True)
    is_seed = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)
