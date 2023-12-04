"""Unit tests for webmention.py."""
import copy
from unittest.mock import ANY, patch

from flask import g, get_flashed_messages
from google.cloud import ndb
from granary import as1, as2, atom, microformats2
from oauth_dropins.webutil import util
from oauth_dropins.webutil import appengine_info
from oauth_dropins.webutil.testutil import NOW, requests_response
from oauth_dropins.webutil.util import json_dumps, json_loads
import requests
from werkzeug.exceptions import BadGateway, BadRequest

# import first so that Fake is defined before URL routes are registered
from . import testutil

from activitypub import ActivityPub
import common
from common import CONTENT_TYPE_HTML
from flask_app import app
from models import Follower, Object
import web
from web import SUPERFEEDR_PUSH_API, TASKS_LOCATION, Web
from . import test_activitypub
from .testutil import Fake, TestCase


FULL_REDIR = requests_response(
    status=302,
    redirected_url='http://localhost/.well-known/webfinger?resource=acct:user.com@user.com')

ACTOR_HTML = """\
<html>
<body class="h-card">
<a class="p-name u-url" rel="me" href="https://user.com/">Ms. ☕ Baz</a>
<a href="http://localhost/"></a>
</body>
</html>
"""
ACTOR_HTML_RESP = requests_response(ACTOR_HTML, url='https://user.com/')
ACTOR_HTML_METAFORMATS = """\
<html>
<head>
<title>Ms. ☕ Baz</title>
<meta property="article:author" content="/" />
</head>
</html>
"""
ACTOR_MF2 = {
    'type': ['h-card'],
    'properties': {
        'url': ['https://user.com/'],
        'name': ['Ms. ☕ Baz'],
    },
}
ACTOR_MF2_REL_URLS = {
    **ACTOR_MF2,
    'rel-urls': {'https://user.com/': {'rels': ['me'], 'text': 'Ms. ☕ Baz'}}
}
ACTOR_AS1_UNWRAPPED = {
    'objectType': 'person',
    'id': 'https://user.com/',
    'url': 'https://user.com/',
    'displayName': 'Ms. ☕ Baz',
}
ACTOR_AS2 = {
    'type': 'Person',
    'id': 'http://localhost/user.com',
    'url': 'http://localhost/r/https://user.com/',
    'name': 'Ms. ☕ Baz',
    'preferredUsername': 'user.com',
    'inbox': 'http://localhost/user.com/inbox',
    'outbox': 'http://localhost/user.com/outbox',
}
ACTOR_AS2_FULL = {
    **ACTOR_AS2,
    '@context': [
        'https://www.w3.org/ns/activitystreams',
        'https://w3id.org/security/v1',
    ],
    'attachment': [{
        'name': 'Web site',
        'type': 'PropertyValue',
        'value': '<a rel="me" href="https://user.com"><span class="invisible">https://</span>user.com</a>',
    }],
    'inbox': 'http://localhost/user.com/inbox',
    'outbox': 'http://localhost/user.com/outbox',
    'following': 'http://localhost/user.com/following',
    'followers': 'http://localhost/user.com/followers',
    'endpoints': {
        'sharedInbox': 'http://localhost/inbox',
    },
}

REPOST_HTML = """\
<html>
<body class="h-entry">
<a class="u-url" href="https://user.com/repost"></a>
<a class="u-repost-of p-name" href="https://mas.to/toot/id">reposted!</a>
<a class="u-author h-card" href="https://user.com/">Ms. ☕ Baz</a>
<a href="http://localhost/"></a>
</body>
</html>
"""
REPOST = requests_response(REPOST_HTML, url='https://user.com/repost')
REPOST_MF2 = util.parse_mf2(REPOST_HTML)['items'][0]
REPOST_AS2 = {
    '@context': 'https://www.w3.org/ns/activitystreams',
    'type': 'Announce',
    'id': 'http://localhost/r/https://user.com/repost',
    'url': 'http://localhost/r/https://user.com/repost',
    'name': 'reposted!',
    'object': 'https://mas.to/toot/id',
    'to': [as2.PUBLIC_AUDIENCE],
    'cc': [
        'https://mas.to/author',
        'https://mas.to/bystander',
        'https://mas.to/recipient',
        as2.PUBLIC_AUDIENCE,
    ],
    'actor': 'http://localhost/user.com',
}

REPOST_HCITE_HTML = """\
<html>
<body class="h-entry">
<a class="u-url p-name" href="https://user.com/repost">reposted!</a>
<div class="u-repost-of h-cite">
  <a class="p-author h-card" href="https://mas.to/@foo">Mr. Foo</a>:</p>
  <a class="u-url" href="https://mas.to/toot/id">a post</a>
</div>
<a class="u-author h-card" href="https://user.com/">Ms. ☕ Baz</a>
<a href="http://localhost/"></a>
</body>
</html>
"""
REPOST_HCITE = requests_response(REPOST_HTML, url='https://user.com/repost')

WEBMENTION_REL_LINK = requests_response(
    '<html><head><link rel="webmention" href="/webmention"></html>')
WEBMENTION_NO_REL_LINK = requests_response('<html></html>')

DELETE_AS1 = {
    'objectType': 'activity',
    'verb': 'delete',
    'id': 'https://user.com/post#bridgy-fed-delete',
    'actor': 'http://localhost/user.com',
    'object': 'https://user.com/post',
}
DELETE_AS2 = {
    '@context': 'https://www.w3.org/ns/activitystreams',
    'type': 'Delete',
    'id': 'http://localhost/r/https://user.com/post#bridgy-fed-delete',
    'actor': 'http://localhost/user.com',
    'object': 'http://localhost/r/https://user.com/post',
    'to': [as2.PUBLIC_AUDIENCE],
}

TOOT_HTML = requests_response("""\
<html>
<meta>
<link href='https://mas.to/toot/atom' rel='alternate' type='application/atom+xml'>
<link href='https://mas.to/toot/id' rel='alternate' type='application/activity+json'>
</meta>
</html>
""", url='https://mas.to/toot')
TOOT_AS2_DATA = {
    '@context': ['https://www.w3.org/ns/activitystreams'],
    'type': 'Article',
    'id': 'https://mas.to/toot/id',
    'content': 'Lots of ☕ words...',
    'actor': {'url': 'https://mas.to/author'},
    'to': ['https://mas.to/recipient', as2.PUBLIC_AUDIENCE],
    'cc': ['https://mas.to/bystander', as2.PUBLIC_AUDIENCE],
}
TOOT_AS2 = requests_response(
    TOOT_AS2_DATA, url='https://mas.to/toot/id',
    content_type=as2.CONTENT_TYPE + '; charset=utf-8')
REPLY_HTML = """\
<html>
<body>
<div class="h-entry">
<a class="u-url" href="https://user.com/reply"></a>
<p class="e-content p-name">
<a class="u-in-reply-to" href="http://not/fediverse"></a>
<a class="u-in-reply-to" href="https://mas.to/toot">foo ☕ bar</a>
<a href="http://localhost/"></a>
</p>
<a class="p-author h-card" href="https://user.com/">Ms. ☕ Baz</a>
</div>
</body>
</html>
"""
REPLY = requests_response(REPLY_HTML, url='https://user.com/reply')
REPLY_MF2 = util.parse_mf2(REPLY_HTML)['items'][0]
REPLY_AS1 = microformats2.json_to_object(REPLY_MF2)
REPLY_AS1['id'] = 'https://user.com/reply'
REPLY_AS1['author']['id'] = 'https://user.com/'
CREATE_REPLY_AS1 = {
    'objectType': 'activity',
    'verb': 'post',
    'id': 'https://user.com/reply#bridgy-fed-create',
    'actor': ACTOR_AS1_UNWRAPPED,
    'object': REPLY_AS1,
    'published': '2022-01-02T03:04:05+00:00',
}
REPLY_AS2 = as2.from_as1(REPLY_AS1)

LIKE_HTML = """\
<html>
<body class="h-entry">
<a class="u-url" href="https://user.com/like"></a>
<a class="u-like-of" href="https://mas.to/toot"></a>
<!--<a class="u-like-of p-name" href="https://mas.to/toot">liked!</a>-->
<a class="p-author h-card" href="https://user.com/">Ms. ☕ Baz</a>
<a href="http://localhost/"></a>
</body>
</html>
"""
LIKE = requests_response(LIKE_HTML, url='https://user.com/like')
LIKE_MF2 = util.parse_mf2(LIKE_HTML)['items'][0]

ACTOR = TestCase.as2_resp({
    'type': 'Person',
    'name': 'Mrs. ☕ Foo',
    'id': 'https://mas.to/mrs-foo',
    'inbox': 'https://mas.to/inbox',
})

AS2_CREATE = {
    '@context': 'https://www.w3.org/ns/activitystreams',
    'type': 'Create',
    'id': 'http://localhost/r/https://user.com/reply#bridgy-fed-create',
    'actor': 'http://localhost/user.com',
    'published': '2022-01-02T03:04:05+00:00',
    'object': {
        'type': 'Note',
        'id': 'http://localhost/r/https://user.com/reply',
        'url': 'http://localhost/r/https://user.com/reply',
        'name': 'foo ☕ bar',
        'content': """\
<a class="u-in-reply-to" href="http://not/fediverse"></a>
<a class="u-in-reply-to" href="https://mas.to/toot">foo ☕ bar</a>
<a href="http://localhost/"></a>""",
        'contentMap': {
            'en': """\
<a class="u-in-reply-to" href="http://not/fediverse"></a>
<a class="u-in-reply-to" href="https://mas.to/toot">foo ☕ bar</a>
<a href="http://localhost/"></a>""",
        },
        'inReplyTo': 'https://mas.to/toot/id',
        'to': [as2.PUBLIC_AUDIENCE],
        'cc': [
            'https://mas.to/author',
            'https://mas.to/bystander',
            'https://mas.to/recipient',
            as2.PUBLIC_AUDIENCE,
        ],
        'attributedTo': 'http://localhost/user.com',
        'tag': [{
            'type': 'Mention',
            'href': 'https://mas.to/author',
        }],
    },
    'to': [as2.PUBLIC_AUDIENCE],
}
AS2_UPDATE = copy.deepcopy(AS2_CREATE)
AS2_UPDATE.update({
    'id': 'http://localhost/r/https://user.com/reply#bridgy-fed-update-2022-01-02T03:04:05+00:00',
    'type': 'Update',
})
del AS2_UPDATE['published']
# we should generate this if it's not already in mf2 because Mastodon
# requires it for updates
AS2_UPDATE['object']['updated'] = NOW.isoformat()

FOLLOW_HTML = """\
<html>
<body class="h-entry">
<a class="u-url" href="https://user.com/follow"></a>
<a class="u-follow-of" href="https://mas.to/mrs-foo"></a>
<a class="p-author h-card" href="https://user.com/">Ms. ☕ Baz</a>
<a href="http://localhost/"></a>
</body>
</html>
"""

