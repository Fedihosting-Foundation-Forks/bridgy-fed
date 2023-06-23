"""Datastore model classes."""
from datetime import timedelta, timezone
import itertools
import json
import logging
import random
import urllib.parse

from arroba.mst import dag_cbor_cid
from Crypto.PublicKey import ECC, RSA
import dag_json
from flask import g, request
from google.cloud import ndb
from granary import as1, as2, bluesky, microformats2
from oauth_dropins.webutil import util
from oauth_dropins.webutil.appengine_info import DEBUG
from oauth_dropins.webutil.flask_util import error
from oauth_dropins.webutil.models import ComputedJsonProperty, JsonProperty, StringIdModel
from oauth_dropins.webutil.util import json_dumps, json_loads

import common
from common import base64_to_long, long_to_base64, redirect_unwrap

# maps string label to Protocol subclass. populated by ProtocolUserMeta.
# seed with old and upcoming protocols that don't have their own classes (yet).
PROTOCOLS = {'bluesky': None, 'ostatus': None}

# 2048 bits makes tests slow, so use 1024 for them
KEY_BITS = 1024 if DEBUG else 2048
PAGE_SIZE = 20

# auto delete old objects of these types via the Object.expire property
# https://cloud.google.com/datastore/docs/ttl
OBJECT_EXPIRE_TYPES = (
    'post',
    'update',
    'delete',
    'accept',
    'reject',
    'undo',
    None
)
OBJECT_EXPIRE_AGE = timedelta(days=90)

logger = logging.getLogger(__name__)


class ProtocolUserMeta(type(ndb.Model)):
    """:class:`User` metaclass. Registers all subclasses in the PROTOCOLS global."""
    def __new__(meta, name, bases, class_dict):
        cls = super().__new__(meta, name, bases, class_dict)
        if hasattr(cls, 'LABEL') and cls.LABEL not in ('protocol', 'user'):
            for label in (cls.LABEL, cls.ABBREV) + cls.OTHER_LABELS:
                if label:
                    PROTOCOLS[label] = cls
        return cls


def reset_protocol_properties():
    """Recreates various protocol properties to include choices PROTOCOLS."""
    Target.protocol = ndb.StringProperty(
        'protocol', choices=list(PROTOCOLS.keys()), required=True)
    Object.source_protocol = ndb.StringProperty(
        'source_protocol', choices=list(PROTOCOLS.keys()))


