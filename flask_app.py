"""Main Flask application."""
import json
import logging
from pathlib import Path

from flask import Flask, g
from flask_caching import Cache
import flask_gae_static
from lexrpc.server import Server
from lexrpc.flask_server import init_flask
from oauth_dropins.webutil import (
    appengine_info,
    appengine_config,
    flask_util,
    util,
)

logger = logging.getLogger(__name__)
logging.getLogger('lexrpc').setLevel(logging.INFO)
logging.getLogger('negotiator').setLevel(logging.WARNING)

app_dir = Path(__file__).parent


app = Flask(__name__, static_folder=None)
app.template_folder = './templates'
app.json.compact = False
app.config.from_pyfile(app_dir / 'config.py')
app.url_map.converters['regex'] = flask_util.RegexConverter
app.after_request(flask_util.default_modern_headers)
app.register_error_handler(Exception, flask_util.handle_exception)
if appengine_info.LOCAL:
    flask_gae_static.init_app(app)


@app.before_request
def init_globals():
    """Set request globals.

    * g.user: Current internal user we're operating on behalf of.
    """
    g.user = None


# don't redirect API requests with blank path elements
app.url_map.merge_slashes = False
app.url_map.redirect_defaults = False

app.wsgi_app = flask_util.ndb_context_middleware(
    app.wsgi_app, client=appengine_config.ndb_client,
    # disable in-memory cache
    # (also in tests/testutil.py)
    # https://github.com/googleapis/python-ndb/issues/888
    cache_policy=lambda key: False,
)

cache = Cache(app)

util.set_user_agent('Bridgy Fed (https://fed.brid.gy/)')

# XRPC server
lexicons = []
for filename in (app_dir / 'lexicons').glob('**/*.json'):
    with open(filename) as f:
        lexicons.append(json.load(f))

xrpc_server = Server(lexicons, validate=False)
init_flask(xrpc_server, app)