FOLLOW = requests_response(FOLLOW_HTML, url='https://user.com/follow')
FOLLOW_MF2 = util.parse_mf2(FOLLOW_HTML)['items'][0]
FOLLOW_AS1 = microformats2.json_to_object(FOLLOW_MF2)
FOLLOW_AS2 = {
    '@context': 'https://www.w3.org/ns/activitystreams',
    'type': 'Follow',
    'id': 'http://localhost/r/https://user.com/follow',
    'url': 'http://localhost/r/https://user.com/follow',
    'object': 'https://mas.to/mrs-foo',
    'actor': 'http://localhost/user.com',
    'to': [as2.PUBLIC_AUDIENCE],
}

FOLLOW_FRAGMENT_HTML = """\
<html>
<body>
<article class=h-entry id=1>
<h1>Ignored</h1>
</article>
<article class=h-entry id=2>
<a class="u-url" href="https://user.com/follow#2"></a>
<a class="u-follow-of" href="https://mas.to/mrs-foo"></a>
<a class="p-author h-card" href="https://user.com/">Ms. ☕ Baz</a>
<a href="http://localhost/"></a>
</article>
</body>
</html>
"""
FOLLOW_FRAGMENT = requests_response(
    FOLLOW_FRAGMENT_HTML, url='https://user.com/follow')
FOLLOW_FRAGMENT_MF2 = \
    util.parse_mf2(FOLLOW_FRAGMENT_HTML, id='2')['items'][0]
FOLLOW_FRAGMENT_AS2 = {
    **FOLLOW_AS2,
    'id': 'http://localhost/r/https://user.com/follow#2',
    'url': 'http://localhost/r/https://user.com/follow#2',
}

NOTE_HTML = """\
<html>
<body class="h-entry">
<a class="u-url" href="https://user.com/post"></a>
<p class="e-content p-name">hello i am a post</p>
<a class="p-author h-card" href="https://user.com/">
  <p class="p-name">Ms. ☕ <span class="p-nickname">Baz</span></p>
</a>
<a href="http://localhost/"></a>
</body>
</html>
"""
NOTE = requests_response(NOTE_HTML, url='https://user.com/post')
NOTE_MF2 = util.parse_mf2(NOTE_HTML)['items'][0]
NOTE_AS1 = microformats2.json_to_object(NOTE_MF2)
NOTE_AS1.update({
    'author': {
        **NOTE_AS1['author'],
        'id': 'https://user.com/',
    },
    'id': 'https://user.com/post',
})
NOTE_AS2 = {
    'type': 'Note',
    'id': 'http://localhost/r/https://user.com/post',
    'url': 'http://localhost/r/https://user.com/post',
    'attributedTo': 'http://localhost/user.com',
    'name': 'hello i am a post',
    'content': 'hello i am a post',
    'contentMap': {'en': 'hello i am a post'},
    'to': [as2.PUBLIC_AUDIENCE],
}
CREATE_AS1 = {
    'objectType': 'activity',
    'verb': 'post',
    'id': 'https://user.com/post#bridgy-fed-create',
    'actor': ACTOR_AS1_UNWRAPPED,
    'object': NOTE_AS1,
    'published': '2022-01-02T03:04:05+00:00',
}
CREATE_AS2 = {
    '@context': 'https://www.w3.org/ns/activitystreams',
    'type': 'Create',
    'id': 'http://localhost/r/https://user.com/post#bridgy-fed-create',
    'actor': 'http://localhost/user.com',
    'object': NOTE_AS2,
    'published': '2022-01-02T03:04:05+00:00',
    'to': [as2.PUBLIC_AUDIENCE],
}
UPDATE_AS2 = copy.deepcopy(CREATE_AS2)
UPDATE_AS2.update({
    'type': 'Update',
    'id': 'http://localhost/r/https://user.com/post#bridgy-fed-update-2022-01-02T03:04:05+00:00',
})
del UPDATE_AS2['published']
UPDATE_AS2['object']['updated'] = NOW.isoformat()

NOT_FEDIVERSE = requests_response("""\
<html>
<body class="h-entry">
<a class="u-author h-card" href="https://not/">Mr. Not</a>
<div class="e-content">foo</div>
</body>
</html>
""", url='http://not/fediverse')
ACTIVITYPUB_GETS = [
    REPLY,
    NOT_FEDIVERSE,  # AP
    NOT_FEDIVERSE,  # Web
    TOOT_AS2,       # AP
    ACTOR,
]


