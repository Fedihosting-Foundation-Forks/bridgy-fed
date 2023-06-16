"""Handles inbound webmentions."""
import datetime
import difflib
import logging
import re
import urllib.parse
from urllib.parse import urlencode, urljoin, urlparse

import feedparser
from flask import g, redirect, render_template, request
from flask.views import View
from google.cloud import ndb
from google.cloud.ndb import ComputedProperty, Key
from granary import as1, as2, microformats2
import mf2util
from oauth_dropins.webutil import flask_util, util
from oauth_dropins.webutil.appengine_config import tasks_client
from oauth_dropins.webutil.appengine_info import APP_ID
from oauth_dropins.webutil.flask_util import error, flash
from oauth_dropins.webutil.util import json_dumps, json_loads
from oauth_dropins.webutil import webmention
from requests import HTTPError, RequestException, URLRequired
from werkzeug.exceptions import BadGateway, BadRequest, HTTPException, NotFound

import activitypub
import common
from flask_app import app, cache
from models import Follower, Object, PROTOCOLS, Target, User
from protocol import Protocol

logger = logging.getLogger(__name__)

# https://cloud.google.com/appengine/docs/locations
TASKS_LOCATION = 'us-central1'

CHAR_AFTER_SPACE = chr(ord(' ') + 1)

# https://github.com/snarfed/bridgy-fed/issues/314
WWW_DOMAINS = frozenset((
    'www.jvt.me',
))
NON_TLDS = frozenset(('html', 'json', 'php', 'xml'))


