from flask import Blueprint

media_bp = Blueprint('media', __name__, url_prefix='/media',
                     template_folder='templates')

from media import models, routes  # noqa: E402, F401