class User(StringIdModel, metaclass=ProtocolUserMeta):
    """Abstract base class for a Bridgy Fed user.

    Stores multiple keypairs needed for the supported protocols. Currently:

    * RSA keypair for ActivityPub HTTP Signatures
      properties: mod, public_exponent, private_exponent, all encoded as
        base64url (ie URL-safe base64) strings as described in RFC 4648 and
        section 5.1 of the Magic Signatures spec
      https://tools.ietf.org/html/draft-cavage-http-signatures-12

    * P-256 keypair for AT Protocol's signing key
      property: p256_key, PEM encoded
      https://atproto.com/guides/overview#account-portability
    """
    obj_key = ndb.KeyProperty(kind='Object')  # user profile
    mod = ndb.StringProperty()
    public_exponent = ndb.StringProperty()
    private_exponent = ndb.StringProperty()
    p256_key = ndb.StringProperty()
    use_instead = ndb.KeyProperty()

    # whether this user signed up or otherwise explicitly, deliberately
    # interacted with Bridgy Fed. For example, if fediverse user @a@b.com looks
    # up @foo.com@fed.brid.gy via WebFinger, we'll create Users for both,
    # @a@b.com will be direct, foo.com will not.
    direct = ndb.BooleanProperty(default=False)

    created = ndb.DateTimeProperty(auto_now_add=True)
    updated = ndb.DateTimeProperty(auto_now=True)

    # OLD. some stored entities still have this; do not reuse.
    # actor_as2 = JsonProperty()

    def __init__(self, **kwargs):
        """Constructor.

        Sets :attr:`obj` explicitly because however :class:`Model` sets it
        doesn't work with @property and @obj.setter below.
        """
        obj = kwargs.pop('obj', None)
        super().__init__(**kwargs)

        if obj:
            self.obj = obj

    @classmethod
    def new(cls, **kwargs):
        """Try to prevent instantiation. Use subclasses instead."""
        raise NotImplementedError()

    def _post_put_hook(self, future):
        logger.info(f'Wrote {self.key}')

    @classmethod
    def get_by_id(cls, id):
        """Override Model.get_by_id to follow the use_instead property."""
        user = cls._get_by_id(id)
        if user and user.use_instead:
            return user.use_instead.get()

        return user

    @classmethod
    @ndb.transactional()
    def get_or_create(cls, id, **kwargs):
        """Loads and returns a User. Creates it if necessary."""
        assert cls != User
        user = cls.get_by_id(id)
        if user:
            # override direct from False => True if set
            direct = kwargs.get('direct')
            if direct and not user.direct:
                logger.info(f'Setting {user.key} direct={direct}')
                user.direct = direct
                user.put()
            return user

        # generate keys for all protocols _except_ our own
        #
        # these can use urandom() and do nontrivial math, so they can take time
        # depending on the amount of randomness available and compute needed.
        if cls.LABEL != 'activitypub':
            # originally from django_salmon.magicsigs
            key = RSA.generate(KEY_BITS, randfunc=random.randbytes if DEBUG else None)
            kwargs.update({
                    'mod': long_to_base64(key.n),
                    'public_exponent': long_to_base64(key.e),
                    'private_exponent': long_to_base64(key.d),
            })

        if cls.LABEL != 'atprotocol':
            key = ECC.generate(
                curve='P-256', randfunc=random.randbytes if DEBUG else None)
            kwargs['p256_key'] = key.export_key(format='PEM')

        user = cls(id=id, **kwargs)
        try:
            user.put()
        except AssertionError as e:
            error(f'Bad {cls.__name__} id {id} : {e}')

        logger.info(f'Created new {user}')
        return user

    @property
    def obj(self):
        """Convenience accessor that loads :attr:`obj_key` from the datastore."""
        if self.obj_key:
            if not hasattr(self, '_obj'):
                self._obj = self.obj_key.get()
            return self._obj

    @obj.setter
    def obj(self, obj):
        if obj:
            assert isinstance(obj, Object)
            assert obj.key
            self._obj = obj
            self.obj_key = obj.key
        else:
            self._obj = self.obj_key = None

    @classmethod
    def load_multi(cls, users):
        """Loads :attr:`obj` for multiple users in parallel.

        Args:
          users: sequence of :class:`User`
        """
        objs = ndb.get_multi(u.obj_key for u in users if u.obj_key)
        keys_to_objs = {o.key: o for o in objs}

        for u in users:
            u._obj = keys_to_objs.get(u.obj_key)

    def as2(self):
        """Returns this user as an AS2 actor."""
        return self.obj.as_as2() if self.obj else {}

    @ndb.ComputedProperty
    def readable_id(self):
        """This user's human-readable unique id, eg '@me@snarfed.org'.

        To be implemented by subclasses.
        """
        return None

    def readable_or_key_id(self):
        """Returns readable_id if set, otherwise key id."""
        return self.readable_id or self.key.id()

    def href(self):
        return f'data:application/magic-public-key,RSA.{self.mod}.{self.public_exponent}'

    def public_pem(self):
        """Returns: bytes"""
        rsa = RSA.construct((base64_to_long(str(self.mod)),
                             base64_to_long(str(self.public_exponent))))
        return rsa.exportKey(format='PEM')

    def private_pem(self):
        """Returns: bytes"""
        rsa = RSA.construct((base64_to_long(str(self.mod)),
                             base64_to_long(str(self.public_exponent)),
                             base64_to_long(str(self.private_exponent))))
        return rsa.exportKey(format='PEM')

    def name(self):
        """Returns this user's human-readable name, eg 'Ryan Barrett'."""
        if self.obj and self.obj.as1:
            name = self.obj.as1.get('displayName')
            if name:
                return name

        return self.readable_or_key_id()

    def web_url(self):
        """Returns this user's web URL (homepage), eg 'https://foo.com/'.

        To be implemented by subclasses.

        Returns:
          str
        """
        raise NotImplementedError()

    def is_web_url(self, url):
        """Returns True if the given URL is this user's web URL (homepage).

        Args:
          url: str

        Returns:
          boolean
        """
        if not url:
            return False

        url = url.strip().rstrip('/')
        parsed_url = urllib.parse.urlparse(url)
        if parsed_url.scheme not in ('http', 'https', ''):
            return False

        this = self.web_url().rstrip('/')
        parsed_this = urllib.parse.urlparse(this)

        return (url == this or url == parsed_this.netloc or
                parsed_url[1:] == parsed_this[1:])  # ignore http vs https

    def ap_address(self):
        """Returns this user's ActivityPub address, eg '@me@foo.com'.

        To be implemented by subclasses.

        Returns:
          str
        """
        raise NotImplementedError()

    def ap_actor(self, rest=None):
        """Returns this user's ActivityPub/AS2 actor id.

        Eg 'https://fed.brid.gy/ap/bluesky/foo.com'

        To be implemented by subclasses.

        Args:
          rest: str, optional, appended to URL path

        Returns:
          str
        """
        raise NotImplementedError()

    def user_page_path(self, rest=None):
        """Returns the user's Bridgy Fed user page path."""
        path = f'/{self.ABBREV}/{self.readable_or_key_id()}'

        if rest:
            if not rest.startswith('?'):
                path += '/'
            path += rest

        return path

    def user_page_link(self):
        """Returns a pretty user page link with the user's name and profile picture."""
        actor = self.obj.as1 if self.obj and self.obj.as1 else {}
        img = util.get_url(actor, 'image') or ''
        return f'<a class="h-card u-author" href="{self.user_page_path()}"><img src="{img}" class="profile"> {self.name()}</a>'