class Web(User, Protocol):
    """Web user and webmention protocol implementation.

    The key name is the domain.
    """
    ABBREV = 'web'
    OTHER_LABELS = ('webmention',)

    has_redirects = ndb.BooleanProperty()
    redirects_error = ndb.TextProperty()
    has_hcard = ndb.BooleanProperty()

    @classmethod
    def _get_kind(cls):
        return 'MagicKey'

    @ComputedProperty
    def readable_id(self):
        # prettify if domain, noop if username
        username = self.username()
        if username != self.key.id():
            return util.domain_from_link(username, minimize=False)

    def put(self, *args, **kwargs):
        """Validate domain id, don't allow lower case or invalid characters."""
        id = self.key.id()
        assert re.match(common.DOMAIN_RE, id)
        assert id.lower() == id, f'lower case is not allowed in Web key id: {id}'
        return super().put(*args, **kwargs)

    @classmethod
    def get_or_create(cls, id, **kwargs):
        """Lower cases id (domain), then passes through to :meth:`User.get_or_create`."""
        return super().get_or_create(id.lower(), **kwargs)

    def web_url(self):
        """Returns this user's web URL aka web_url, eg 'https://foo.com/'."""
        return f'https://{self.key.id()}/'

    def ap_address(self):
        """Returns this user's ActivityPub address, eg '@foo.com@foo.com'.

        Uses the user's domain if they're direct, fed.brid.gy if they're not.
        """
        if self.direct:
            return f'@{self.username()}@{self.key.id()}'
        else:
            return f'@{self.key.id()}@{request.host}'

    def ap_actor(self, rest=None):
        """Returns this user's ActivityPub/AS2 actor id.

        Eg 'https://fed.brid.gy/foo.com'

        Web users are special cased to not have an /ap/web/ prefix, for backward
        compatibility.
        """
        url = common.host_url(self.key.id())
        if rest:
            url += f'/{rest}'
        return url

    def user_page_path(self, rest=None):
        """Always use domain."""
        path = f'/{self.ABBREV}/{self.key.id()}'

        if rest:
            if not rest.startswith('?'):
                path += '/'
            path += rest

        return path

    def username(self):
        """Returns the user's preferred username.

        Uses stored representative h-card if available, falls back to id.

        Returns: str
        """
        id = self.key.id()

        if self.obj and self.obj.as1 and self.direct:
            for url in (util.get_list(self.obj.as1, 'url') +
                        util.get_list(self.obj.as1, 'urls')):
                url = url.get('value') if isinstance(url, dict) else url
                if url and url.startswith('acct:'):
                    try:
                        urluser, urldomain = util.parse_acct_uri(url)
                    except ValueError:
                        continue
                    if urldomain == id:
                        logger.info(f'Found custom username: {urluser}')
                        return urluser

        logger.info(f'Defaulting username to key id {id}')
        return id

    def verify(self):
        """Fetches site a couple ways to check for redirects and h-card.


        Returns: :class:`Web` that was verified. May be different than
          self! eg if self's domain started with www and we switch to the root
          domain.
        """
        domain = self.key.id()
        logger.info(f'Verifying {domain}')

        if domain.startswith('www.') and domain not in WWW_DOMAINS:
            # if root domain redirects to www, use root domain instead
            # https://github.com/snarfed/bridgy-fed/issues/314
            root = domain.removeprefix("www.")
            root_site = f'https://{root}/'
            try:
                resp = util.requests_get(root_site, gateway=False)
                if resp.ok and self.is_web_url(resp.url):
                    logger.info(f'{root_site} redirects to {resp.url} ; using {root} instead')
                    root_user = Web.get_or_create(root)
                    self.use_instead = root_user.key
                    self.put()
                    return root_user.verify()
            except RequestException:
                pass

        # check webfinger redirect
        path = f'/.well-known/webfinger?resource=acct:{domain}@{domain}'
        self.has_redirects = False
        self.redirects_error = None
        try:
            url = urljoin(self.web_url(), path)
            resp = util.requests_get(url, gateway=False)
            domain_urls = ([f'https://{domain}/' for domain in common.DOMAINS] +
                           [common.host_url()])
            expected = [urljoin(url, path) for url in domain_urls]
            if resp.ok and resp.url:
                got = urllib.parse.unquote(resp.url)
                if got in expected:
                    self.has_redirects = True
                elif got:
                    diff = '\n'.join(difflib.Differ().compare([got], [expected[0]]))
                    self.redirects_error = f'Current vs expected:<pre>{diff}</pre>'
            else:
                lines = [url, f'  returned HTTP {resp.status_code}']
                if resp.url and resp.url != url:
                    lines[1:1] = ['  redirected to:', resp.url]
                self.redirects_error = '<pre>' + '\n'.join(lines) + '</pre>'
        except RequestException:
            pass

        # check home page
        try:
            self.obj = Web.load(self.web_url(), gateway=True)
            self.has_hcard = True
        except (BadRequest, NotFound, common.NoMicroformats):
            self.obj = None
            self.has_hcard = False

        self.put()
        return self

    @classmethod
    def key_for(cls, id):
        """Returns the :class:`ndb.Key` for a given id.

        If id is a domain, uses it as is. If it's a home page URL or fed.brid.gy
        or web.brid.gy AP actor URL, extracts the domain and uses that.
        Otherwise, raises AssertionError.

        Args:
          id: str

        Raises:
          ValueError
        """
        if not id:
            raise ValueError()

        if util.is_web(id):
            parsed = urlparse(id)
            if parsed.path in ('', '/'):
                id = parsed.netloc

        if re.match(common.DOMAIN_RE, id):
            tld = id.split('.')[-1]
            if tld in NON_TLDS:
                raise ValueError(f"{id} looks like a domain but {tld} isn't a TLD")
            return cls(id=id).key

        raise ValueError(f'{id} is not a domain or usable home page URL')

    @classmethod
    def owns_id(cls, id):
        """Returns None if id is a domain or http(s) URL, False otherwise.

        All web pages are http(s) URLs, but not all http(s) URLs are web pages.
        """
        if not id:
            return False

        try:
            key = cls.key_for(id)
            if key:
                user = key.get()
                return True if user and user.has_redirects else None
        except ValueError as e:
            logger.info(e)

        return None if util.is_web(id) else False

    @classmethod
    def send(cls, obj, url):
        """Sends a webmention to a given target URL.

        See :meth:`Protocol.send` for details.

        *Does not* propagate HTTP errors, DNS or connection failures, or other
        exceptions, since webmention support is optional for web recipients.
        https://fed.brid.gy/docs#error-handling
        """
        source_url = obj.proxy_url()
        logger.info(f'Sending webmention from {source_url} to {url}')

        endpoint = common.webmention_discover(url).endpoint
        try:
            if endpoint:
                webmention.send(endpoint, source_url, url)
                return True
        except RequestException as e:
            # log exception, then ignore it
            util.interpret_http_exception(e)
            return False

    @classmethod
    def fetch(cls, obj, gateway=False, check_backlink=False, **kwargs):
        """Fetches a URL over HTTP and extracts its microformats2.

        Follows redirects, but doesn't change the original URL in obj's id! The
        :class:`Model` class doesn't allow that anyway, but more importantly, we
        want to preserve that original URL becase other objects may refer to it
        instead of the final redirect destination URL.

        See :meth:`Protocol.fetch` for other background.

        Args:
          gateway: passed through to :func:`webutil.util.fetch_mf2`
          check_backlink: bool, optional, whether to require a link to Bridgy
            Fed. Ignored if the URL is a homepage, ie has no path.
          kwargs: ignored
        """
        url = obj.key.id()
        is_homepage = urlparse(url).path.strip('/') == ''

        require_backlink = (common.host_url().rstrip('/')
                            if check_backlink and not is_homepage
                            else None)

        try:
            parsed = util.fetch_mf2(url, gateway=gateway,
                                    require_backlink=require_backlink)
        except (ValueError, URLRequired) as e:
            error(str(e))

        if parsed is None:
            error(f'id {urlparse(url).fragment} not found in {url}')

        # find mf2 item
        if is_homepage:
            logger.info(f"{url} is user's web url")
            entry = mf2util.representative_hcard(parsed, parsed['url'])
            logger.info(f'Representative h-card: {json_dumps(entry, indent=2)}')
            if not entry:
                msg = f"Couldn't find a representative h-card (http://microformats.org/wiki/representative-hcard-parsing) on {parsed['url']}"
                logging.info(msg)
                raise common.NoMicroformats(msg)
        else:
            entry = mf2util.find_first_entry(parsed, ['h-entry'])
            if not entry:
                error(f'No microformats2 found in {url}')

        # store final URL in mf2 object, and also default url property to it,
        # since that's the fallback for AS1/AS2 id
        if is_homepage:
            entry.setdefault('rel-urls', {}).update(parsed.get('rel-urls', {}))
            entry.setdefault('type', ['h-card'])
        props = entry.setdefault('properties', {})
        if parsed['url']:
            entry['url'] = parsed['url']
            props.setdefault('url', [parsed['url']])
        logger.info(f'Extracted microformats2 entry: {json_dumps(entry, indent=2)}')

        # run full authorship algorithm if necessary: https://indieweb.org/authorship
        # duplicated in microformats2.json_to_object
        author = util.get_first(props, 'author')
        if not isinstance(author, dict) and not is_homepage:
            logger.info(f'Fetching full authorship for author {author}')
            author = mf2util.find_author({'items': [entry]}, hentry=entry,
                                         fetch_mf2_func=util.fetch_mf2)
            logger.info(f'Got: {author}')
            if author:
                props['author'] = util.trim_nulls([{
                    "type": ["h-card"],
                    'properties': {
                        field: [author[field]] if author.get(field) else []
                        for field in ('name', 'photo', 'url')
                    },
                }])

        obj.mf2 = entry
        return obj

    @classmethod
    def serve(cls, obj):
        """Serves an :class:`Object` as HTML."""
        obj_as1 = obj.as1

        from_proto = PROTOCOLS.get(obj.source_protocol)
        if from_proto:
            # fill in author/actor if available
            for field in 'author', 'actor':
                val = as1.get_object(obj.as1, field)
                if val.keys() == set(['id']) and val['id']:
                    loaded = from_proto.load(val['id'])
                    if loaded and loaded.as1:
                        obj_as1 = {**obj_as1, field: loaded.as1}
        else:
            logger.debug(f'Not hydrating actor or author due to source_protocol {obj.source_protocol}')

        html = microformats2.activities_to_html([obj_as1])

        # add HTML meta redirect to source page. should trigger for end users in
        # browsers but not for webmention receivers (hopefully).
        url = util.get_url(obj_as1)
        if url:
            utf8 = '<meta charset="utf-8">'
            refresh = f'<meta http-equiv="refresh" content="0;url={url}">'
            html = html.replace(utf8, utf8 + '\n' + refresh)

        return html, {'Content-Type': common.CONTENT_TYPE_HTML}


