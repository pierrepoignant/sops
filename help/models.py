from datetime import datetime
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from init_db import db


# Large-text columns: MEDIUMTEXT on MySQL (up to 16 MB, room for tables +
# inline content), plain TEXT elsewhere (e.g. SQLite in local dev).
_BODY = db.Text().with_variant(MEDIUMTEXT(), 'mysql')


# Per-department contributors: being listed grants create/edit rights on the
# department's SOPs (one user can contribute to several departments). Replaces
# the former global 'contributor' role + users.department allocation.
sop_department_contributors = db.Table(
    'sop_department_contributors',
    db.Column('department_id', db.Integer,
              db.ForeignKey('sop_departments.id'), primary_key=True),
    db.Column('user_id', db.Integer, db.ForeignKey('users.id'),
              primary_key=True),
)


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
    # Department owner: with admins, the only one who can manage the
    # department's training quiz. Column added post-launch — see
    # _upgrade_schema().
    owner_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    owner = db.relationship('User')
    contributors = db.relationship('User', secondary=sop_department_contributors,
                                   order_by='User.first_name', lazy='selectin')

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
    # Review cycle: who owns the SOP, when it was last verified, and when the
    # next review is due. Columns added post-launch — see _upgrade_schema().
    owner_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    review_due = db.Column(db.Date, nullable=True)
    last_reviewed_at = db.Column(db.DateTime, nullable=True)
    last_reviewed_by_id = db.Column(db.Integer, db.ForeignKey('users.id'),
                                    nullable=True)

    owner = db.relationship('User', foreign_keys=[owner_id])
    last_reviewed_by = db.relationship('User', foreign_keys=[last_reviewed_by_id])

    @property
    def review_overdue(self):
        return bool(self.review_due and self.review_due < datetime.utcnow().date())


class FileAttachmentMixin:
    """Display helpers shared by the SOP- and department-level attachments."""

    @property
    def size_human(self):
        s = float(self.size or 0)
        for unit in ('o', 'Ko', 'Mo', 'Go'):
            if s < 1024:
                return f'{s:.0f} {unit}'
            s /= 1024
        return f'{s:.0f} To'

    @property
    def icon(self):
        ct = (self.content_type or '').lower()
        name = (self.filename or '').lower()
        if ct.startswith('image/'):
            return 'fa-file-image'
        if 'pdf' in ct:
            return 'fa-file-pdf'
        if 'sheet' in ct or 'excel' in ct or name.endswith(('.xls', '.xlsx', '.csv')):
            return 'fa-file-excel'
        if 'word' in ct or name.endswith(('.doc', '.docx')):
            return 'fa-file-word'
        if ct.startswith('video/'):
            return 'fa-file-video'
        return 'fa-file-alt'


class SopAttachment(FileAttachmentMixin, db.Model):
    """A file attached to one SOP. Bytes live in S3 (same bucket as the media
    library, under sops/attachments/); this row is metadata + the S3 key.
    ``folder`` optionally groups files under a named folder (flat, one level)."""
    __tablename__ = 'sop_attachments'

    id = db.Column(db.Integer, primary_key=True)
    article_id = db.Column(db.Integer, db.ForeignKey('help_articles.id'),
                           nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False)
    content_type = db.Column(db.String(120), nullable=False,
                             default='application/octet-stream')
    s3_key = db.Column(db.String(512), nullable=False)
    size = db.Column(db.Integer, default=0)
    folder = db.Column(db.String(160), nullable=True)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    article = db.relationship(
        'HelpArticle',
        backref=db.backref('attachments', cascade='all, delete-orphan',
                           lazy='selectin', order_by='SopAttachment.created_at'))
    uploaded_by = db.relationship('User')


class SopDeptAttachment(FileAttachmentMixin, db.Model):
    """A file attached to a whole department — documents that don't belong to
    one SOP (plans, forms, posters…). Same shape as SopAttachment, with an
    optional flat ``folder`` for grouping."""
    __tablename__ = 'sop_dept_attachments'

    id = db.Column(db.Integer, primary_key=True)
    department_id = db.Column(db.Integer, db.ForeignKey('sop_departments.id'),
                              nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False)
    content_type = db.Column(db.String(120), nullable=False,
                             default='application/octet-stream')
    s3_key = db.Column(db.String(512), nullable=False)
    size = db.Column(db.Integer, default=0)
    folder = db.Column(db.String(160), nullable=True)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    department = db.relationship(
        'SopDepartment',
        backref=db.backref('attachments', cascade='all, delete-orphan',
                           lazy='selectin',
                           order_by='SopDeptAttachment.created_at'))
    uploaded_by = db.relationship('User')


