from flask import Blueprint

help_bp = Blueprint('help', __name__, url_prefix='/help',
                    template_folder='templates')

from help import models, routes  # noqa: E402, F401
