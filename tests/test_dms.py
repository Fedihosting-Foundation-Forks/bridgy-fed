"""Unit tests for dms.py."""
from unittest import mock

from atproto import ATProto
from common import memcache
import dms
from dms import maybe_send, receive
import ids
from models import DM, Follower, Object
from web import Web

from oauth_dropins.webutil.flask_util import NotModified
from .testutil import ExplicitFake, Fake, OtherFake, TestCase
from .test_atproto import DID_DOC

DM_EFAKE_ALICE_REQUESTS_OTHER_BOB = {
    'objectType': 'note',
    'id': 'efake:dm',
    'actor': 'efake:alice',
    'to': ['other.brid.gy'],
    'content': ' other:handle:bob ',
}
ALICE_CONFIRMATION_CONTENT = """Got it! We'll send <a class="h-card u-author" rel="me" href="web:other:bob" title="other:handle:bob">other:handle:bob</a> a message and say that you hope they'll enable the bridge. Fingers crossed!"""
ALICE_REQUEST_CONTENT = """\
<p>Hi! <a class="h-card u-author" rel="me" href="web:other:efake:alice" title="efake:handle:alice &middot; other:handle:efake:handle:alice">efake:handle:alice &middot; other:handle:efake:handle:alice</a> is using Bridgy Fed to bridge their account from efake-phrase into other-phrase, and they'd like to follow you. You can bridge your account into efake-phrase by following this account. <a href="https://fed.brid.gy/docs">See the docs</a> for more information.
<p>If you do nothing, your account won't be bridged, and users on efake-phrase won't be able to see or interact with you.
<p>Bridgy Fed will only send you this message once."""