class Target(ndb.Model):
    """Delivery destinations. ActivityPub inboxes, webmention targets, etc.

    Used in StructuredPropertys inside Object; not stored directly in the
    datastore.

    ndb implements this by hoisting each property here into a corresponding
    property on the parent entity, prefixed by the StructuredProperty name
    below, eg delivered.uri, delivered.protocol, etc.

    For repeated StructuredPropertys, the hoisted properties are all
    repeated on the parent entity, and reconstructed into
    StructuredPropertys based on their order.

    https://googleapis.dev/python/python-ndb/latest/model.html#google.cloud.ndb.model.StructuredProperty
    """
    uri = ndb.StringProperty(required=True)
    # choices is populated in flask_app, after all User subclasses are created,
    # so that PROTOCOLS is fully populated
    protocol = ndb.StringProperty(choices=[], required=True)


class Object(StringIdModel):
    """An activity or other object, eg actor.

    Key name is the id. We synthesize ids if necessary.
    """
    STATUSES = ('new', 'in progress', 'complete', 'failed', 'ignored')
    LABELS = ('activity', 'feed', 'notification', 'user')

    # Users this activity is to or from
    users = ndb.KeyProperty(repeated=True)
    # DEPRECATED but still used read only to maintain backward compatibility
    # with old Objects in the datastore that we haven't bothered migrating.
    domains = ndb.StringProperty(repeated=True)

    status = ndb.StringProperty(choices=STATUSES)
    # choices is populated in flask_app, after all User subclasses are created,
    # so that PROTOCOLS is fully populated
    # TODO: remove? is this redundant with the protocol-specific data fields below?
    source_protocol = ndb.StringProperty(choices=[])
    labels = ndb.StringProperty(repeated=True, choices=LABELS)

    # TODO: switch back to ndb.JsonProperty if/when they fix it for the web console
    # https://github.com/googleapis/python-ndb/issues/874
    as2 = JsonProperty()      # only one of the rest will be populated...
    bsky = JsonProperty()     # Bluesky / AT Protocol
    mf2 = JsonProperty()      # HTML microformats2 item (ie _not_ the top level
                              # parse object with items inside an 'items' field)
    our_as1 = JsonProperty()  # AS1 for activities that we generate or modify ourselves

    # Protocol and subclasses set these in fetch if this Object is new or if its
    # contents have changed from what was originally loaded from the datastore.
    new = None
    changed = None

    @ComputedJsonProperty
    def as1(self):
        # TODO: switch back to assert?
        # assert (self.as2 is not None) ^ (self.bsky is not None) ^ (self.mf2 is not None), \
        #     f'{self.as2} {self.bsky} {self.mf2}'
        if bool(self.as2) + bool(self.bsky) + bool(self.mf2) > 1:
            logger.warning(f'{self.key} has multiple! {bool(self.as2)} {bool(self.bsky)} {bool(self.mf2)}')

        if self.our_as1 is not None:
            return redirect_unwrap(self.our_as1)
        elif self.as2 is not None:
            return as2.to_as1(redirect_unwrap(self.as2))
        elif self.bsky is not None:
            return bluesky.to_as1(self.bsky)
        elif self.mf2 is not None:
            return microformats2.json_to_object(self.mf2,
                                                rel_urls=self.mf2.get('rel-urls'))

    @ndb.ComputedProperty
    def type(self):  # AS1 objectType, or verb if it's an activity
        if self.as1:
            return as1.object_type(self.as1)

    def _object_ids(self):  # id(s) of inner objects
        if self.as1:
            return redirect_unwrap(as1.get_ids(self.as1, 'object'))
    object_ids = ndb.ComputedProperty(_object_ids, repeated=True)

    deleted = ndb.BooleanProperty()

    delivered = ndb.StructuredProperty(Target, repeated=True)
    undelivered = ndb.StructuredProperty(Target, repeated=True)
    failed = ndb.StructuredProperty(Target, repeated=True)

    created = ndb.DateTimeProperty(auto_now_add=True)
    updated = ndb.DateTimeProperty(auto_now=True)

    # For certain types, automatically delete this Object after 90d using a
    # TTL policy:
    # https://cloud.google.com/datastore/docs/ttl#ttl_properties_and_indexes
    # They recommend not indexing TTL properties:
    # https://cloud.google.com/datastore/docs/ttl#ttl_properties_and_indexes
    def _expire(self):
        if self.type in OBJECT_EXPIRE_TYPES:
            return (self.updated or util.now()) + OBJECT_EXPIRE_AGE
    expire = ndb.ComputedProperty(_expire, indexed=False)

    def _pre_put_hook(self):
        assert '^^' not in self.key.id()

        if self.as1 and self.as1.get('objectType') == 'activity':
            if 'activity' not in self.labels:
                self.labels.append('activity')
        else:
            if 'activity' in self.labels:
                self.labels.remove('activity')

    def _post_put_hook(self, future):
        """Update :meth:`Protocol.load` cache."""
        # TODO: assert that as1 id is same as key id? in pre put hook?

        # log, pruning data fields
        props = self.to_dict()
        for prop in 'as2', 'bsky', 'mf2':
            if props.get(prop):
                props[prop] = "..."
        logger.info(f'Wrote {self.key} {props}')

        if '#' not in self.key.id():
            import protocol  # TODO: actually fix this circular import
            # make a copy so that if we later modify this object in memory,
            # those modifications don't affect the cache.
            # NOTE: keep in sync with Protocol.load!
            protocol.objects_cache[self.key.id()] = Object(
                id=self.key.id(),
                # exclude computed properties
                **self.to_dict(exclude=['as1', 'expire', 'object_ids', 'type']))

    @classmethod
    def get_by_id(cls, id):
        """Override Model.get_by_id to un-escape ^^ to #.

        https://github.com/snarfed/bridgy-fed/issues/469

        See "meth:`proxy_url()` for the inverse.
        """
        return super().get_by_id(id.replace('^^', '#'))

    def clear(self):
        """Clears all data properties."""
        for prop in 'as2', 'bsky', 'mf2':
            val = getattr(self, prop, None)
            if val:
                logger.warning(f'Wiping out {prop}: {json_dumps(val, indent=2)}')
            setattr(self, prop, None)

    def as_as2(self):
        """Returns this object as an AS2 dict."""
        return self.as2 or as2.from_as1(self.as1) or {}

    def proxy_url(self):
        """Returns the Bridgy Fed proxy URL to render this post as HTML.

        Escapes # characters to ^^.
        https://github.com/snarfed/bridgy-fed/issues/469

        See "meth:`get_by_id()` for the inverse.
        """
        assert '^^' not in self.key.id()
        id = self.key.id().replace('#', '^^')
        # TODO: canonicalize to ABBREV? but need to handle eg ui
        return common.host_url(f'convert/{self.source_protocol}/web/{id}')

    def actor_link(self):
        """Returns a pretty actor link with their name and profile picture."""
        attrs = {'class': 'h-card u-author'}

        if (self.source_protocol in ('web', 'webmention', 'ui') and g.user
                and (g.user.key in self.users or g.user.key.id() in self.domains)):
            # outbound; show a nice link to the user
            return g.user.user_page_link()

        actor = (util.get_first(self.as1, 'actor')
                 or util.get_first(self.as1, 'author')
                 or {})
        if isinstance(actor, str):
            return common.pretty_link(actor, attrs=attrs)

        url = util.get_first(actor, 'url') or ''
        name = actor.get('displayName') or actor.get('username') or ''
        image = util.get_url(actor, 'image')
        if not image:
            return common.pretty_link(url, text=name, attrs=attrs)

        return f"""\
        <a class="h-card u-author" href="{url}" title="{name}">
          <img class="profile" src="{image}" />
          {util.ellipsize(name, chars=40)}
        </a>"""