@app.get('/web-site')
@flask_util.cached(cache, datetime.timedelta(days=1))
def enter_web_site():
    return render_template('enter_web_site.html')


@app.post('/web-site')
def check_web_site():
    url = request.values['url']
    # this normalizes and lower cases domain
    domain = util.domain_from_link(url, minimize=False)
    if not domain:
        flash(f'No domain found in {url}')
        return render_template('enter_web_site.html')

    g.user = Web.get_or_create(domain, direct=True)
    try:
        g.user = g.user.verify()
    except BaseException as e:
        code, body = util.interpret_http_exception(e)
        if code:
            flash(f"Couldn't connect to {url}: {e}")
            return render_template('enter_web_site.html')
        raise

    g.user.put()
    return redirect(g.user.user_page_path())


@app.post('/webmention')
def webmention_external():
    """Handles inbound webmention, enqueue task to process.

    Use a task queue to deliver to followers because we send to each inbox in
    serial, which can take a long time with many followers/instances.
    """
    logger.info(f'Params: {list(request.form.items())}')

    source = flask_util.get_required_param('source').strip()
    if not util.is_web(source):
        error(f'Bad URL {source}')

    domain = util.domain_from_link(source, minimize=False)
    if not domain:
        error(f'Bad source URL {source}')

    g.user = Web.get_by_id(domain)
    if not g.user:
        error(f'No user found for domain {domain}')

    queue_path = tasks_client.queue_path(APP_ID, TASKS_LOCATION, 'webmention')
    task = tasks_client.create_task(
        parent=queue_path,
        task={
            'app_engine_http_request': {
                'http_method': 'POST',
                'relative_uri': '/_ah/queue/webmention',
                'body': urlencode(request.form).encode(),
                # https://googleapis.dev/python/cloudtasks/latest/gapic/v2/types.html#google.cloud.tasks_v2.types.AppEngineHttpRequest.headers
                'headers': {'Content-Type': 'application/x-www-form-urlencoded'},
            },
        },
    )
    msg = f'Enqueued task {task.name}.'
    logger.info(msg)
    return msg, 202


