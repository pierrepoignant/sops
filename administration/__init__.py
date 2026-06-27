from flask import Blueprint

administration_bp = Blueprint('administration', __name__, url_prefix='/administration',
                              template_folder='templates')

from administration import models, routes  # noqa: E402, F401
