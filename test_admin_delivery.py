from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import admin_delivery
from admin_runtime import public_podcasts
from podcast_sync import fingerprint
from tracking_delivery import lookup_key


class AdminDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.store = Mock()
        self.profile = dict(kind='pinepods', active=True, settings=dict(url='http://pine:8055', username='listener'))
        self.store.list_connections.return_value = {'profiles':[self.profile]}
        self.env = dict(TRACKING_PROFILE_ORIGIN='http://pine:8055', TRACKING_RELAY_UPSTREAM='https://pine.example',
                        TRACKING_PINE_USER_ID='2', TRACKING_DELIVERY_DB='/test/relay.sqlite')

    def test_effective_profile_bound_to_compose_alias_only_for_exact_server(self):
        with patch.object(admin_delivery,'search_episode_delivery',return_value={'state':'ok'}) as read:
            args = admin_delivery.parameters('q=H%C3%B6ren&offset=40',search=True)
            self.assertEqual(admin_delivery.report(self.store,args,search=True,env=self.env)['state'], 'ok')
            read.assert_called_once_with(Path('/test/relay.sqlite'),upstream='https://pine.example',username='listener',user_id=2,query='Hören',offset=40)
            self.profile['settings']['url']='http://another:8055'
            admin_delivery.report(self.store,args,search=True,env=self.env)
            self.assertEqual(read.call_args.kwargs['upstream'], 'http://another:8055')

    def test_row_uses_exact_hash_and_does_not_disclose_subscription(self):
        key = lookup_key('https://pod.example/feed?ticket=privat','https://pod.example/folgeä.mp3')
        self.assertEqual(key, fingerprint(['https://pod.example/feed?ticket=privat','https://pod.example/folgeä.mp3']))
        row = dict(id='_podcast:show:ep', delivery_lookup=key, feed_url='privat')
        public = public_podcasts({'episodes':[row]}, {}, False)
        self.assertNotIn('privat', str(public))
        self.store.status.return_value = {'podcasts':public}
        with patch.object(admin_delivery,'read_episode_delivery',return_value={'state':'not_found'}) as read:
            admin_delivery.report(self.store,{'episode_id':row['id']},search=False,env=self.env)
            self.assertEqual(read.call_args.kwargs['lookup_hash'],key)
            self.assertEqual(admin_delivery.report(self.store,{'episode_id':'other'},search=False,env=self.env)['state'],'not_found')
            self.assertEqual(read.call_count,1)