class AtpNode(StringIdModel):
    """An AT Protocol (Bluesky) node.

    May be a data record, an MST node, or a commit.

    Key name is the DAG-CBOR base32 CID of the data.

    Properties:
    * data: JSON-decoded DAG-JSON value of this node
    * obj: optional, Key of the corresponding :class:`Object`, only populated
      for records
    """
    data = JsonProperty(required=True)
    obj = ndb.KeyProperty(Object)

    @staticmethod
    def create(data):
        """Writes a new AtpNode to the datastore.

        Args:
          data: dict value

        Returns:
          :class:`AtpNode`
        """
        data = json.loads(dag_json.encode(data))
        cid = dag_cbor_cid(data)
        node = AtpNode(id=cid.encode('base32'), data=data)
        node.put()
        return node


class Follower(ndb.Model):
    """A follower of a Bridgy Fed user."""
    STATUSES = ('active', 'inactive')

    # these are both subclasses of User
    from_ = ndb.KeyProperty(name='from', required=True)
    to = ndb.KeyProperty(required=True)

    follow = ndb.KeyProperty(Object)  # last follow activity
    status = ndb.StringProperty(choices=STATUSES, default='active')

    created = ndb.DateTimeProperty(auto_now_add=True)
    updated = ndb.DateTimeProperty(auto_now=True)

    # OLD. some stored entities still have these; do not reuse.
    # src = ndb.StringProperty()
    # dest = ndb.StringProperty()
    # last_follow = JsonProperty()

    def _pre_put_hook(self):
        if self.from_.kind() == 'Fake' and self.to.kind() == 'Fake':
            return

        # we're a bridge! stick with bridging.
        assert self.from_.kind() != self.to.kind(), f'from {self.from_} to {self.to}'

    def _post_put_hook(self, future):
        logger.info(f'Wrote {self}')

    @classmethod
    @ndb.transactional()
    def get_or_create(cls, *, from_, to, **kwargs):
        """Returns a Follower with the given from_ and to users.

        If a matching Follower doesn't exist in the datastore, creates it first.

        Args:
          from_: :class:`User`
          to: :class:`User`

        Returns:
          :class:`Follower`
        """
        assert from_
        assert to

        follower = Follower.query(Follower.from_ == from_.key,
                                  Follower.to == to.key,
                                  ).get()
        if not follower:
            follower = Follower(from_=from_.key, to=to.key, **kwargs)
            follower.put()
        elif kwargs:
            # update existing entity with new property values, eg to make an
            # inactive Follower active again
            for prop, val in kwargs.items():
                setattr(follower, prop, val)
            follower.put()

        return follower

    @staticmethod
    def fetch_page(collection):
        """Fetches a page of Followers for the current user.

        Wraps :func:`fetch_page`. Paging uses the `before` and `after` query
        parameters, if available in the request.

        Args:
          collection, str, 'followers' or 'following'

        Returns:
          (followers, new_before, new_after) tuple with:
          followers: list of :class:`Follower` entities, annotated with an extra
            `user` attribute that holds the follower or following :class:`User`
          new_before, new_after: str query param values for `before` and `after`
            to fetch the previous and next pages, respectively
        """
        assert collection in ('followers', 'following'), collection

        filter_prop = Follower.to if collection == 'followers' else Follower.from_
        query = Follower.query(
            Follower.status == 'active',
            filter_prop == g.user.key,
        ).order(-Follower.updated)

        followers, before, after = fetch_page(query, Follower)
        users = ndb.get_multi(f.from_ if collection == 'followers' else f.to
                              for f in followers)
        User.load_multi(u for u in users if u)

        for f, u in zip(followers, users):
            f.user = u

        return followers, before, after