class DmsTest(TestCase):
    # def setUp(self):
    #     print('@@@')

    def make_alice_bob(self):
        self.make_user(id='efake.brid.gy', cls=Web)
        self.make_user(id='other.brid.gy', cls=Web)
        alice = self.make_user(id='efake:alice', cls=ExplicitFake,
                               enabled_protocols=['other'], obj_as1={'x': 'y'})
        bob = self.make_user(id='other:bob', cls=OtherFake, obj_as1={'x': 'y'})
        return alice, bob

    def assert_sent(self, from_cls, tos, type, text):
        if not isinstance(tos, list):
            tos = [tos]
        from_id = f'{from_cls.ABBREV}.brid.gy'
        self.assertEqual([
            (f'{to.key.id()}:target', {
                'objectType': 'activity',
                'verb': 'post',
                'id': f'https://{from_id}/#{type}-dm-{to.key.id()}-2022-01-02T03:04:05+00:00-create',
                'actor': from_id,
                'object': {
                    'objectType': 'note',
                    'id': f'https://{from_id}/#{type}-dm-{to.key.id()}-2022-01-02T03:04:05+00:00',
                    'author': from_id,
                    'content': text,
                    'tags': [{'objectType': 'mention', 'url': to.key.id()}],
                    'to': [to.key.id()],
                },
                'to': [to.key.id()],
             })
            for to in tos], tos[0].sent)

    def test_maybe_send(self):
        self.make_user(id='fa.brid.gy', cls=Web)
        user = self.make_user(id='other:user', cls=OtherFake, obj_as1={'x': 'y'})

        maybe_send(from_proto=Fake, to_user=user, text='hi hi hi',
                   type='replied_to_bridged_user')
        self.assert_sent(Fake, user, 'replied_to_bridged_user', 'hi hi hi')
        expected_sent_dms = [DM(protocol='fake', type='replied_to_bridged_user')]
        self.assertEqual(expected_sent_dms, user.key.get().sent_dms)

        # now that this type is in sent_dms, another attempt should be a noop
        OtherFake.sent = []
        maybe_send(from_proto=Fake, to_user=user, text='hi again',
                   type='replied_to_bridged_user')
        self.assertEqual([], OtherFake.sent)
        self.assertEqual(expected_sent_dms, user.key.get().sent_dms)

    def test_maybe_send_no_type(self):
        self.make_user(id='fa.brid.gy', cls=Web)
        user = self.make_user(id='other:user', cls=OtherFake, obj_as1={'x': 'y'})

        maybe_send(from_proto=Fake, to_user=user, text='hi hi hi')
        self.assert_sent(Fake, user, '?', 'hi hi hi')
        self.assertEqual([], user.key.get().sent_dms)

        # another DM without type should also work
        OtherFake.sent = []
        maybe_send(from_proto=Fake, to_user=user, text='hi again')
        self.assert_sent(Fake, user, '?', 'hi again')
        self.assertEqual([], user.key.get().sent_dms)

    def test_maybe_send_user_missing_obj(self):
        self.make_user(id='other.brid.gy', cls=Web)
        user = OtherFake(id='other:user')
        assert not user.obj

        maybe_send(from_proto=OtherFake, to_user=user, text='nope', type='welcome')
        self.assertEqual([], OtherFake.sent)
        self.assertEqual([], user.sent_dms)

    def test_receive_unknown_text(self):
        self.make_user(id='other.brid.gy', cls=Web)
        alice = self.make_user('efake:alice', cls=ExplicitFake,
                               enabled_protocols=['other'], obj_id='efake:alice')

        obj = Object(our_as1={
            **DM_EFAKE_ALICE_REQUESTS_OTHER_BOB,
            'content': 'foo bar',
        })
        with self.assertRaises(NotModified) as e:
            receive(from_user=alice, obj=obj)

        self.assertIn("Couldn't understand DM: foo bar", str(e.exception))
        self.assertEqual([], OtherFake.sent)
        self.assertEqual([], Fake.sent)

    def test_receive_no_yes_sets_enabled_protocols(self):
        alice = self.make_user('fake:alice', cls=Fake, obj_id='fake:alice')
        # bot user
        self.make_user('fa.brid.gy', cls=Web)

        dm = Object(our_as1={
            'objectType': 'note',
            'id': 'efake:dm',
            'actor': 'efake:user',
            'to': ['fa.brid.gy'],
            'content': 'no',
        })

        user = self.make_user('efake:user', cls=ExplicitFake)
        self.assertFalse(user.is_enabled(Fake))

        # fake protocol isn't enabled yet, no DM should be a noop
        self.assertEqual(('OK', 200), receive(from_user=user, obj=dm))
        user = user.key.get()
        self.assertEqual([], user.enabled_protocols)
        self.assertEqual([], Fake.created_for)

        # "yes" DM should add to enabled_protocols
        dm.our_as1['id'] += '2'
        dm.our_as1['content'] = '<p><a href="...">@bsky.brid.gy</a> yes</p>'
        self.assertEqual(('OK', 200), receive(from_user=user, obj=dm))
        user = user.key.get()
        self.assertEqual(['fake'], user.enabled_protocols)
        self.assertEqual(['efake:user'], Fake.created_for)
        self.assertTrue(user.is_enabled(Fake))

        # another "yes" DM should be a noop
        dm.our_as1['id'] += '3'
        Fake.created_for = []
        self.assertEqual(('OK', 200), receive(from_user=user, obj=dm))
        user = user.key.get()
        self.assertEqual(['fake'], user.enabled_protocols)
        self.assertTrue(user.is_enabled(Fake))
        self.assertEqual(['efake:user'], Fake.created_for)

        # "no" DM should remove from enabled_protocols
        Follower.get_or_create(to=user, from_=alice)
        dm.our_as1['id'] += '4'
        dm.our_as1['content'] = '<p><a href="...">@bsky.brid.gy</a>\n  NO \n</p>'
        self.assertEqual(('OK', 200), receive(from_user=user, obj=dm))
        user = user.key.get()
        self.assertEqual([], user.enabled_protocols)
        self.assertFalse(user.is_enabled(Fake))

        # ...and delete copy actor
        self.assertEqual([('fake:shared:target', {
            'objectType': 'activity',
            'verb': 'delete',
            'id': 'efake:user#delete-user-fake-2022-01-02T03:04:05+00:00',
            'actor': 'efake:user',
            'object': 'efake:user',
        })], Fake.sent)

    def test_receive_handle_sends_request_dm(self):
        alice, bob = self.make_alice_bob()

        obj = Object(our_as1=DM_EFAKE_ALICE_REQUESTS_OTHER_BOB)
        self.assertEqual(('OK', 200), receive(from_user=alice, obj=obj))

        self.assert_sent(OtherFake, alice, '?', ALICE_CONFIRMATION_CONTENT)
        self.assert_sent(ExplicitFake, bob, 'request_bridging',
                         ALICE_REQUEST_CONTENT)

    def test_receive_handle_strips_leading_at_sign(self):
        alice, bob = self.make_alice_bob()

        obj = Object(our_as1={
            **DM_EFAKE_ALICE_REQUESTS_OTHER_BOB,
            'content': '@other:handle:bob',
        })
        self.assertEqual(('OK', 200), receive(from_user=alice, obj=obj))
        self.assert_sent(OtherFake, alice, '?', ALICE_CONFIRMATION_CONTENT)
        self.assert_sent(ExplicitFake, bob, 'request_bridging',
                         ALICE_REQUEST_CONTENT)

    def test_receive_handle_fetch_user(self):
        self.make_user(id='efake.brid.gy', cls=Web)
        self.make_user(id='other.brid.gy', cls=Web)
        alice = self.make_user(id='efake:alice', cls=ExplicitFake,
                               enabled_protocols=['other'], obj_as1={'x': 'y'})
        OtherFake.fetchable['other:bob'] = {'x': 'y'}

        obj = Object(our_as1=DM_EFAKE_ALICE_REQUESTS_OTHER_BOB)
        self.assertEqual(('OK', 200), receive(from_user=alice, obj=obj))
        self.assert_sent(OtherFake, alice, '?', ALICE_CONFIRMATION_CONTENT)
        self.assert_sent(ExplicitFake, OtherFake(id='other:bob'),
                         'request_bridging', ALICE_REQUEST_CONTENT)
        self.assertEqual(['other:bob'], OtherFake.fetched)

    def test_receive_handle_user_doesnt_exist(self):
        self.make_user(id='other.brid.gy', cls=Web)
        alice = self.make_user(id='efake:alice', cls=ExplicitFake,
                               enabled_protocols=['other'], obj_as1={'x': 'y'})
        OtherFake.fetchable = {}

        obj = Object(our_as1=DM_EFAKE_ALICE_REQUESTS_OTHER_BOB)
        self.assertEqual(('OK', 200), receive(from_user=alice, obj=obj))
        self.assert_sent(OtherFake, alice, '?', "Couldn't find other-phrase user other:handle:bob")
        self.assertEqual([], OtherFake.sent)

    def test_receive_handle_from_user_not_bridged(self):
        alice, _ = self.make_alice_bob()
        # not bridged into OtherFake
        alice.enabled_protocols = ['fake']
        alice.put()

        obj = Object(our_as1=DM_EFAKE_ALICE_REQUESTS_OTHER_BOB)
        self.assertEqual(('OK', 200), receive(from_user=alice, obj=obj))
        self.assert_sent(OtherFake, alice, '?', 'Please bridge your account to other-phrase by following this account before requesting another user.')
        self.assertEqual([], OtherFake.sent)
        self.assertEqual([], Fake.sent)

    def test_receive_handle_already_bridged(self):
        alice, bob = self.make_alice_bob()
        bob.enabled_protocols = ['efake']
        bob.put()

        obj = Object(our_as1=DM_EFAKE_ALICE_REQUESTS_OTHER_BOB)
        self.assertEqual(('OK', 200), receive(from_user=alice, obj=obj))
        self.assert_sent(OtherFake, alice, '?', """<a class="h-card u-author" rel="me" href="web:efake:other:bob" title="other:handle:bob &middot; efake:handle:other:handle:bob">other:handle:bob &middot; efake:handle:other:handle:bob</a> is already bridged into efake-phrase.""")
        self.assertEqual([], OtherFake.sent)

    def test_receive_handle_already_requested(self):
        alice, bob = self.make_alice_bob()
        bob.sent_dms = [DM(protocol='efake', type='request_bridging')]
        bob.put()

        obj = Object(our_as1=DM_EFAKE_ALICE_REQUESTS_OTHER_BOB)
        self.assertEqual(('OK', 200), receive(from_user=alice, obj=obj))
        self.assert_sent(OtherFake, alice, '?', """We've already sent <a class="h-card u-author" rel="me" href="web:other:bob" title="other:handle:bob">other:handle:bob</a> a DM. Fingers crossed!""")
        self.assertEqual([], OtherFake.sent)
        self.assertEqual([], Fake.sent)

    @mock.patch.object(dms, 'REQUESTS_LIMIT_USER', 2)
    def test_receive_handle_request_rate_limit(self):
        alice, bob = self.make_alice_bob()
        eve = self.make_user(id='other:eve', cls=OtherFake, obj_as1={'x': 'y'})
        frank = self.make_user(id='other:frank', cls=OtherFake, obj_as1={'x': 'y'})

        obj = Object(our_as1=DM_EFAKE_ALICE_REQUESTS_OTHER_BOB)
        self.assertEqual(('OK', 200), receive(from_user=alice, obj=obj))

        obj = Object(our_as1={
            **DM_EFAKE_ALICE_REQUESTS_OTHER_BOB,
            'content': 'other:handle:eve',
        })
        self.assertEqual(('OK', 200), receive(from_user=alice, obj=obj))

        self.assert_sent(ExplicitFake, [bob, eve], 'request_bridging',
                         ALICE_REQUEST_CONTENT)
        self.assertEqual(2, memcache.get('dm-user-requests-efake-efake:alice'))

        # over the limit
        OtherFake.sent = []
        ExplicitFake.sent = []
        obj = Object(our_as1={
            **DM_EFAKE_ALICE_REQUESTS_OTHER_BOB,
            'content': 'other:handle:frank',
        })
        self.assertEqual(('OK', 200), receive(from_user=alice, obj=obj))
        self.assertEqual([], OtherFake.sent)
        self.assert_sent(OtherFake, alice, '?', "Sorry, you've hit your limit of 2 requests per day. Try again tomorrow!")
        self.assertEqual(3, memcache.get('dm-user-requests-efake-efake:alice'))

    def test_receive_handle_wrong_protocol(self):
        self.make_user(id='other.brid.gy', cls=Web)

        obj = Object(our_as1={
            **DM_EFAKE_ALICE_REQUESTS_OTHER_BOB,
            'content': 'fake:eve',
        })
        with self.assertRaises(NotModified) as e:
            receive(from_user=Fake(id='fake:user'), obj=obj)

        self.assertIn("Couldn't understand DM: fake:eve", str(e.exception))
        self.assertEqual([], ExplicitFake.sent)
        self.assertEqual([], OtherFake.sent)
        self.assertEqual([], Fake.sent)

    @mock.patch('ids.translate_handle', side_effect=ValueError('nope'))
    def test_receive_handle_not_supported_in_target_protocol(self, _):
        alice, bob = self.make_alice_bob()
        obj = Object(our_as1=DM_EFAKE_ALICE_REQUESTS_OTHER_BOB)

        self.assertEqual(('OK', 200), receive(from_user=alice, obj=obj))
        self.assert_sent(OtherFake, alice, '?', "Sorry, Bridgy Fed doesn't yet support bridging handle other:handle:bob from other-phrase to efake-phrase.")
        self.assertEqual([], OtherFake.sent)