@app.post('/webmention-interactive')
def webmention_interactive():
    """Handler that runs interactive webmention-based requests from the web UI.

    ...eg the update profile button on user pages.
    """
    try:
        webmention_external()
        flash(f'Updating fediverse profile from <a href="{g.user.web_url()}">{g.user.key.id()}</a>...')
    except HTTPException as e:
        flash(util.linkify(str(e.description), pretty=True))

    path = g.user.user_page_path() if g.user else '/'
    return redirect(path, code=302)


@app.post('/_ah/queue/webmention')
def webmention_task():
    """Handles inbound webmention task."""
    logger.info(f'Params: {list(request.form.items())}')

    # load user
    source = flask_util.get_required_param('source').strip()
    domain = util.domain_from_link(source, minimize=False)
    logger.info(f'webmention from {domain}')

    g.user = Web.get_by_id(domain)
    if not g.user:
        error(f'No user found for domain {domain}', status=304)

    # fetch source page
    try:
        obj = Web.load(source, refresh=True, check_backlink=True)
    except BadRequest as e:
        error(str(e.description), status=304)
    except HTTPError as e:
        if e.response.status_code not in (410, 404):
            error(f'{e} ; {e.response.text if e.response else ""}', status=502)

        create_id = f'{source}#bridgy-fed-create'
        logger.info(f'Interpreting as Delete. Looking for {create_id}')
        create = Object.get_by_id(create_id)
        if not create or create.status != 'complete':
            error(f"Bridgy Fed hasn't successfully published {source}", status=304)

        id = f'{source}#bridgy-fed-delete'
        obj = Object(id=id, our_as1={
            'id': id,
            'objectType': 'activity',
            'verb': 'delete',
            'actor': g.user.ap_actor(),
            'object': source,
        })

    if not obj.mf2 and obj.type != 'delete':
        error(f'No microformats2 found in {source}', status=304)
    elif obj.mf2:
        # set actor to user
        props = obj.mf2['properties']
        author_urls = microformats2.get_string_urls(props.get('author', []))
        if author_urls and not g.user.is_web_url(author_urls[0]):
            logger.info(f'Overriding author {author_urls[0]} with {g.user.ap_actor()}')
            props['author'] = [g.user.ap_actor()]
        logger.info(f'Converted to AS1: {obj.type}: {json_dumps(obj.as1, indent=2)}')

    # if source is home page, update Web user and send an actor Update to
    # followers' instances
    if g.user.is_web_url(obj.key.id()):
        obj.put()
        g.user.obj = obj
        g.user.put()

        actor_as1 = {
            **obj.as1,
            'id': g.user.ap_actor(),
            'updated': util.now().isoformat(),
        }
        id = common.host_url(f'{obj.key.id()}#update-{util.now().isoformat()}')
        obj = Object(id=id, our_as1={
            'objectType': 'activity',
            'verb': 'update',
            'id': id,
            'actor': g.user.ap_actor(),
            'object': actor_as1,
        })

    inboxes_to_targets = _activitypub_targets(obj)

    obj.populate(
        users=[g.user.key],
        source_protocol='web',
    )
    if not inboxes_to_targets:
        obj.labels.append('user')
        obj.status = 'ignored'
        obj.put()
        return 'No targets'

    err = None
    last_success = None
    log_data = True

    if obj.type in ('note', 'article', 'comment'):
        # have we already seen this object? has it changed? or is it new?
        if obj.changed:
            logger.info(f'Content has changed from last time at {obj.updated}! Redelivering to all inboxes')
            updated = util.now().isoformat()
            id = f'{obj.key.id()}#bridgy-fed-update-{updated}'
            logger.info(f'Wrapping in update activity {id}')
            obj.put()
            update_as1 = {
                'objectType': 'activity',
                'verb': 'update',
                'id': id,
                'actor': g.user.ap_actor(),
                'object': {
                    # Mastodon requires the updated field for Updates, so
                    # add a default value.
                    # https://docs.joinmastodon.org/spec/activitypub/#supported-activities-for-statuses
                    # https://socialhub.activitypub.rocks/t/what-could-be-the-reason-that-my-update-activity-does-not-work/2893/4
                    # https://github.com/mastodon/documentation/pull/1150
                    'updated': updated,
                    **obj.as1,
                },
            }
            obj = Object(id=id, mf2=obj.mf2, our_as1=update_as1, labels=['user'],
                         users=[g.user.key], source_protocol='web')

        elif obj.new or 'force' in request.form:
            logger.info(f'New Object {obj.key.id()}')
            id = f'{obj.key.id()}#bridgy-fed-create'
            logger.info(f'Wrapping in post activity {id}')
            obj.put()
            create_as1 = {
                'objectType': 'activity',
                'verb': 'post',
                'id': id,
                'actor': g.user.ap_actor(),
                'object': obj.as1,
            }
            obj = Object(id=id, mf2=obj.mf2, our_as1=create_as1,
                         users=[g.user.key], labels=['user'],
                         source_protocol='web')

        else:
            msg = f'{obj.key.id()} is unchanged, nothing to do'
            logger.info(msg)
            return msg, 204

    # TODO: collect by inbox, add 'to' fields, de-dupe inboxes and recipients
    #
    # make copy of undelivered because we modify it below
    obj.populate(
        status='in progress',
        labels=['user'],
        delivered=[],
        failed=[],
        undelivered=[Target(uri=uri, protocol='activitypub')
                     for uri in inboxes_to_targets.keys()],
    )

    logger.info(f'Delivering to inboxes: {sorted(t.uri for t in obj.undelivered)}')
    for target in list(obj.undelivered):
        inbox = target.uri
        assert inbox
        if inbox in inboxes_to_targets:
            target_as2 = inboxes_to_targets[inbox]
        else:
            logger.warning(f'Missing target_as2 for inbox {inbox}!')
            target_as2 = None

        if obj.type == 'follow':
            # prefer AS2 id or url, if available
            # https://github.com/snarfed/bridgy-fed/issues/307
            dest = target_as2 or as2.from_as1(as1.get_object(obj.as1))
            dest_id = dest.get('id') or util.get_url(dest)
            if not dest_id:
                error('follow missing target')

            # TODO(#512): generalize across protocols
            to_obj = Object.get_or_insert(dest_id, as2=dest)
            to_ = activitypub.ActivityPub.get_or_create(id=dest_id, obj=to_obj)

            Follower.get_or_create(to=to_, from_=g.user, follow=obj.key)

        # this is reused later in ActivityPub.send()
        # TODO: find a better way
        obj.target_as2 = target_as2

        try:
            last = activitypub.ActivityPub.send(obj, inbox, log_data=log_data)
            obj.delivered.append(target)
            last_success = last
        except BaseException as e:
            code, body = util.interpret_http_exception(e)
            if not code and not body:
                raise
            obj.failed.append(target)
            err = e
        finally:
            log_data = False

        obj.undelivered.remove(target)
        obj.put()

    obj.status = ('complete' if obj.delivered
                  else 'failed' if obj.failed
                  else 'ignored')
    obj.put()

    # Pass the AP response status code and body through as our response
    if last_success:
        return last_success.text or 'Sent!', last_success.status_code
    elif isinstance(err, BadGateway):
        raise err
    elif isinstance(err, HTTPError):
        return str(err), err.status_code
    else:
        return str(err)