def fetch_page(query, model_class):
    """Fetches a page of results from a datastore query.

    Uses the `before` and `after` query params (if provided; should be ISO8601
    timestamps) and the queried model class's `updated` property to identify the
    page to fetch.

    Populates a `log_url_path` property on each result entity that points to a
    its most recent logged request.

    Args:
      query: :class:`ndb.Query`
      model_class: ndb model class

    Returns:
      (results, new_before, new_after) tuple with:
      results: list of query result entities
      new_before, new_after: str query param values for `before` and `after`
        to fetch the previous and next pages, respectively
    """
    # if there's a paging param ('before' or 'after'), update query with it
    # TODO: unify this with Bridgy's user page
    def get_paging_param(param):
        val = request.values.get(param)
        if val:
            try:
                dt = util.parse_iso8601(val.replace(' ', '+'))
            except BaseException as e:
                error(f"Couldn't parse {param}, {val!r} as ISO8601: {e}")
            if dt.tzinfo:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt

    before = get_paging_param('before')
    after = get_paging_param('after')
    if before and after:
        error("can't handle both before and after")
    elif after:
        query = query.filter(model_class.updated >= after).order(model_class.updated)
    elif before:
        query = query.filter(model_class.updated < before).order(-model_class.updated)
    else:
        query = query.order(-model_class.updated)

    query_iter = query.iter()
    results = sorted(itertools.islice(query_iter, 0, PAGE_SIZE),
                     key=lambda r: r.updated, reverse=True)

    # calculate new paging param(s)
    has_next = results and query_iter.probably_has_next()
    new_after = (
        before if before
        else results[0].updated if has_next and after
        else None)
    if new_after:
        new_after = new_after.isoformat()

    new_before = (
        after if after else
        results[-1].updated if has_next
        else None)
    if new_before:
        new_before = new_before.isoformat()

    return results, new_before, new_after