class SopVersion(db.Model):
    """Immutable snapshot of a SOP taken at each save (v1 = state at creation,
    or the pre-edit state for articles older than versioning)."""
    __tablename__ = 'sop_versions'

    id = db.Column(db.Integer, primary_key=True)
    article_id = db.Column(db.Integer, db.ForeignKey('help_articles.id'),
                           nullable=False, index=True)
    version_no = db.Column(db.Integer, nullable=False)
    title = db.Column(db.String(255), nullable=False)
    category = db.Column(db.String(160), nullable=False, default='Général')
    body_html = db.Column(_BODY, nullable=False, default='')
    edited_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    # Verification: stamped by the department owner (or an admin) when they
    # check this exact version. Columns added post-launch — see
    # _upgrade_schema().
    verified_at = db.Column(db.DateTime, nullable=True)
    verified_by_id = db.Column(db.Integer, db.ForeignKey('users.id'),
                               nullable=True)

    article = db.relationship(
        'HelpArticle',
        backref=db.backref('versions', cascade='all, delete-orphan',
                           lazy='selectin', order_by='SopVersion.version_no'))
    edited_by = db.relationship('User', foreign_keys=[edited_by_id])
    verified_by = db.relationship('User', foreign_keys=[verified_by_id])

    __table_args__ = (
        db.UniqueConstraint('article_id', 'version_no', name='uq_version_art_no'),
    )


class SopPendingChange(db.Model):
    """A contributor's proposed change awaiting validation — only used when the
    brand's publish mode (AppSetting sop_publish_mode) is 'moderated'. The
    article itself is untouched ('update') or created unpublished ('create')
    until an approver applies the change from the validation queue."""
    __tablename__ = 'sop_pending_changes'

    id = db.Column(db.Integer, primary_key=True)
    article_id = db.Column(db.Integer, db.ForeignKey('help_articles.id'),
                           nullable=False, index=True)
    kind = db.Column(db.String(10), nullable=False, default='update')  # create | update
    title = db.Column(db.String(255), nullable=False)
    category = db.Column(db.String(160), nullable=False, default='Général')
    body_html = db.Column(_BODY, nullable=False, default='')
    status = db.Column(db.String(10), nullable=False, default='pending',
                       index=True)  # pending | approved | rejected
    submitted_by_id = db.Column(db.Integer, db.ForeignKey('users.id'),
                                nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey('users.id'),
                               nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    review_note = db.Column(db.String(300), nullable=True)

    article = db.relationship(
        'HelpArticle',
        backref=db.backref('pending_changes', cascade='all, delete-orphan',
                           lazy='selectin',
                           order_by='SopPendingChange.created_at'))
    submitted_by = db.relationship('User', foreign_keys=[submitted_by_id])
    reviewed_by = db.relationship('User', foreign_keys=[reviewed_by_id])


class SopRead(db.Model):
    """One 'lu et approuvé' acknowledgment: the user confirms having read the
    article at the given version. A new version requires a new ack."""
    __tablename__ = 'sop_reads'

    id = db.Column(db.Integer, primary_key=True)
    article_id = db.Column(db.Integer, db.ForeignKey('help_articles.id'),
                           nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False,
                        index=True)
    version_no = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    article = db.relationship(
        'HelpArticle',
        backref=db.backref('reads', cascade='all, delete-orphan', lazy='dynamic'))
    user = db.relationship('User')


class SopArticleView(db.Model):
    """One page view of a SOP by a user — feeds the admin stats."""
    __tablename__ = 'sop_article_views'

    id = db.Column(db.Integer, primary_key=True)
    article_id = db.Column(db.Integer, db.ForeignKey('help_articles.id'),
                           nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False,
                           index=True)

    article = db.relationship(
        'HelpArticle',
        backref=db.backref('views', cascade='all, delete-orphan', lazy='dynamic'))
    user = db.relationship('User')


class SopSearchLog(db.Model):
    """One search (deduped while the user is still typing) — feeds the admin
    stats, in particular the zero-result queries."""
    __tablename__ = 'sop_search_logs'

    id = db.Column(db.Integer, primary_key=True)
    brand = db.Column(db.String(40), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    # Named query_text: an attribute called `query` would shadow Model.query.
    query_text = db.Column(db.String(255), nullable=False)
    results_count = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False,
                           index=True)

    user = db.relationship('User')


class SopQuiz(db.Model):
    """One named quiz of a department. A department can have several; staff
    only see (and take) the active ones. The owner/admin prepares questions,
    then flips ``is_active``."""
    __tablename__ = 'sop_quizzes'

    id = db.Column(db.Integer, primary_key=True)
    brand = db.Column(db.String(40), nullable=False, index=True)
    department = db.Column(db.String(80), nullable=False, index=True)
    title = db.Column(db.String(160), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey('users.id'),
                              nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    created_by = db.relationship('User')

    @property
    def approved_questions(self):
        return [q for q in self.questions if q.status == 'approved']

    @property
    def proposed_questions(self):
        return [q for q in self.questions if q.status == 'proposed']


class SopQuizQuestion(db.Model):
    """One multiple-choice question of a quiz. AI proposes ('proposed'); the
    department owner validates ('approved') or discards ('rejected'). Only
    approved questions are served to staff. ``article_id`` points at the SOP
    the question was drawn from, so a wrong answer can link back to the
    procedure to (re)read."""
    __tablename__ = 'sop_quiz_questions'

    id = db.Column(db.Integer, primary_key=True)
    quiz_id = db.Column(db.Integer, db.ForeignKey('sop_quizzes.id'),
                        nullable=False, index=True)
    article_id = db.Column(db.Integer, db.ForeignKey('help_articles.id'),
                           nullable=True, index=True)
    question = db.Column(db.Text, nullable=False)
    options_json = db.Column(db.Text, nullable=False)  # JSON list of choices
    correct_index = db.Column(db.Integer, nullable=False, default=0)
    explanation = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), nullable=False, default='proposed',
                       index=True)  # proposed | approved | rejected
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    quiz = db.relationship(
        'SopQuiz',
        backref=db.backref('questions', cascade='all, delete-orphan',
                           order_by='SopQuizQuestion.id'))
    article = db.relationship('HelpArticle')

    @property
    def options(self):
        import json
        try:
            return json.loads(self.options_json)
        except (ValueError, TypeError):
            return []