def _activitypub_targets(obj):
    """
    Args:
      obj: :class:`models.Object`

    Returns: dict of {str inbox URL: dict target AS2 object}
    """
    # if there's in-reply-to, like-of, or repost-of, they're the targets.
    # otherwise, it's all followers' inboxes.
    targets = util.get_urls(obj.as1, 'inReplyTo')
    verb = obj.as1.get('verb')
    if targets:
        logger.info(f'targets from inReplyTo: {targets}')
    elif verb in as1.VERBS_WITH_OBJECT:
        targets = util.get_urls(obj.as1, 'object')
        logger.info(f'targets from object: {targets}')

    targets = common.remove_blocklisted(targets)

    inboxes_to_targets = {}
    target_obj = None
    for target in targets:
        # fetch target page as AS2 object
        try:
            # TODO: make this generic across protocols
            target_stored = activitypub.ActivityPub.load(target)
            target_obj = target_stored.as2 or as2.from_as1(target_stored.as1)
        except (HTTPError, BadGateway) as e:
            resp = getattr(e, 'requests_response', None)
            if resp and resp.ok:
                type = common.content_type(resp)
                if type and type.startswith('text/html'):
                    continue  # give up
            raise

        inbox_url = target_obj.get('inbox')
        if not inbox_url:
            # TODO: test actor/attributedTo and not, with/without inbox
            actor = (util.get_first(target_obj, 'actor') or
                     util.get_first(target_obj, 'attributedTo'))
            if isinstance(actor, dict):
                inbox_url = actor.get('inbox')
                actor = util.get_first(actor, 'url') or actor.get('id')
            if not inbox_url and not actor:
                error('Target object has no actor or attributedTo with URL or id.', status=304)
            elif not isinstance(actor, str):
                error(f'Target actor or attributedTo has unexpected url or id object: {actor}', status=304)

        if not inbox_url:
            # fetch actor as AS object
            # TODO: make this generic across protocols
            actor_obj = activitypub.ActivityPub.load(actor)
            actor = actor_obj.as2 or as2.from_as1(actor_obj.as1)
            inbox_url = actor.get('inbox')

        if not inbox_url:
            # TODO: probably need a way to surface errors like this
            logger.error('Target actor has no inbox')
            continue

        inbox_url = urljoin(target, inbox_url)
        inboxes_to_targets[inbox_url] = target_obj

    if not targets or verb == 'share':
        logger.info('Delivering to followers')
        for follower in Follower.query(Follower.to == g.user.key,
                                       Follower.status == 'active'):
            recip = follower.from_.get()
            inbox = None
            # TODO(#512): generalize across protocols
            if recip and recip.obj and recip.obj.as2:
                inbox = (recip.obj.as2.get('endpoints', {}).get('sharedInbox') or
                         recip.obj.as2.get('publicInbox') or
                         recip.obj.as2.get('inbox'))
            if inbox:
                # HACK: use last target object from above for reposts, which
                # has its resolved id
                inboxes_to_targets[inbox] = (target_obj if verb == 'share' else None)
            else:
                # TODO: probably need a way to surface errors like this
                logger.error(f'Follower {follower.from_} has no entity or inbox')

    return inboxes_to_targets