@patch('requests.post')
@patch('requests.get')
class WebTest(TestCase):
    def setUp(self):
        super().setUp()

        obj = Object(id='https://user.com/', mf2=ACTOR_MF2, source_protocol='web')
        obj.put()
        self.user = self.make_user('user.com', cls=Web, has_redirects=True, obj=obj)

        self.mrs_foo = ndb.Key(ActivityPub, 'https://mas.to/mrs-foo')

    def assert_deliveries(self, mock_post, inboxes, data, ignore=()):
        self.assertEqual(len(inboxes), len(mock_post.call_args_list),
                         mock_post.call_args_list)

        calls = {}  # maps inbox URL to JSON data
        for args, kwargs in mock_post.call_args_list:
            self.assertEqual(as2.CONTENT_TYPE, kwargs['headers']['Content-Type'])
            rsa_key = kwargs['auth'].header_signer._rsa._key
            self.assertEqual(self.user.private_pem(), rsa_key.exportKey())
            calls[args[0]] = json_loads(kwargs['data'])

        for inbox in inboxes:
            got = calls[inbox]
            as1.get_object(got).pop('publicKey', None)
            self.assert_equals(data, got, inbox, ignore=ignore)

    def assert_object(self, id, **props):
        return super().assert_object(id, delivered_protocol='activitypub', **props)

    def make_followers(self):
        self.followers = []

        for id, kwargs, actor in [
            ('https://mastodon/aaa', {}, None),
            ('https://mastodon/bbb', {}, {
                'publicInbox': 'https://public/inbox',
                'inbox': 'https://unused',
            }),
            ('https://mastodon/ccc', {}, {
                'endpoints': {
                    'sharedInbox': 'https://shared/inbox',
                },
            }),
            ('https://mastodon/ddd', {}, {
               'inbox': 'https://inbox',
            }),
            ('https://mastodon/ggg', {'status': 'inactive'}, {
                'inbox': 'https://unused/2',
            }),
            ('https://mastodon/hhh', {}, {
                # dupe of ddd; should be de-duped
                'inbox': 'https://inbox',
            }),
        ]:
            from_ = self.make_user(id, cls=ActivityPub, obj_as2=actor)
            f = Follower.get_or_create(to=self.user, from_=from_, **kwargs)
            if f.status != 'inactive':
                self.followers.append(from_.key)

    def test_put_validates_domain_id(self, *_):
        for bad in (
            'AbC.cOm',
            'foo',
            '@user.com',
            '@user.com@user.com',
            'acct:user.com',
            'acct:@user.com@user.com',
            'acc:me@user.com',
            'fed.brid.gy',
            'ap.brid.gy',
            'localhost',
        ):
            with self.assertRaises(AssertionError):
                Web(id=bad).put()

    def test_get_or_create_lower_cases_domain(self, mock_get, mock_post):
        mock_get.return_value = requests_response('')

        user = Web.get_or_create('AbC.oRg')
        self.assertEqual('abc.org', user.key.id())
        self.assert_entities_equal(user, Web.get_by_id('abc.org'))
        self.assertIsNone(Web.get_by_id('AbC.oRg'))

    def test_get_or_create_unicode_domain(self, mock_get, mock_post):
        mock_get.return_value = requests_response('')

        user = Web.get_or_create('☃.net')
        self.assertEqual('☃.net', user.key.id())
        self.assert_entities_equal(user, Web.get_by_id('☃.net'))

    def test_get_or_create_home_page_url(self, mock_get, mock_post):
        mock_get.return_value = requests_response('')

        user = Web.get_or_create('https://foo.com/')
        self.assertEqual('foo.com', user.key.id())
        self.assert_entities_equal(user, Web.get_by_id('foo.com'))

    def test_get_or_create_scripts_leading_trailing_dots(self, mock_get, mock_post):
        mock_get.return_value = requests_response('')

        user = Web.get_or_create('..foo.bar.')
        self.assertEqual('foo.bar', user.key.id())
        self.assert_entities_equal(user, Web.get_by_id('foo.bar'))
        self.assertIsNone(Web.get_by_id('..foo.bar.'))

    def test_get_or_create_existing(self, *_):
        user = Web.get_or_create('user.com')
        self.assertEqual('user.com', user.key.id())
        self.assert_entities_equal(user, self.user)

    def test_get_or_create_existing_opted_out(self, *_):
        self.user.obj.mf2['properties']['summary'] = '#nobridge'
        self.user.obj.put()
        self.user.put()
        self.assertIsNone(Web.get_or_create('user.com'))

    def test_bad_source_url(self, *mocks):
        orig_count = Object.query().count()

        for data in b'', {'source': 'bad'}, {'source': 'https://'}:
            got = self.post('/webmention', data=data)
            self.assertEqual(400, got.status_code)
            self.assertEqual(orig_count, Object.query().count())

    def test_username(self, *mocks):
        self.assertEqual('user.com', self.user.username())

        self.user.obj = Object(id='a', as2={
            'type': 'Person',
            'name': 'foo',
            'url': ['bar'],
            'preferredUsername': 'baz',
        })
        self.user.direct = True
        self.assertEqual('user.com', self.user.username())

        # bad acct: URI, util.parse_acct_uri raises ValueError
        # https://console.cloud.google.com/errors/detail/CPLmrpzFs4qTUA;time=P30D?project=bridgy-federated
        self.user.obj.as2['url'].append('acct:@user.com')
        self.assertEqual('user.com', self.user.username())

        self.user.obj.as2['url'].append('acct:alice@foo.com')
        self.assertEqual('user.com', self.user.username())

        self.user.obj.as2['url'].append('acct:alice@user.com')
        self.assertEqual('alice', self.user.username())

        self.user.direct = False
        self.assertEqual('user.com', self.user.username())

    @patch('oauth_dropins.webutil.appengine_config.tasks_client.create_task')
    def test_make_task(self, mock_create_task, mock_get, mock_post):
        common.RUN_TASKS_INLINE = False
        mock_get.side_effect = [NOTE, ACTOR]

        params = {
            'source': 'https://user.com/post',
            'target': 'https://fed.brid.gy/',
        }
        got = self.post('/webmention', data=params)

        self.assertEqual(202, got.status_code)
        self.assert_task(mock_create_task, 'webmention', '/queue/webmention',
                         **params)

        self.assertEqual(NOW, self.user.key.get().last_webmention_in)

    def test_no_user(self, mock_get, mock_post):
        orig_count = Object.query().count()

        got = self.post('/webmention', data={'source': 'https://nope.com/post'})
        self.assertEqual(400, got.status_code)
        self.assertEqual(orig_count, Object.query().count())

    def test_source_fetch_fails(self, mock_get, mock_post):
        orig_count = Object.query().count()

        mock_get.side_effect = (
            requests_response(REPLY_HTML, status=405),
        )

        got = self.post('/queue/webmention',
                               data={'source': 'https://user.com/post'})
        self.assertEqual(502, got.status_code)
        self.assertEqual(orig_count, Object.query().count())

    def test_no_source_entry(self, mock_get, mock_post):
        orig_count = Object.query().count()

        mock_get.return_value = requests_response("""
<html>
<body>
<p>nothing to see here except <a href="http://localhost/">link</a></p>
</body>
</html>""", url='https://user.com/post')

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/post',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(304, got.status_code)
        self.assertEqual(orig_count, Object.query().count())

        mock_get.assert_has_calls((self.req('https://user.com/post'),))

    def test_no_targets(self, mock_get, mock_post):
        mock_get.return_value = requests_response("""
<html>
<body class="h-entry">
<p class="e-content">no one to send to!</p>
</body>
<a href="http://localhost/"></a>
</html>""", url='https://user.com/post')

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/post',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(204, got.status_code)

        mock_get.assert_has_calls((self.req('https://user.com/post'),))

    def test_bad_target_url(self, mock_get, mock_post):
        mock_get.side_effect = (
            requests_response(
                REPLY_HTML.replace('https://mas.to/toot', 'bad:nope')\
                          .replace('http://not/fediverse', ''),
                content_type=CONTENT_TYPE_HTML, url='https://user.com/reply'),
            ValueError('foo bar'),  # AS2 fetch
            ValueError('foo bar'),  # HTML fetch
        )

        got = self.post('/queue/webmention',
                        data={'source': 'https://user.com/reply'})
        self.assertEqual(204, got.status_code)

        self.assert_object('https://user.com/reply',
                           source_protocol='web',
                           type='comment',
                           labels=[],
                           ignore=['our_as1', 'mf2'],
                           )
        self.assert_object('https://user.com/reply#bridgy-fed-create',
                           source_protocol='web',
                           our_as1=CREATE_REPLY_AS1,
                           type='post',
                           labels=['activity', 'user'],
                           ignore=['our_as1'],
                           status='ignored',
                           users=[self.user.key],
                           )

    def test_target_fetch_fails(self, mock_get, mock_post):
        mock_get.side_effect = [
            requests_response(
                REPLY_HTML.replace('https://mas.to/toot', 'bad:nope'),
                url='https://user.com/post'),
            # http://not/fediverse AP protocol discovery
            requests.Timeout('foo bar'),
            # http://not/fediverse web protocol discovery
            requests.Timeout('foo bar'),
        ]

        got = self.post('/queue/webmention',
                               data={'source': 'https://user.com/reply'})
        self.assertEqual(204, got.status_code)

    def test_target_fetch_has_no_content_type(self, mock_get, mock_post):
        Object(id='http://not/fediverse', mf2=NOTE_MF2, source_protocol='web').put()

        no_content_type = requests_response(REPLY_HTML, content_type='')

        mock_get.side_effect = (
            requests_response(REPLY_HTML, url='https://user.com/reply'),
            # requests:
            no_content_type,  # https://mas.to/toot AP protocol discovery
            no_content_type,  # https://mas.to/toot Web protocol discovery
            no_content_type,  # https://user.com/ webmention discovery
            no_content_type,  # http://not/fediverse webmention discovery
        )
        got = self.post('/queue/webmention',
                               data={'source': 'https://user.com/reply'})
        self.assertEqual(204, got.status_code)
        mock_post.assert_not_called()

    def test_missing_backlink(self, mock_get, mock_post):
        orig_count = Object.query().count()

        mock_get.return_value = requests_response(
            REPLY_HTML.replace('<a href="http://localhost/"></a>', ''),
            url='https://user.com/reply')

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/reply',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(304, got.status_code)
        self.assertEqual(orig_count, Object.query().count())

        mock_get.assert_has_calls((self.req('https://user.com/reply'),))

    def test_backlink_without_trailing_slash(self, mock_get, mock_post):
        mock_get.return_value = requests_response(
            REPLY_HTML.replace('<a href="http://localhost/"></a>',
                               '<a href="http://localhost"></a>'),
            content_type=CONTENT_TYPE_HTML, url='https://user.com/reply')

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/reply',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(204, got.status_code)

    def test_create_reply(self, mock_get, mock_post):
        mock_get.side_effect = ACTIVITYPUB_GETS
        mock_post.return_value = requests_response('abc xyz')

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/reply',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(202, got.status_code)

        mock_get.assert_has_calls((
            self.req('https://user.com/reply'),
            self.as2_req('http://not/fediverse'),
            self.req('http://not/fediverse'),
            self.as2_req('https://mas.to/toot'),
            self.as2_req('https://mas.to/author'),
        ))

        self.assert_deliveries(mock_post, ['https://mas.to/inbox'], AS2_CREATE)

        self.assert_object('https://user.com/reply',
                           source_protocol='web',
                           our_as1=REPLY_AS1,
                           type='comment',
                           )
        author = ndb.Key(ActivityPub, 'https://mas.to/author')
        self.assert_object('https://user.com/reply#bridgy-fed-create',
                           users=[self.user.key],
                           notify=[author],
                           source_protocol='web',
                           status='complete',
                           our_as1=CREATE_REPLY_AS1,
                           delivered=['https://mas.to/inbox'],
                           type='post',
                           )

    def test_update_reply(self, mock_get, mock_post):
        self.make_followers()

        mf2 = {
            'properties': {
                'content': ['other'],
            },
        }
        Object(id='https://user.com/reply', status='complete', mf2=mf2).put()

        mock_get.side_effect = ACTIVITYPUB_GETS
        mock_post.return_value = requests_response('abc xyz')

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/reply',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(202, got.status_code)

        self.assertEqual(1, mock_post.call_count)
        args, kwargs = mock_post.call_args
        self.assertEqual(('https://mas.to/inbox',), args)
        self.assert_equals(AS2_UPDATE, json_loads(kwargs['data']))

    def test_redo_repost_isnt_update(self, mock_get, mock_post):
        """Like and Announce shouldn't use Update, they should just resend as is."""
        Object(id='https://user.com/repost', mf2={}, status='complete').put()

        mock_get.side_effect = [REPOST, TOOT_AS2, ACTOR]
        mock_post.return_value = requests_response('abc xyz')

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/repost',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(202, got.status_code)
        self.assert_deliveries(mock_post, ['https://mas.to/inbox'], REPOST_AS2,
                               ignore=['cc'])

    def test_skip_update_if_content_unchanged(self, mock_get, mock_post):
        """https://github.com/snarfed/bridgy-fed/issues/78"""
        self.store_object(id='https://user.com/reply', mf2=REPLY_MF2)
        self.store_object(id='https://user.com/reply#bridgy-fed-create',
                          status='complete')

        mock_get.side_effect = ACTIVITYPUB_GETS

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/reply',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(204, got.status_code)
        mock_post.assert_not_called()

    def test_force_with_content_unchanged_sends_create(self, mock_get, mock_post):
        Object(id='https://user.com/reply', mf2=REPLY_MF2).put()

        mock_get.side_effect = ACTIVITYPUB_GETS
        mock_post.return_value = requests_response('abc xyz')

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/reply',
            'target': 'https://fed.brid.gy/',
            'force': '',
        })
        self.assertEqual(202, got.status_code)

        args, kwargs = mock_post.call_args
        self.assertEqual(('https://mas.to/inbox',), args)
        self.assert_equals(AS2_CREATE, json_loads(kwargs['data']))

    def test_create_reply_attributed_to_id_only(self, mock_get, mock_post):
        """Based on PeerTube's AS2.

        https://github.com/snarfed/bridgy-fed/issues/40
        """
        toot_as2_data = copy.deepcopy(TOOT_AS2_DATA)
        del toot_as2_data['actor']
        toot_as2_data['attributedTo'] = {
            'type': 'Person',
            'id': 'https://mas.to/author',
        }

        mock_get.side_effect = [
            REPLY,
            NOT_FEDIVERSE,  # AP
            NOT_FEDIVERSE,  # Web
            self.as2_resp(toot_as2_data),  # AP
            ACTOR,
        ]
        mock_post.return_value = requests_response('abc xyz')

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/reply',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(202, got.status_code)

        mock_get.assert_has_calls((
            self.req('https://user.com/reply'),
            self.as2_req('http://not/fediverse'),
            self.req('http://not/fediverse'),
            self.as2_req('https://mas.to/toot'),
            self.as2_req('https://mas.to/author'),
        ))

        args, kwargs = mock_post.call_args
        self.assertEqual(('https://mas.to/inbox',), args)
        self.assert_equals(AS2_CREATE, json_loads(kwargs['data']))

    def test_repost(self, mock_get, mock_post):
        self._test_repost(REPOST_HTML, REPOST_AS2, mock_get, mock_post)

    def test_repost_composite_hcite(self, mock_get, mock_post):
        self._test_repost(REPOST_HCITE_HTML, REPOST_AS2, mock_get, mock_post)

    def _test_repost(self, html, expected_as2, mock_get, mock_post):
        self.make_followers()

        mock_get.side_effect = [
            requests_response(html, url='https://user.com/repost'),
            TOOT_AS2,
            ACTOR,
        ]
        mock_post.return_value = requests_response('abc xyz')

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/repost',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(202, got.status_code)

        mock_get.assert_has_calls((
            self.req('https://user.com/repost'),
            self.as2_req('https://mas.to/toot/id'),
            self.as2_req('https://mas.to/author'),
        ))

        inboxes = ('https://inbox/', 'https://public/inbox',
                   'https://shared/inbox', 'https://mas.to/inbox')
        self.assert_deliveries(mock_post, inboxes, expected_as2, ignore=['cc'])

        for args, kwargs in mock_get.call_args_list[1:]:
            with self.subTest(url=args[0]):
                rsa_key = kwargs['auth'].header_signer._rsa._key
                self.assertEqual(self.user.private_pem(), rsa_key.exportKey())

        mf2 = util.parse_mf2(html)['items'][0]
        author_key = ndb.Key('ActivityPub', 'https://mas.to/author')
        self.assert_object('https://user.com/repost',
                           users=[self.user.key],
                           notify=[author_key],
                           feed=self.followers,
                           source_protocol='web',
                           status='complete',
                           mf2=mf2,
                           delivered=inboxes,
                           type='share',
                           object_ids=['https://mas.to/toot/id'],
                           labels=['user', 'activity', 'notification', 'feed'],
                           )

    def test_link_rel_alternate_as2(self, mock_get, mock_post):
        mock_get.side_effect = [
            REPLY,
            NOT_FEDIVERSE,  # AP
            NOT_FEDIVERSE,  # Web
            TOOT_HTML,      # AP
            TOOT_AS2,       # AP via rel-alternate
            ACTOR,
        ]
        mock_post.return_value = requests_response('abc xyz')

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/reply',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(202, got.status_code)

        mock_get.assert_has_calls((
            self.req('https://user.com/reply'),
            self.as2_req('http://not/fediverse'),
            self.req('http://not/fediverse'),
            self.as2_req('https://mas.to/toot'),
            self.as2_req('https://mas.to/toot/id', headers=as2.CONNEG_HEADERS),
            self.as2_req('https://mas.to/author'),
        ))

        args, kwargs = mock_post.call_args
        self.assertEqual(('https://mas.to/inbox',), args)
        self.assert_equals(AS2_CREATE, json_loads(kwargs['data']))

    def test_like_stored_object(self, mock_get, mock_post):
        Object(id='https://mas.to/toot', source_protocol='ap').put()
        Object(id='https://user.com/', mf2=ACTOR_MF2).put()
        mock_get.side_effect = [
            LIKE,
            ACTOR_HTML_RESP,
        ]

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/like',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(204, got.status_code)

        mock_get.assert_has_calls((
            self.req('https://user.com/like'),
        ))
        mock_post.assert_not_called()

        self.assert_object('https://user.com/like',
                           users=[self.user.key],
                           source_protocol='web',
                           mf2=LIKE_MF2,
                           type='like',
                           labels=['activity', 'user'],
                           status='ignored',
                           )

    def test_post_type_discovery_multiple_types(self, mock_get, mock_post):
        self.make_followers()

        mock_get.return_value = requests_response(
            NOTE_HTML.replace('<a href="http://localhost/"></a>', """
  <a class="u-like-of" href="https://alice.com/post"></a>
  <a class="u-bookmark-of" href="http://bob.com/post"></a>
  <a href="http://localhost/"></a>
"""), url='https://user.com/post')

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/multiple',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(202, got.status_code)

        inboxes = ['https://inbox/', 'https://public/inbox', 'https://shared/inbox']
        expected = {
            **NOTE_AS2,
            'attributedTo': None,
            'type': 'Create',
            'actor': 'http://localhost/user.com',
            # TODO: this is an awkward wart left over from the multi-type mf2.
            # remove it eventually.
            'object': {
                'targetUrl': 'http://bob.com/post',
                'to': ['https://www.w3.org/ns/activitystreams#Public'],
            },
        }
        del expected['contentMap']
        self.assert_deliveries(mock_post, inboxes, expected)

    def test_create_default_url_to_wm_source(self, mock_get, mock_post):
        """Source post has no u-url. AS2 id should default to webmention source."""
        missing_url = requests_response("""\
<html>
<body class="h-entry">
<a class="u-repost-of p-name" href="https://mas.to/toot">reposted!</a>
<a class="p-author h-card" href="https://user.com/">Ms. ☕ Baz</a>
<a href="http://localhost/"></a>
</body>
</html>
""", url='https://user.com/repost')
        mock_get.side_effect = [missing_url, TOOT_AS2, ACTOR]
        mock_post.return_value = requests_response('abc xyz')

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/repost',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(202, got.status_code)

        args, kwargs = mock_post.call_args
        self.assertEqual(('https://mas.to/inbox',), args)
        self.assert_equals(REPOST_AS2, json_loads(kwargs['data']))

    def test_create_author_only_url(self, mock_get, mock_post):
        """Mf2 author property is just a URL. We should run full authorship.

        https://indieweb.org/authorship
        """
        repost = requests_response("""\
<html>
<body class="h-entry">
<a class="u-repost-of p-name" href="https://mas.to/toot">reposted!</a>
<a class="u-author" href="https://user.com/"></a>
<a href="http://localhost/"></a>
</body>
</html>
""", url='https://user.com/repost')
        mock_get.side_effect = [repost, ACTOR, TOOT_AS2, ACTOR]
        mock_post.return_value = requests_response('abc xyz')

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/repost',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(202, got.status_code)

        args, kwargs = mock_post.call_args
        self.assertEqual(('https://mas.to/inbox',), args)
        self.assert_equals(REPOST_AS2, json_loads(kwargs['data']))

    def test_create_no_author(self, mock_get, mock_post):
        """No mf2 author. We should default to the user's homepage."""
        mock_get.side_effect = [
            requests_response("""\
<html>
<body class="h-entry">
<a class="u-repost-of p-name" href="https://mas.to/toot/id">reposted!</a>
<a href="http://localhost/"></a>
</body>
</html>
""", url='https://user.com/repost'),
            NOT_FEDIVERSE,
            TOOT_AS2,
            ACTOR,
        ]
        mock_post.return_value = requests_response('abc xyz')

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/repost',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(202, got.status_code)

        repost_mf2 = copy.deepcopy(REPOST_MF2)
        repost_mf2['properties']['author'] = ['https://user.com/']
        self.assert_object('https://user.com/repost',
                           users=[self.user.key],
                           source_protocol='web',
                           mf2=repost_mf2,  # includes author https://user.com/
                           type='share',
                           labels=['activity', 'user'],
                           notify=[ndb.Key('ActivityPub', 'https://mas.to/author')],
                           delivered=['https://mas.to/inbox'],
                           status='complete',
                           )

    def test_create_non_domain_author(self, mock_get, mock_post):
        """Author is a page on the user's domain."""
        self.make_followers()

        mock_get.side_effect = [
            requests_response(NOTE_HTML.replace(
                '<a class="p-author h-card" href="https://user.com/">',
                '<a class="p-author h-card" href="https://user.com/hcard">'
            ), url='https://user.com/post'),
            ACTOR_HTML_RESP,
            TOOT_AS2,
            ACTOR,
        ]

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/post',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(202, got.status_code)

        inboxes = ['https://inbox/', 'https://public/inbox', 'https://shared/inbox']
        self.assert_object('https://user.com/post#bridgy-fed-create',
                           users=[self.user.key],
                           source_protocol='web',
                           our_as1=CREATE_AS1,
                           type='post',
                           labels=['activity', 'user'],
                           delivered=inboxes,
                           status='complete',
                           )

    def test_create_post_use_instead_strip_www(self, mock_get, mock_post):
        self.user.obj.mf2 = {
            'type': ['h-card'],
            'properties': {
                # this is the key part to test; Object.as1 uses this as id
                'url': ['https://www.user.com/'],
                'name': ['Ms. ☕ Baz'],
            },
        }
        self.user.obj.put()
        self.make_user('www.user.com', cls=Web, use_instead=self.user.key)
        self.make_followers()

        note_html = NOTE_HTML.replace('https://user.com/', 'https://www.user.com/')
        mock_get.side_effect = [
            requests_response(note_html, url='https://www.user.com/post'),
            ACTOR,
        ]
        mock_post.return_value = requests_response('abc xyz')

        got = self.post('/queue/webmention', data={
            'source': 'https://www.user.com/post',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(202, got.status_code)

        mock_get.assert_has_calls((
            self.req('https://www.user.com/post'),
        ))

        inboxes = ('https://inbox/', 'https://public/inbox', 'https://shared/inbox')
        create_as2 = copy.deepcopy(CREATE_AS2)
        create_as2['id'] = 'http://localhost/r/https://www.user.com/post#bridgy-fed-create'
        create_as2['object'].update({
            'id': 'http://localhost/r/https://www.user.com/post',
            'url': 'http://localhost/r/https://www.user.com/post',
        })
        self.assert_deliveries(mock_post, inboxes, create_as2)

    def test_create_post(self, mock_get, mock_post):
        mock_get.side_effect = [NOTE, ACTOR]
        mock_post.return_value = requests_response('abc xyz')
        self.make_followers()

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/post',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(202, got.status_code)

        mock_get.assert_has_calls((
            self.req('https://user.com/post'),
        ))
        inboxes = ('https://inbox/', 'https://public/inbox', 'https://shared/inbox')
        self.assert_deliveries(mock_post, inboxes, CREATE_AS2)

        self.assert_object('https://user.com/post',
                           our_as1=NOTE_AS1,
                           feed=self.followers,
                           type='note',
                           source_protocol='web',
                           )
        self.assert_object('https://user.com/post#bridgy-fed-create',
                           users=[self.user.key],
                           source_protocol='web',
                           status='complete',
                           our_as1=CREATE_AS1,
                           delivered=inboxes,
                           type='post',
                           )

    def test_update_post(self, mock_get, mock_post):
        mock_get.side_effect = [NOTE, ACTOR]
        mock_post.return_value = requests_response('abc xyz')

        mf2 = copy.deepcopy(NOTE_MF2)
        mf2['properties']['content'] = 'different'
        Object(id='https://user.com/post', users=[self.user.key], mf2=mf2).put()

        self.make_followers()

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/post',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(202, got.status_code)

        mock_get.assert_has_calls((
            self.req('https://user.com/post'),
        ))
        inboxes = ('https://inbox/', 'https://public/inbox', 'https://shared/inbox')
        self.assert_deliveries(mock_post, inboxes, UPDATE_AS2)

        update_as1 = {
            'objectType': 'activity',
            'verb': 'update',
            'id': 'https://user.com/post#bridgy-fed-update-2022-01-02T03:04:05+00:00',
            'actor': ACTOR_AS1_UNWRAPPED,
            'object': {
                **NOTE_AS1,
                'updated': '2022-01-02T03:04:05+00:00',
            },
        }
        self.assert_object(
            f'https://user.com/post#bridgy-fed-update-2022-01-02T03:04:05+00:00',
            users=[self.user.key],
            source_protocol='web',
            status='complete',
            our_as1=update_as1,
            delivered=inboxes,
            type='update',
            labels=['user', 'activity'],
        )

    def test_create_with_image(self, mock_get, mock_post):
        create_html = NOTE_HTML.replace(
            '</body>', '<img class="u-photo" src="http://im/age" />\n</body>')
        mock_get.side_effect = [
            requests_response(create_html, url='https://user.com/post',
                              content_type=CONTENT_TYPE_HTML),
            ACTOR,
        ]
        mock_post.return_value = requests_response('abc xyz ')

        Follower.get_or_create(
            to=self.user,
            from_=self.make_user('http://a', cls=ActivityPub,
                                 obj_as2={'inbox': 'https://inbox'}))
        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/post',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(202, got.status_code)

        self.assertEqual(('https://inbox/',), mock_post.call_args[0])
        create = copy.deepcopy(CREATE_AS2)
        create['object'].update({
            'image': {'url': 'http://im/age', 'type': 'Image'},
            'attachment': [{'url': 'http://im/age', 'type': 'Image'}],
        })
        self.assert_equals(create, json_loads(mock_post.call_args[1]['data']))

    def test_follow(self, mock_get, mock_post):
        mock_get.side_effect = [FOLLOW, ACTOR, WEBMENTION_REL_LINK]
        mock_post.return_value = requests_response('abc xyz')

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/follow',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(202, got.status_code)

        mock_get.assert_has_calls((
            self.req('https://user.com/follow'),
            self.as2_req('https://mas.to/mrs-foo'),
        ))

        self.assert_deliveries(mock_post, ['https://mas.to/inbox'], FOLLOW_AS2)

        obj = self.assert_object('https://user.com/follow',
                                 users=[self.user.key],
                                 notify=[self.mrs_foo],
                                 source_protocol='web',
                                 status='complete',
                                 mf2=FOLLOW_MF2,
                                 delivered=['https://mas.to/inbox'],
                                 type='follow',
                                 object_ids=['https://mas.to/mrs-foo'],
                                 labels=['user', 'activity', 'notification'],
                                 )

        to = self.assert_user(ActivityPub, 'https://mas.to/mrs-foo', obj_as2={
            'name': 'Mrs. ☕ Foo',
            'id': 'https://mas.to/mrs-foo',
            'inbox': 'https://mas.to/inbox',
            'type': 'Person',
        })

        followers = Follower.query().fetch()
        self.assertEqual(1, len(followers))
        self.assertEqual(self.user.key, followers[0].from_)
        self.assertEqual(to.key, followers[0].to)
        self.assert_equals(obj.key, followers[0].follow)

    def test_follow_no_actor(self, mock_get, mock_post):
        self.user.obj_key = Object(id='a', as2=ACTOR_AS2).put()
        self.user.put()

        html = FOLLOW_HTML.replace(
            '<a class="p-author h-card" href="https://user.com/">Ms. ☕ Baz</a>', '')
        follow = requests_response(html, url='https://user.com/follow',
                                   content_type=CONTENT_TYPE_HTML)

        mock_get.side_effect = [
            follow,
            ACTOR_HTML_RESP,  # authorship on follower
            ACTOR,  # followee AS2
        ]
        mock_post.return_value = requests_response('abc xyz')

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/follow',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(202, got.status_code)

        args, kwargs = mock_post.call_args
        self.assertEqual(('https://mas.to/inbox',), args)
        self.assert_equals(FOLLOW_AS2, json_loads(kwargs['data']))

    def test_follow_no_target(self, mock_get, mock_post):
        self.make_followers()

        html = FOLLOW_HTML.replace(
            '<a class="u-follow-of" href="https://mas.to/mrs-foo"></a>',
            '<a class="u-follow-of"></a>')
        follow = requests_response(html, url='https://user.com/follow',
                                   content_type=CONTENT_TYPE_HTML)

        mock_get.side_effect = [follow, ACTOR]

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/follow',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(204, got.status_code)
        mock_post.assert_not_called()

    def test_follow_fragment(self, mock_get, mock_post):
        mock_get.side_effect = [FOLLOW_FRAGMENT, ACTOR]
        mock_post.return_value = requests_response('abc xyz')

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/follow#2',
            'target': 'https://fed.brid.gy/',
        })
        self.assert_equals(202, got.status_code)

        mock_get.assert_has_calls((
            self.req('https://user.com/follow'),
            self.as2_req('https://mas.to/mrs-foo'),
        ))

        self.assert_deliveries(mock_post, ['https://mas.to/inbox'],
                               FOLLOW_FRAGMENT_AS2)

        self.assert_object('https://user.com/follow#2',
                           users=[self.user.key],
                           notify=[self.mrs_foo],
                           source_protocol='web',
                           status='complete',
                           mf2=FOLLOW_FRAGMENT_MF2,
                           delivered=['https://mas.to/inbox'],
                           type='follow',
                           object_ids=['https://mas.to/mrs-foo'],
                           labels=['user', 'activity', 'notification',],
                           )

        followers = Follower.query().fetch()
        self.assert_equals(1, len(followers))
        self.assert_equals(self.user.key, followers[0].from_)
        self.assert_equals(ActivityPub(id='https://mas.to/mrs-foo').key,
                           followers[0].to)

    def test_follow_multiple(self, mock_get, mock_post):
        html = FOLLOW_HTML.replace(
            '<a class="u-follow-of" href="https://mas.to/mrs-foo"></a>',
            '<a class="u-follow-of" href="https://mas.to/mrs-foo"></a> '
            '<a class="u-follow-of" href="https://mas.to/mr-biff"></a>')

        mock_get.side_effect = [
            requests_response(
                html, url='https://user.com/follow',
                content_type=CONTENT_TYPE_HTML),
            ACTOR,
            self.as2_resp({
                'objectType': 'Person',
                'displayName': 'Mr. ☕ Biff',
                'id': 'https://mas.to/mr-biff',
                'inbox': 'https://mas.to/inbox/biff',
            }),
        ]
        mock_post.return_value = requests_response('unused')

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/follow',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(202, got.status_code)

        mock_get.assert_has_calls((
            self.req('https://user.com/follow'),
            self.as2_req('https://mas.to/mrs-foo'),
            self.as2_req('https://mas.to/mr-biff'),
        ))

        self.assertCountEqual([
            (('https://mas.to/inbox',), FOLLOW_AS2),
            (('https://mas.to/inbox/biff',), {
                **FOLLOW_AS2,
                'object': 'https://mas.to/mr-biff',
            }),
        ], [(args, json_loads(kwargs['data']))
            for args, kwargs in mock_post.call_args_list])

        mf2 = util.parse_mf2(html)['items'][0]
        mr_biff = ndb.Key(ActivityPub, 'https://mas.to/mr-biff')
        obj = self.assert_object('https://user.com/follow',
                                 users=[self.user.key],
                                 notify=[self.mrs_foo, mr_biff],
                                 source_protocol='web',
                                 status='complete',
                                 mf2=mf2,
                                 delivered=['https://mas.to/inbox',
                                            'https://mas.to/inbox/biff'],
                                 type='follow',
                                 object_ids=['https://mas.to/mrs-foo',
                                             'https://mas.to/mr-biff'],
                                 labels=['user', 'activity', 'notification',],
                                 )

        followers = Follower.query().fetch()
        self.assertEqual(2, len(followers))

        self.assertEqual(self.user.key, followers[0].from_)
        self.assertEqual(ActivityPub(id='https://mas.to/mr-biff').key,
                         followers[0].to)
        self.assert_equals(obj.key, followers[0].follow)

        self.assertEqual(self.user.key, followers[1].from_)
        self.assertEqual(ActivityPub(id='https://mas.to/mrs-foo').key,
                         followers[1].to)
        self.assert_equals(obj.key, followers[1].follow)

    def test_error_fragment_missing(self, mock_get, mock_post):
        mock_get.return_value = requests_response(
            FOLLOW_FRAGMENT_HTML, url='https://user.com/follow',
            content_type=CONTENT_TYPE_HTML)

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/follow#3',
            'target': 'https://fed.brid.gy/',
        })
        self.assert_equals(304, got.status_code)
        mock_get.assert_has_calls((
            self.req('https://user.com/follow'),
        ))

    def test_delete(self, mock_get, mock_post):
        mock_get.return_value = requests_response('"unused"', status=410,
                                                  url='http://final/delete')
        mock_post.return_value = requests_response('unused', status=200)
        Object(id='https://user.com/post#bridgy-fed-create',
               mf2=NOTE_MF2, status='complete').put()

        self.make_followers()

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/post',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(202, got.status_code, got.text)

        inboxes = ('https://inbox/', 'https://public/inbox', 'https://shared/inbox')
        self.assert_deliveries(mock_post, inboxes, DELETE_AS2)

        self.assert_object('https://user.com/post#bridgy-fed-delete',
                           users=[self.user.key],
                           source_protocol='web',
                           status='complete',
                           our_as1={
                               **DELETE_AS1,
                               'actor': ACTOR_AS1_UNWRAPPED,
                           },
                           delivered=inboxes,
                           type='delete',
                           object_ids=['https://user.com/post'],
                           labels=['user', 'activity'],
                           )

    def test_delete_no_object(self, mock_get, mock_post):
        mock_get.side_effect = [
            requests_response('"unused"', status=410, url='http://final/delete'),
        ]
        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/post',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(304, got.status_code, got.text)
        mock_post.assert_not_called()

    def test_delete_incomplete_response(self, mock_get, mock_post):
        mock_get.return_value = requests_response('"unused"', status=410,
                                                  url='http://final/delete')

        Object(id='https://user.com/post#bridgy-fed-create',
               mf2=NOTE_MF2, status='in progress')

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/post',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(304, got.status_code, got.text)
        mock_post.assert_not_called()

    def test_error(self, mock_get, mock_post):
        mock_get.side_effect = [FOLLOW, ACTOR]
        mock_post.return_value = requests_response(
            'abc xyz', status=405, url='https://mas.to/inbox')

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/follow',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(202, got.status_code)

        mock_get.assert_has_calls((
            self.req('https://user.com/follow'),
            self.as2_req('https://mas.to/mrs-foo'),
        ))

        self.assert_deliveries(mock_post, ['https://mas.to/inbox'], FOLLOW_AS2)

        self.assert_object('https://user.com/follow',
                           users=[self.user.key],
                           notify=[self.mrs_foo],
                           source_protocol='web',
                           status='failed',
                           mf2=FOLLOW_MF2,
                           failed=['https://mas.to/inbox'],
                           type='follow',
                           object_ids=['https://mas.to/mrs-foo'],
                           labels=['user', 'activity', 'notification',],
                           )

    def test_repost_twitter_blocklisted(self, *mocks):
        self._test_repost_blocklisted_error('https://twitter.com/foo', *mocks)

    def test_repost_bridgy_fed_blocklisted(self, *mocks):
        self._test_repost_blocklisted_error('https://fed.brid.gy/foo', *mocks)

    def _test_repost_blocklisted_error(self, orig_url, mock_get, mock_post):
        """Reposts of non-fediverse (ie blocklisted) sites aren't yet supported."""
        repost_html = REPOST_HTML.replace('https://mas.to/toot', orig_url)
        repost_resp = requests_response(repost_html, url='https://user.com/repost')
        mock_get.side_effect = [repost_resp]

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/repost',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(204, got.status_code)
        mock_post.assert_not_called()

    def test_update_profile(self, mock_get, mock_post):
        mock_get.side_effect = [ACTOR_HTML_RESP]
        mock_post.return_value = requests_response('abc xyz')
        Follower.get_or_create(to=self.user, from_=self.make_user(
            'http://ccc', cls=ActivityPub, obj_as2={
                'endpoints': {
                    'sharedInbox': 'https://shared/inbox',
                },
            }))
        Follower.get_or_create(to=self.user, from_=self.make_user(
            'http://ddd', cls=ActivityPub, obj_as2={'inbox': 'https://inbox'}))

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(202, got.status_code)
        mock_get.assert_has_calls((
            self.req('https://user.com/'),
        ))

        id = 'https://user.com/#update-2022-01-02T03:04:05+00:00'
        wrapped_id = f'http://localhost/r/{id}'
        expected_as2 = {
            '@context': 'https://www.w3.org/ns/activitystreams',
            'type': 'Update',
            'id': wrapped_id,
            'actor': 'http://localhost/user.com',
            'object': {
                **ACTOR_AS2,
                'attachment': ACTOR_AS2_FULL['attachment'],
                'updated': NOW.isoformat(),
                'to': ['https://www.w3.org/ns/activitystreams#Public'],
            },
            'to': ['https://www.w3.org/ns/activitystreams#Public'],
        }
        self.assert_deliveries(mock_post, ('https://shared/inbox', 'https://inbox/'),
                               expected_as2)

        # updated Web user
        expected_actor_as2 = {
            'type': 'Person',
            'id': 'https://user.com/',
            'url': 'https://user.com/',
            'name': 'Ms. ☕ Baz',
            'attachment': [{
                'name': 'Ms. ☕ Baz',
                'type': 'PropertyValue',
                'value': '<a rel="me" href="https://user.com"><span class="invisible">https://</span>user.com</a>',
            }],
            'updated': '2022-01-02T03:04:05+00:00',
        }
        self.assert_user(Web, 'user.com', obj_as2=expected_actor_as2, direct=True,
                         has_redirects=True)

        # homepage object
        actor = {
            'objectType': 'person',
            'id': 'https://user.com/',
            'url': 'https://user.com/',
            'urls': [{'displayName': 'Ms. ☕ Baz', 'value': 'https://user.com/'}],
            'displayName': 'Ms. ☕ Baz',
            'updated': '2022-01-02T03:04:05+00:00',
        }
        self.assert_object('https://user.com/',
                           source_protocol='web',
                           our_as1=actor,
                           type='person',
                           )

        # update activity
        expected_as1 = {
            'objectType': 'activity',
            'verb': 'update',
            'id': id,
            'actor': actor,
            'object': actor,
        }
        self.assert_object(id,
                           users=[self.user.key],
                           source_protocol='web',
                           status='complete',
                           our_as1=expected_as1,
                           delivered=['https://inbox/', 'https://shared/inbox'],
                           type='update',
                           object_ids=['https://user.com/'],
                           labels=['user', 'activity'],
                           )

    def test_update_profile_homepage_no_mf2_or_metaformats(self, mock_get, mock_post):
        mock_get.return_value = requests_response("""
<html>
<body>
<p>nothing to see here except <a href="http://localhost/">link</a></p>
</body>
</html>""", url='https://user.com/')

        mock_post.return_value = requests_response('OK')
        Follower.get_or_create(to=self.user, from_=self.make_user(
            'http://ddd', cls=ActivityPub, obj_as2={'inbox': 'https://inbox'}))

        got = self.post('/queue/webmention', data={
            'source': 'https://user.com/',
            'target': 'https://fed.brid.gy/',
        })
        self.assertEqual(202, got.status_code)
        mock_get.assert_has_calls((
            self.req('https://user.com/'),
        ))

        id = 'https://user.com/#update-2022-01-02T03:04:05+00:00'
        wrapped_id = f'http://localhost/r/{id}'
        update_as2 = {
            '@context': 'https://www.w3.org/ns/activitystreams',
            'type': 'Update',
            'id': wrapped_id,
            'actor': 'http://localhost/user.com',
            'object': {
                **ACTOR_AS2_FULL,
                'name': 'user.com',
                'updated': NOW.isoformat(),
                'to': ['https://www.w3.org/ns/activitystreams#Public'],
            },
            'to': ['https://www.w3.org/ns/activitystreams#Public'],
        }
        del update_as2['object']['endpoints']
        del update_as2['object']['followers']
        del update_as2['object']['following']
        self.assert_deliveries(mock_post, ('https://inbox/',), update_as2)

        # updated Web user
        self.assert_user(Web, 'user.com', direct=True, has_redirects=True, obj_as2={
            'type': 'Person',
            'id': 'https://user.com/',
            'url': 'https://user.com/',
            'name': 'user.com',
            'attachment': [{
                'name': 'Link',
                'type': 'PropertyValue',
                'value': '<a rel="me" href="https://user.com"><span class="invisible">https://</span>user.com</a>',
            }],
            'updated': NOW.isoformat(),
        })

    def test_like_actor_is_not_source_domain(self, mock_get, mock_post):
        like_html = LIKE_HTML.replace(
            'class="p-author h-card" href="https://user.com/"',
            'class="p-author h-card" href="https://eve.com/"')
        mock_get.side_effect = [
            requests_response(like_html, url='https://user.com/like'),
            TOOT_AS2,
            ACTOR,
        ]

        with self.assertLogs() as logs:
            got = self.post('/queue/webmention', data={
                'source': 'https://user.com/like',
                'target': 'https://fed.brid.gy/',
            })
            self.assertEqual(202, got.status_code)

        self.assertIn(
            "WARNING:models:actor https://user.com/ isn't https://user.com/like's author or actor ['https://eve.com/']",
            logs.output)

    @patch('oauth_dropins.webutil.appengine_config.tasks_client.create_task')
    def test_superfeedr_make_task(self, mock_create_task, *_):
        common.RUN_TASKS_INLINE = False

        got = self.post('/superfeedr/notify/user.com', data="""\
<?xml version="1.0" encoding="UTF-8"?>
<entry xmlns="http://www.w3.org/2005/Atom">
<uri>https://user.com/post</uri>
<content>I hereby ☕ post.</content>
</entry>
""", headers={'Content-Type': atom.CONTENT_TYPE})
        self.assertEqual(200, got.status_code)
        self.assert_task(mock_create_task, 'receive', '/queue/receive',
                         obj=Object(id='https://user.com/post').key.urlsafe(),
                         authed_as='user.com')

    def test_superfeedr_no_user(self, *_):
        orig_count = Object.query().count()

        got = self.post('/webmention', data={'source': 'https://nope.com/post'})
        self.assertEqual(400, got.status_code)
        self.assertEqual(orig_count, Object.query().count())

    def test_superfeedr_no_id(self, *mocks):
        got = self.post('/superfeedr/notify/user.com', data="""\
<?xml version="1.0" encoding="UTF-8"?>
<entry xmlns="http://www.w3.org/2005/Atom">
<content>I hereby ☕ post.</content>
</entry>
""", headers={'Content-Type': atom.CONTENT_TYPE})
        self.assertEqual(400, got.status_code)

    @patch('oauth_dropins.webutil.appengine_info.LOCAL_SERVER', False)
    def test_maybe_superfeedr_subscribe(self, mock_get, mock_post):
        self.assertFalse(self.user.superfeedr_subscribed)
        self.user.mf2 = {
            **ACTOR_MF2,
            'rel-urls': {
                'https://foo': {'rels': ['me'], 'text': 'Ms. ☕ Baz'},
                'https://foo/atom': {'rels': ['alternate'], 'type': atom.CONTENT_TYPE},
            },
        }

        web.maybe_superfeedr_subscribe(self.user)
        self.assert_req(mock_post, SUPERFEEDR_PUSH_API, data={
            'hub.mode': 'subscribe',
            'hub.topic': 'https://foo/atom',
            'hub.callback': 'http://localhost/superfeedr/notify/user.com',
            'format': 'atom',
            'retrieve': 'true',
        }, auth=ANY)
        self.assertTrue(self.user.key.get().superfeedr_subscribed)

    def test_maybe_superfeedr_subscribe_no_feed(self, mock_get, mock_post):
        self.user.mf2 = ACTOR_MF2  # no rel-urls
        web.maybe_superfeedr_subscribe(self.user)
        self.assertFalse(self.user.key.get().superfeedr_subscribed)

    def test_maybe_superfeedr_subscribe_already_subscribed(self, mock_get, mock_post):
        self.user.superfeedr_subscribed = True
        self.user.put()
        web.maybe_superfeedr_subscribe(self.user)
        # should be a noop

    def _test_verify(self, redirects, hcard, actor, redirects_error=None):
        self.user.has_redirects = False
        self.user.put()

        got = self.user.verify()
        self.assertEqual(self.user.key, got.key)

        with self.subTest(redirects=redirects, hcard=hcard, actor=actor,
                          redirects_error=redirects_error):
            self.assert_equals(redirects, bool(self.user.has_redirects))
            self.assert_equals(hcard, bool(self.user.has_hcard))
            if actor is None:
                assert not self.user.obj or not self.user.obj.as1
            else:
                got = {k: v for k, v in self.user.obj.as1.items()
                       if k in actor}
                self.assert_equals(actor, got)
            self.assert_equals(redirects_error, self.user.redirects_error)

    def test_verify_neither(self, mock_get, _):
        empty = requests_response('')
        mock_get.side_effect = [empty, empty]
        self._test_verify(False, False, None, """\
<pre>https://user.com/.well-known/webfinger?resource=acct:user.com@user.com
  returned HTTP 200</pre>""")

    def test_verify_redirect_strips_query_params(self, mock_get, _):
        half_redir = requests_response(
            status=302, redirected_url='http://localhost/.well-known/webfinger')
        no_hcard = requests_response('<html><body></body></html>')
        mock_get.side_effect = [half_redir, no_hcard]
        self._test_verify(False, False, None, """\
Current vs expected:<pre>- http://localhost/.well-known/webfinger
+ https://fed.brid.gy/.well-known/webfinger?resource=acct:user.com@user.com</pre>""")

    def test_verify_multiple_redirects(self, mock_get, _):
        two_redirs = requests_response(
            status=302, redirected_url=[
                'https://www.user.com/.well-known/webfinger?resource=acct:user.com@user.com',
                'http://localhost/.well-known/webfinger?resource=acct:user.com@user.com',
            ])
        no_hcard = requests_response('<html><body></body></html>')
        mock_get.side_effect = [two_redirs, no_hcard]
        self._test_verify(True, False, None)

    def test_verify_redirect_404(self, mock_get, _):
        redir_404 = requests_response(status=404, redirected_url='http://this/404s')
        no_hcard = requests_response('<html><body></body></html>')
        mock_get.side_effect = [redir_404, no_hcard]
        self._test_verify(False, False, None, """\
<pre>https://user.com/.well-known/webfinger?resource=acct:user.com@user.com
  redirected to:
http://this/404s
  returned HTTP 404</pre>""")

    def test_verify_webfinger_urlencoded(self, mock_get, _):
        mock_get.side_effect = [
            requests_response(
                status=302,
                redirected_url='http://localhost/.well-known/webfinger?resource=acct%3Auser.com%40user.com'),
            requests_response(''),
        ]
        self._test_verify(True, False, None)

    def test_verify_no_hcard(self, mock_get, _):
        mock_get.side_effect = [
            FULL_REDIR,
            requests_response("""
<body>
<div class="h-review">
  <p class="e-content">foo bar</p>
</div>
</body>
"""),
        ]
        self._test_verify(True, False, None)

    def test_verify_non_representative_hcard(self, mock_get, _):
        bad_hcard = requests_response(
            '<html><body><a class="h-card u-url" href="https://a.b/">acct:me@user.com</a></body></html>',
            url='https://user.com/',
        )
        mock_get.side_effect = [FULL_REDIR, bad_hcard]
        self._test_verify(True, False, None)

    def test_verify_both_work(self, mock_get, _):
        hcard = requests_response("""
<html><body class="h-card">
  <a class="u-url p-name" href="/">me</a>
  <a class="u-url" href="acct:myself@user.com">Masto</a>
</body></html>""",
            url='https://user.com/',
        )
        mock_get.side_effect = [FULL_REDIR, hcard]
        self._test_verify(True, True, {
            'objectType': 'person',
            'displayName': 'me',
            'url': 'https://user.com/',
            'urls': [
                {'value': 'https://user.com/'},
                {'value': 'acct:myself@user.com'},
            ],
        })

    def test_verify_www_redirect(self, mock_get, _):
        www_user = self.make_user('www.user.com', cls=Web)

        empty = requests_response('')
        mock_get.side_effect = [
            requests_response(status=302, redirected_url='https://www.user.com/'),
            empty, empty,
        ]

        got = www_user.verify()
        self.assertEqual('user.com', got.key.id())

        root_user = Web.get_by_id('user.com')
        self.assertEqual(root_user.key, www_user.key.get().use_instead)
        self.assertEqual(root_user.key, Web.get_or_create('www.user.com').key)

    def test_verify_actor_rel_me_links(self, mock_get, _):
        mock_get.side_effect = [
            FULL_REDIR,
            requests_response("""
<body>
<div class="h-card">
<a class="u-url" rel="me" href="/about-me">Mrs. ☕ Foo</a>
<a class="u-url" rel="me" href="/">should be ignored</a>
<a class="u-url" rel="me" href="http://one" title="one title">
  one text
</a>
<a class="u-url" rel="me" href="https://two" title=" two title "> </a>
</div>
</body>
""", url='https://user.com/'),
        ]
        self._test_verify(True, True, {
            'urls': [{
                'value': 'https://user.com/about-me',
                'displayName': 'Mrs. \u2615 Foo',
            }, {
                'value': 'https://user.com/',
                'displayName': 'should be ignored',
            }, {
                'value': 'http://one',
                'displayName': 'one text',
            }, {
                'value': 'https://two',
                'displayName': 'two title',
            }],
        })

    def test_verify_override_preferredUsername(self, mock_get, _):
        mock_get.side_effect = [
            FULL_REDIR,
            requests_response("""
<body>
<a class="h-card u-url" rel="me" href="/about-me">
  <span class="p-nickname">Nick</span>
</a>
</body>
""", url='https://user.com/'),
            NOT_FEDIVERSE,
            NOT_FEDIVERSE,
        ]
        self._test_verify(True, True, {})

        # preferredUsername stays y.z despite user's username. since Mastodon
        # queries Webfinger for preferredUsername@fed.brid.gy
        # https://github.com/snarfed/bridgy-fed/issues/77#issuecomment-949955109
        postprocessed = ActivityPub.convert(self.user.obj, from_user=self.user)
        self.assertEqual('user.com', postprocessed['preferredUsername'])

    def test_web_url(self, _, __):
        self.assertEqual('https://user.com/', self.user.web_url())

    def test_is_web_url(self, *_):
        self.assertTrue(self.user.is_web_url('https://user.com/'))
        self.assertTrue(self.user.is_web_url('https://www.user.com/'))
        self.assertFalse(self.user.is_web_url('https://other.com/'))

    def test_handle_as(self, *_):
        self.user.ap_subdomain = 'web'

        self.assertEqual('@user.com@user.com', self.user.handle_as(ActivityPub))

        self.user.obj = Object(id='a', as2={'type': 'Person'})
        self.assertEqual('@user.com@user.com', self.user.handle_as(ActivityPub))

        self.user.obj.as2 = {'url': 'http://foo'}
        self.assertEqual('@user.com@user.com', self.user.handle_as(ActivityPub))

        self.user.has_redirects = False
        self.assertEqual('@user.com@web.brid.gy', self.user.handle_as(ActivityPub))

        self.user.obj.as2 = {'url': ['http://foo', 'acct:bar@foo', 'acct:baz@user.com']}
        self.user.has_redirects = True
        self.assertEqual('@baz@user.com', self.user.handle_as(ActivityPub))

        self.user.has_redirects = False
        self.assertEqual('@user.com@web.brid.gy', self.user.handle_as(ActivityPub))

        self.assertEqual('fake:handle:user.com', self.user.handle_as(Fake))
        self.assertEqual('user.com.web.brid.gy', self.user.handle_as('atproto'))

        self.user.ap_subdomain = 'fed'
        self.assertEqual('@user.com@fed.brid.gy', self.user.handle_as(ActivityPub))

    def test_id_as(self, *_):
        self.assertEqual('http://localhost/user.com', self.user.id_as(ActivityPub))

        with app.test_request_context('', base_url='https://web.brid.gy/'):
            self.assertEqual('https://web.brid.gy/user.com',
                             self.user.id_as(ActivityPub))

        self.user.ap_subdomain = 'fed'
        with app.test_request_context('', base_url='https://web.brid.gy/'):
            self.assertEqual('https://fed.brid.gy/user.com',
                             self.user.id_as(ActivityPub))

    def test_check_web_site(self, mock_get, _):
        redir = 'http://localhost/.well-known/webfinger?resource=acct:user.com@user.com'
        mock_get.side_effect = (
            requests_response('', status=302, redirected_url=redir),
            ACTOR_HTML_RESP,
        )

        got = self.post('/web-site', data={'url': 'https://user.com/'})
        self.assert_equals(302, got.status_code)
        self.assert_equals('/web/user.com', got.headers['Location'])

        user = Web.get_by_id('user.com')
        self.assertTrue(user.has_hcard)
        self.assertEqual('person', user.obj.as1['objectType'])

    def test_check_web_site_unicode_domain(self, mock_get, _):
        mock_get.side_effect = (
            requests_response(''),
            requests_response(''),
            requests_response(''),
        )

        got = self.post('/web-site', data={'url': 'https://☃.net/'})
        self.assert_equals(302, got.status_code)
        self.assert_equals('/web/%E2%98%83.net', got.headers['Location'])
        self.assertIsNotNone(Web.get_by_id('☃.net'))

    def test_check_web_site_lower_cases_domain(self, mock_get, _):
        mock_get.side_effect = (
            requests_response(''),
            requests_response(''),
            requests_response(''),
        )

        got = self.post('/web-site', data={'url': 'https://AbC.oRg/'})
        self.assert_equals(302, got.status_code)
        self.assert_equals('/web/abc.org', got.headers['Location'])
        self.assertIsNotNone(Web.get_by_id('abc.org'))
        self.assertIsNone(Web.get_by_id('AbC.oRg'))

    def test_check_web_site_bad_url(self, _, __):
        got = self.post('/web-site', data={'url': '!!!'})
        self.assert_equals(400, got.status_code)
        self.assertEqual(['!!! is not a valid or supported web site'],
                         get_flashed_messages())
        self.assertEqual(1, Web.query().count())

    def test_check_web_site_bridgy_fed_domain(self, _, __):
        got = self.post('/web-site', data={'url': 'https://fed.brid.gy/foo'})
        self.assert_equals(400, got.status_code)
        self.assertEqual(
            ['https://fed.brid.gy/foo is not a valid or supported web site'],
            get_flashed_messages())
        self.assertEqual(1, Web.query().count())

    def test_check_web_site_blocklisted(self, _, __):
        got = self.post('/web-site', data={'url': 'https://t.co/'})
        self.assert_equals(400, got.status_code)
        self.assertEqual(['https://t.co/ is not a valid or supported web site'],
                         get_flashed_messages())
        self.assertEqual(1, Web.query().count())

    def test_check_webfinger_redirects_then_fails(self, mock_get, _):
        redir = 'http://localhost/.well-known/webfinger?resource=acct:orig@orig'
        mock_get.side_effect = (
            ACTOR_HTML_RESP,
            requests_response('', status=302, redirected_url=redir),
            requests_response('', status=503),
        )

        got = self.post('/web-site', data={'url': 'https://orig.co/'})
        self.assert_equals(200, got.status_code, got.headers)
        self.assertTrue(get_flashed_messages()[0].startswith(
            "Couldn't connect to https://orig.co/: "))

    def test_check_web_site_fetch_fails(self, mock_get, _):
        mock_get.return_value = requests_response('', status=503)

        got = self.post('/web-site', data={'url': 'https://orig.co/'})
        self.assert_equals(200, got.status_code, got.headers)
        self.assertTrue(get_flashed_messages()[0].startswith(
            "Couldn't connect to https://orig.co/: "))