class SopQuizAttempt(db.Model):
    """One staff run through a quiz."""
    __tablename__ = 'sop_quiz_attempts'

    id = db.Column(db.Integer, primary_key=True)
    quiz_id = db.Column(db.Integer, db.ForeignKey('sop_quizzes.id'),
                        nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False,
                        index=True)
    score = db.Column(db.Integer, nullable=False, default=0)
    total = db.Column(db.Integer, nullable=False, default=0)
    answers_json = db.Column(db.Text, nullable=False, default='[]')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    quiz = db.relationship(
        'SopQuiz',
        backref=db.backref('attempts', cascade='all, delete-orphan',
                           lazy='dynamic'))
    user = db.relationship('User')


class SopPdfExport(db.Model):
    """One generated department/category PDF export. Heavy WeasyPrint runs
    happen in a background thread; the resulting file lives in S3 and past
    exports stay downloadable from the export page."""
    __tablename__ = 'sop_pdf_exports'

    id = db.Column(db.Integer, primary_key=True)
    brand = db.Column(db.String(40), nullable=False, index=True)
    department = db.Column(db.String(80), nullable=False, index=True)
    category = db.Column(db.String(160), nullable=True)  # None = whole dept
    doc_title = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='running')
    error = db.Column(db.Text, nullable=True)
    s3_key = db.Column(db.String(255), nullable=True)
    filename = db.Column(db.String(255), nullable=False)
    size_bytes = db.Column(db.Integer, nullable=False, default=0)
    n_articles = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey('users.id'),
                              nullable=True)

    created_by = db.relationship('User')

    @property
    def size_human(self):
        size = self.size_bytes or 0
        for unit in ('o', 'Ko', 'Mo', 'Go'):
            if size < 1024 or unit == 'Go':
                return f'{size:.0f} {unit}' if unit == 'o' else f'{size:.1f} {unit}'
            size /= 1024

    @property
    def is_stale(self):
        """A run that never finished (e.g. the pod died mid-generation)."""
        from datetime import timedelta
        return (self.status == 'running'
                and datetime.utcnow() - self.created_at > timedelta(minutes=15))