@patch('requests.post')
@patch('requests.get')
class WebUtilTest(TestCase):

    def setUp(self):
        super().setUp()
        self.user = self.make_user('user.com', cls=Web)

    def test_key_for(self, *_):
        for id in 'user.com', 'http://user.com', 'https://user.com/':
            self.assertEqual(Web(id='user.com').key, Web.key_for(id))

        for bad in '', 'foo', 'https://foo/', 'foo bar', 'user.json':
            with self.subTest(bad=bad):
                self.assertIsNone(Web.key_for(bad))

        # no stored user
        self.assertEqual(Web(id='foo.com').key, Web.key_for('foo.com'))

    def test_key_for_use_instead(self, *_):
        Web(id='www.user.com', use_instead=self.user.key).put()
        self.assertEqual(self.user.key, Web.key_for('www.user.com'))

    def test_handle(self, *_):
        self.assertEqual('user.com', self.user.handle)

    def test_owns_id(self, *_):
        self.assertIsNone(Web.owns_id('http://foo.com'))
        self.assertIsNone(Web.owns_id('https://bar.com/'))
        self.assertIsNone(Web.owns_id('https://bar.com/baz'))
        self.assertIsNone(Web.owns_id('https://bar/'))
        self.assertFalse(Web.owns_id('at://did:plc:foo/bar/123'))
        self.assertFalse(Web.owns_id('e45fab982'))

        self.assertFalse(Web.owns_id('user.com'))
        self.user.has_redirects = True
        self.user.put()
        self.assertTrue(Web.owns_id('user.com'))
        self.user.key.delete()
        self.assertIsNone(Web.owns_id('user.com'))

        self.assertFalse(Web.owns_id('https://twitter.com/foo'))
        self.assertFalse(Web.owns_id('https://fed.brid.gy/foo'))

    def test_owns_handle(self, *_):
        self.assertIsNone(Web.owns_handle('foo.com'))
        self.assertIsNone(Web.owns_handle('foo.bar.com'))

        self.assertFalse(Web.owns_handle('foo'))
        self.assertFalse(Web.owns_handle('@foo'))
        self.assertFalse(Web.owns_handle('@foo.com'))
        self.assertFalse(Web.owns_handle('@foo@bar.com'))
        self.assertFalse(Web.owns_handle('foo@bar.com'))

    def test_handle_to_id(self, *_):
        self.assertEqual('foo.com', Web.handle_to_id('foo.com'))

    def test_profile_id(self, *_):
        self.assertEqual('https://foo.com/', Web(id='foo.com').profile_id())

    def test_fetch(self, mock_get, __):
        mock_get.return_value = REPOST

        obj = Object(id='https://user.com/post')
        Web.fetch(obj)

        self.assert_equals({**REPOST_MF2, 'url': 'https://user.com/repost'}, obj.mf2)

    def test_fetch_redirect(self, mock_get, __):
        mock_get.return_value = requests_response(
            REPOST_HTML, redirected_url='http://new/url')
        obj = Object(id='https://orig/url')
        Web.fetch(obj)

        self.assert_equals('http://new/url', obj.mf2['url'])
        self.assert_equals({**REPOST_MF2, 'url': 'http://new/url'}, obj.mf2)
        self.assertIsNone(Object.get_by_id('http://new/url'))

    def test_fetch_error(self, mock_get, __):
        mock_get.return_value = requests_response(REPOST_HTML, status=405)
        with self.assertRaises(BadGateway):
            Web.fetch(Object(id='https://foo'), gateway=True)

    def test_fetch_run_authorship(self, mock_get, __):
        mock_get.side_effect = [
            # post
            requests_response(
                REPOST_HTML.replace(
                    '<a class="p-author h-card" href="https://user.com/">Ms. ☕ Baz</a>',
                    '<a class="u-author" href="https://user.com/"></a>'),
                content_type=CONTENT_TYPE_HTML, url='https://user.com/repost'),
            # author URL
            ACTOR,
        ]

        obj = Object(id='https://user.com/repost')
        Web.fetch(obj)
        self.assert_equals({**REPOST_MF2, 'url': 'https://user.com/repost'}, obj.mf2)

    def test_fetch_default_missing_author_to_user(self, mock_get, __):
        mock_get.return_value = requests_response("""\
<html>
<body class="h-entry">
<p class="p-name">hello i am a post</p>
</body>
</html>
""", url='https://user.com/post')

        obj = Object(id='https://user.com/post')
        Web.fetch(obj)
        self.assert_equals({
            'type': ['h-entry'],
            'properties': {
                'name': ['hello i am a post'],
                'author': ['https://user.com/'],
                'url': ['https://user.com/post'],
            },
            'url': 'https://user.com/post',
        }, obj.mf2)

    def test_fetch_default_author_missing_url_to_user(self, mock_get, __):
        mock_get.return_value = requests_response("""\
<html>
<body class="h-entry">
<p class="p-author h-card">Alice</p>
</body>
</html>
""", url='https://user.com/post')

        obj = Object(id='https://user.com/post')
        Web.fetch(obj)
        self.assert_equals({
            'type': ['h-entry'],
            'properties': {
                'author': [{
                    'type': ['h-card'],
                    'properties': {
                        'name': ['Alice'],
                        'url': ['https://user.com/'],
                    },
                    'value': 'Alice',
                }],
                'url': ['https://user.com/post'],
            },
            'url': 'https://user.com/post',
        }, obj.mf2)

    def test_load_id_is_url_not_uid(self, mock_get, __):
        mock_get.return_value = requests_response(NOTE_HTML.replace(
            '<a href="http://localhost/"></a>', """\
<data class="p-uid" value="abc123"></data>
<a href="http://localhost/"></a>
"""))

        obj = Web.load('https://user.com/post')
        self.assertEqual('https://user.com/post', obj.key.id())
        self.assertEqual('https://user.com/post', obj.as1['id'])

        self.assertEqual(NOTE_MF2, obj.mf2)
        self.assertNotIn('uid', obj.mf2['properties'])

    def test_fetch_user_homepage(self, mock_get, __):
        mock_get.return_value = ACTOR_HTML_RESP

        obj = Object(id='https://user.com/')
        Web.fetch(obj)

        self.assert_equals({
            **ACTOR_MF2_REL_URLS,
            'url': 'https://user.com/',
        }, obj.mf2)
        self.assert_equals({
            **ACTOR_AS1_UNWRAPPED,
            'urls': [{'value': 'https://user.com/', 'displayName': 'Ms. ☕ Baz'}],
        }, obj.as1)

    def test_fetch_user_homepage_metaformats(self, mock_get, __):
        mock_get.return_value = requests_response(
            ACTOR_HTML_METAFORMATS, url='https://user.com/')

        obj = Object(id='https://user.com/')
        Web.fetch(obj)

        self.assert_equals(ACTOR_MF2, obj.mf2, ignore=['rel-urls', 'url'])
        self.assert_equals(ACTOR_AS1_UNWRAPPED, obj.as1, ignore=['author'])

    def test_fetch_user_homepage_metaformats_mf2_but_no_hcard(self, mock_get, __):
        html = ACTOR_HTML_METAFORMATS.replace('</html>', """\
<div class="h-entry"><span class="e-content">foo</span></div>
</html>""")
        mock_get.return_value = requests_response(html, url='https://user.com/')

        obj = Object(id='https://user.com/')
        Web.fetch(obj)

        self.assert_equals(ACTOR_MF2, obj.mf2, ignore=['rel-urls', 'url'])
        self.assert_equals(ACTOR_AS1_UNWRAPPED, obj.as1, ignore=['author'])

    def test_fetch_user_homepage_no_hcard(self, mock_get, __):
        mock_get.return_value = requests_response('<html><body>foo<body><html>',
                                                  url='https://user.com/')

        obj = Object(id='https://user.com/')
        self.assertTrue(Web.fetch(obj))
        self.assert_equals({
            'type': ['h-card'],
            'properties': {
                'url': ['https://user.com/'],
                'name': ['user.com'],
            },
        }, obj.mf2, ignore=['rel-urls', 'url'])

    def test_fetch_user_homepage_non_representative_hcard(self, mock_get, __):
        mock_get.return_value = requests_response(
            '<html><body><a class="h-card u-url" href="https://a.b/">acct:me@y.z</a></body></html>',
            content_type=CONTENT_TYPE_HTML)

        obj = Object(id='https://user.com/')
        with self.assertRaises(BadRequest):
            Web.fetch(obj)

    def test_fetch_user_homepage_fail(self, mock_get, __):
        mock_get.return_value = requests_response('', status=500)

        obj = Object(id='https://user.com/')
        with self.assertRaises(requests.HTTPError) as e:
            Web.fetch(obj)
            self.assertEqual(500, e.status_code)

    def test_fetch_not_html(self, mock_get, __):
        mock_get.return_value = self.as2_resp({})

        obj = Object(id='https://user.com/post')
        self.assertFalse(Web.fetch(obj))
        self.assertIsNone(obj.as1)

    def test_fetch_non_url(self, mock_get, __):
        obj = Object(id='x y z')
        self.assertFalse(Web.fetch(obj))
        self.assertIsNone(obj.as1)

    def test_fetch_no_mf2(self, mock_get, __):
        mock_get.return_value = requests_response(
            '<html>\n<body>foo</body>\n</html>')

        obj = Object(id='https://user.com/post')
        self.assertFalse(Web.fetch(obj))
        self.assertIsNone(obj.as1)

    def test_send_note_does_nothing(self, mock_get, mock_post):
        Follower.get_or_create(
            to=self.make_user('https://mas.to/bob', cls=ActivityPub),
            from_=self.user)

        self.assertFalse(Web.send(
            Object(id='http://mas.to/note', as2=test_activitypub.NOTE),
            'https://user.com/'))
        mock_get.assert_not_called()
        mock_post.assert_not_called()

    def test_send_unrelated_repost_does_nothing(self, mock_get, mock_post):
        Follower.get_or_create(
            to=self.make_user('https://mas.to/bob', cls=ActivityPub),
            from_=self.user)

        self.assertFalse(Web.send(
            Object(id='http://mas.to/note', as2={
                **test_activitypub.REPOST,
                'actor': 'https://mas.to/bob',
            }),
            'https://user.com/'))
        mock_get.assert_not_called()
        mock_post.assert_not_called()

    def test_send_unrelated_reply_does_nothing(self, mock_get, mock_post):
        Follower.get_or_create(
            to=self.make_user('https://mas.to/bob', cls=ActivityPub),
            from_=self.user)

        self.assertFalse(Web.send(
            Object(id='http://mas.to/note', as2={
                **test_activitypub.REPLY,
                'actor': 'https://mas.to/bob',
            }),
            'https://user.com/'))
        mock_get.assert_not_called()
        mock_post.assert_not_called()

    def test_send_like(self, mock_get, mock_post):
        mock_get.return_value = WEBMENTION_REL_LINK
        mock_post.return_value = requests_response()

        obj = Object(id='http://mas.to/like#ok', as2=test_activitypub.LIKE,
                     source_protocol='ui')
        self.assertTrue(Web.send(obj, 'https://user.com/post'))

        self.assert_req(mock_get, 'https://user.com/post')
        args, kwargs = mock_post.call_args
        self.assertEqual(('https://user.com/webmention',), args)
        self.assertEqual({
            'source': 'https://fed.brid.gy/convert/web/http://mas.to/like%23ok',
            'target': 'https://user.com/post',
        }, kwargs['data'])

    def test_send_no_endpoint(self, mock_get, mock_post):
        mock_get.return_value = WEBMENTION_NO_REL_LINK
        obj = Object(id='http://mas.to/like#ok', as2=test_activitypub.LIKE,
                     source_protocol='activitypub')

        self.assertFalse(Web.send(obj, 'https://user.com/post'))

        self.assert_req(mock_get, 'https://user.com/post')
        mock_post.assert_not_called()

    def test_send_skips_accept_follow(self, mock_get, mock_post):
        obj = Object(id='https://user.com/accept', as2=test_activitypub.ACCEPT)
        self.assertFalse(Web.send(obj, 'https://user.com/'))
        mock_get.assert_not_called()
        mock_post.assert_not_called()

    def test_send_blocklisted(self, mock_get, mock_post):
        obj = Object(id='http://mas.to/like#ok', as2={
            **test_activitypub.LIKE,
            'object': 'https://fed.brid.gy/foo',
        })
        self.assertFalse(Web.send(obj, 'https://fed.brid.gy/foo'))
        mock_get.assert_not_called()
        mock_post.assert_not_called()

    def test_send_errors(self, mock_get, mock_post):
        for err in [
                requests.HTTPError(response=util.Struct(status_code='429', text='')),
                requests.ConnectionError(),
        ]:
            with self.subTest(err=err):
                mock_get.return_value = WEBMENTION_REL_LINK
                mock_post.side_effect = err

                obj = Object(id='http://mas.to/like#ok', as2=test_activitypub.LIKE,
                             source_protocol='ui')
                with self.assertRaises(err.__class__):
                    Web.send(obj, 'https://user.com/post')

                self.assert_req(mock_get, 'https://user.com/post')
                args, kwargs = mock_post.call_args
                self.assertEqual(('https://user.com/webmention',), args)
                self.assertEqual({
                    'source': 'https://fed.brid.gy/convert/web/http://mas.to/like%23ok',
                    'target': 'https://user.com/post',
                }, kwargs['data'])

    def test_convert(self, _, __):
        obj = Object(id='http://orig', mf2=ACTOR_MF2)
        self.assert_multiline_equals("""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8">
<meta http-equiv="refresh" content="0;url=https://user.com/"></head>
<body class="">
  <span class="h-card">
    <data class="p-uid" value="https://user.com/"></data>
    <a class="p-name u-url" href="https://user.com/">Ms. ☕ Baz</a>
  </span>
</body>
</html>
""", Web.convert(obj, from_user=None), ignore_blanks=True)

    def test_convert_translates_ids(self, *_):
        self.store_object(id='http://fed/post', source_protocol='activitypub')

        self.assert_multiline_in("""\
<article class="h-entry">
<span class="p-uid">https://fa.brid.gy/convert/web/fake:reply</span>
<span class="p-author h-card">
  <data class="p-uid" value="https://fa.brid.gy/web/fake:alice"></data>
  <span class="p-name">Ms. Alice</span>
</span>
<span class="p-name"></span>
<div class="">
</div>
<a class="u-in-reply-to" href="https://ap.brid.gy/convert/web/http://fed/post"></a>
</article>""", Web.convert(Object(our_as1={
            'objectType': 'activity',
            'verb': 'post',
            'object': {
                'id': 'fake:reply',
                'objectType': 'note',
                'inReplyTo': 'http://fed/post',
                'author': {
                    'id': 'fake:alice',
                    'displayName': 'Ms. Alice',
                },
            },
        }), from_user=None), ignore_blanks=True)

    def test_target_for(self, _, __):
        self.assertIsNone(Web.target_for(Object(id='x', source_protocol='web')))

        self.assertEqual('http://foo', Web.target_for(
            Object(id='http://foo', source_protocol='web')))
        self.assertEqual('http://foo', Web.target_for(
            Object(id='http://foo', source_protocol='web'), shared=True))
