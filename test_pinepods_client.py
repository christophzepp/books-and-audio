import copy
import json
import unittest
from unittest.mock import Mock

import requests

from pinepods_client import PinepodsClient, RequestError, EpisodeUnavailable, action_epoch


FEED = 'https://feed.example/podcast.xml?private=not-for-logs'
EPISODE = 'https://media.example/episode.mp3'
STAMP = '2026-11-01T12:00:00'
IMPORT_DISABLED = dict(sync_type='gpodder',gpodder_enabled=True,external_enabled=False,external_url=None)


def response(value, code=200):
    r = Mock(status_code=code, content=json.dumps(value).encode())
    r.json.return_value = value
    return r


def action(position=120, **kw):
    return dict(podcast=FEED, episode=EPISODE, action='play',
                position=position, started=0, total=600, timestamp=STAMP,
                device='phone', **kw)


class PinepodsTests(unittest.TestCase):
    def setUp(self):
        self.c = PinepodsClient('http://192.168.1.20:8055', 'reader', 'hidden-password')
        self.c.session.request = Mock()

    def actions(self, rows):
        self.c.session.request.return_value = response({'timestamp': 1793534400, 'actions': rows})

    def test_probe_is_readonly_and_does_not_create_missing_device(self):
        self.c.session.request.side_effect = [response([]), response({'timestamp': 100, 'actions': []})]
        got = self.c.probe()
        self.assertEqual(got['username'], 'reader')
        self.assertFalse(got['device_exists'])
        for call in self.c.session.request.call_args_list:
            self.assertEqual(call.args[0], 'GET')
            self.assertFalse(call.kwargs['allow_redirects'])
            self.assertTrue(call.kwargs['verify'])
        self.assertFalse(self.c.session.trust_env)

    def test_public_http_rejected_private_and_tailscale_allowed(self):
        for url in ('http://pinepods.example', 'http://8.8.8.8', 'http://169.254.169.254'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                PinepodsClient(url, 'reader', 'password')
        for url in ('https://pinepods.example', 'http://pinepods:8040', 'http://100.64.0.42:8055', 'http://[::1]:8040'):
            PinepodsClient(url, 'reader', 'password').close()

    def test_bad_base_urls_and_username_rejected(self):
        for url in ('https://user:pw@pinepods.example', 'https://pinepods.example/?token=secret', 'https://pinepods.example/path', 'file:///tmp/data'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                PinepodsClient(url, 'reader', 'password')
        for username in ('../reader', 'reader:pw', ' reader', ''):
            with self.subTest(username=username), self.assertRaises(ValueError):
                PinepodsClient('https://pinepods.example', username, 'password')

    def test_username_dots_encoded_without_wrong_namespace(self):
        c = PinepodsClient('https://pinepods.example', 'reader.one', 'password')
        c.session.request = Mock(return_value=response([]))
        c.devices()
        self.assertTrue(c.session.request.call_args.args[1].endswith('/devices/reader.one.json'))

    def test_network_error_never_echoes_secret_url(self):
        self.c.session.request.side_effect = requests.ConnectionError('https://user:secret@host/private')
        with self.assertRaises(RequestError) as e:
            self.c.actions()
        self.assertEqual(str(e.exception), 'PinePods: transport failure')

    def test_redirect_never_followed(self):
        self.c.session.request.return_value = response({'url': 'credential-exfiltration'}, 302)
        with self.assertRaises(RequestError) as e:
            self.c.devices()
        self.assertEqual(e.exception.status, 302)
        self.assertFalse(self.c.session.request.call_args.kwargs['allow_redirects'])

    def test_account_auth_failure_fixed_error(self):
        self.c.session.request.return_value = response({'error': 'secret-and-private-url'}, 401)
        with self.assertRaises(RequestError) as e:
            self.c.devices()
        self.assertNotIn('secret', str(e.exception))

    def test_read_position_and_no_url_in_public_snapshot(self):
        self.actions([action()])
        got = self.c._read_gpodder(FEED, EPISODE, 600)
        self.assertEqual((got['position'], got['status']), (120, 'in_progress'))
        self.assertEqual(got['updated_at'], action_epoch(STAMP))
        self.assertNotIn('feed.example', json.dumps(got))
        self.assertEqual(self.c.session.request.call_args.kwargs['params']['aggregated'], 'false')

    def test_download_or_delete_cannot_erase_latest_play(self):
        for kind in ('download', 'delete'):
            after = dict(podcast=FEED, episode=EPISODE, action=kind, timestamp='2026-11-01T12:01:00')
            self.actions([action(), after])
            self.assertEqual(self.c._read_gpodder(FEED, EPISODE, 600)['position'], 120)

    def test_new_reset_is_not_missing_progress(self):
        self.actions([action(), dict(podcast=FEED, episode=EPISODE, action='new', timestamp='2026-11-01T12:01:00')])
        got = self.c._read_gpodder(FEED, EPISODE, 600)
        self.assertEqual((got['position'], got['status'], got['missing']), (0, 'not_started', False))

    def test_missing_is_not_started(self):
        self.actions([])
        self.assertTrue(self.c._read_gpodder(FEED, EPISODE, 600)['missing'])

    def test_finished_canonical_position_is_duration(self):
        self.actions([action(540)])
        got = self.c._read_gpodder(FEED, EPISODE, 600)
        self.assertEqual((got['position'], got['status']), (600, 'finished'))

    def test_wrong_duration_guid_or_ambiguous_tie_block(self):
        for rows, guid in (([action(total_override=10)], None), ([action(guid='other')], 'this'), ([action(120), action(150)], None)):
            if 'total_override' in rows[0]:
                rows[0]['total'] = rows[0].pop('total_override')
            self.actions(rows)
            with self.subTest(rows=rows), self.assertRaises(RequestError):
                self.c._read_gpodder(FEED, EPISODE, 600, guid)

    def test_feed_and_enclosure_match_exactly(self):
        wrong = action()
        wrong['podcast'] = 'https://feed.example/another.xml'
        self.actions([wrong])
        self.assertTrue(self.c._read_gpodder(FEED, EPISODE, 600)['missing'])

    def test_malformed_inventory_refuses_incomplete_response(self):
        for data in ({'actions': [], 'timestamp': True}, {'actions': None, 'timestamp': 12}, {'actions': [action(position=-2)], 'timestamp': 12}):
            self.c.session.request.return_value = response(data)
            with self.subTest(data=data), self.assertRaises(RequestError):
                self.c.actions()

    def test_body_bound(self):
        r = response({})
        r.content = b'x' * (self.c.MAX_BODY + 1)
        self.c.session.request.return_value = r
        with self.assertRaises(RequestError):
            self.c.actions()

    def test_prepare_whole_seconds_no_download_or_deletion(self):
        got = self.c.prepare_action(FEED, EPISODE, {'position': 120.8, 'status': 'in_progress'}, 600, STAMP)
        self.assertEqual((got['action'], got['position'], got['started'], got['total']), ('play', 120, 0, 600))
        self.assertEqual(got['timestamp'], STAMP)

    def test_near_end_inprogress_and_zero_cannot_be_silently_finished(self):
        for position in (0, 539.9 + 1, 599):
            with self.subTest(position=position), self.assertRaises(RequestError):
                self.c.prepare_action(FEED, EPISODE, {'position': position, 'status': 'in_progress'}, 600, STAMP)

    def test_prepare_completion_and_reset(self):
        done = self.c.prepare_action(FEED, EPISODE, {'position': 120, 'status': 'finished'}, 600, STAMP)
        reset = self.c.prepare_action(FEED, EPISODE, {'position': 0, 'status': 'not_started'}, 600, STAMP)
        self.assertEqual(done['position'], 600)
        self.assertEqual(reset['action'], 'new')
        self.assertNotIn('position', reset)

    def test_reopening_plan_is_two_separate_actions_with_ordered_stamps(self):
        self.c._native_episode = Mock(return_value={'episode_id': 7, 'user_id': 2, 'completed': True})
        plan = self.c.prepare_actions(FEED, EPISODE, {'position': 120, 'status': 'in_progress'}, 600, STAMP, previous={'status': 'finished'})
        self.assertEqual([a['action'] for a in plan], ['new', 'new', 'play'])
        self.assertEqual(plan[0]['_native_action'], 'uncompleted')
        self.assertEqual(action_epoch(plan[1]) - action_epoch(plan[0]), 1)

    def test_write_is_single_post_without_retry(self):
        payload = self.c.prepare_action(FEED, EPISODE, {'position': 120, 'status': 'in_progress'}, 600, STAMP)
        self.c.session.request.side_effect = [response([{'id': self.c.device_id}]), requests.ConnectionError('private')]
        with self.assertRaises(RequestError):
            self.c._write_gpodder(payload)
        self.assertEqual([c.args[0] for c in self.c.session.request.call_args_list], ['GET', 'POST'])
        self.assertEqual(self.c.session.request.call_args.kwargs['json'], [payload])

    def test_write_missing_device_cannot_create_it(self):
        payload = self.c.prepare_action(FEED, EPISODE, {'position': 0, 'status': 'not_started'}, 600, STAMP)
        self.c.session.request.return_value = response([])
        with self.assertRaises(RequestError):
            self.c._write_gpodder(payload)
        self.assertEqual(self.c.session.request.call_count, 1)

    def test_write_rejects_download_delete_and_foreign_device(self):
        good = self.c.prepare_action(FEED, EPISODE, {'position': 0, 'status': 'not_started'}, 600, STAMP)
        for key, value in (('action', 'delete'), ('action', 'download'), ('device', 'some-other-device')):
            bad = dict(good, **{key: value})
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.c._write_gpodder(bad)
        self.c.session.request.assert_not_called()

    def test_acknowledgement_does_not_accept_url_rewrites(self):
        good = self.c.prepare_action(FEED, EPISODE, {'position': 0, 'status': 'not_started'}, 600, STAMP)
        self.c.session.request.side_effect = [response([{'id': self.c.device_id}]), response({'timestamp': 100, 'update_urls': [['a', 'b']]})]
        with self.assertRaises(RequestError):
            self.c._write_gpodder(good)

    def test_timestamp_utc_offsets_and_epoch(self):
        expected = action_epoch(STAMP)
        self.assertEqual(expected, action_epoch('2026-11-01T13:00:00+01:00'))
        self.assertEqual(expected, action_epoch(float(expected) + .3))
        with self.assertRaises(ValueError):
            action_epoch(True)

    def test_close_drops_authentication(self):
        self.c.close()
        self.assertIsNone(self.c.session.auth)

    def test_unverified_native_version_blocks_progress_read_and_write(self):
        payload = self.c.prepare_action(FEED, EPISODE, {'position': 0, 'status': 'not_started'}, 600, STAMP)
        with self.assertRaises(RequestError):
            self.c.read_episode(FEED, EPISODE, 600)
        with self.assertRaises(RequestError):
            self.c.write_action(payload)
        self.c.session.request.assert_not_called()

    def native_client(self):
        c = PinepodsClient('https://pinepods.example', 'reader', 'password', api_key='secret-key', server_version='0.9.0')
        c.session.request = Mock()
        return c

    def test_native_probe_verifies_personal_account_without_creating_key(self):
        c = self.native_client()
        c.session.request.side_effect = [response([{'id': c.device_id}]), response({'timestamp': 1, 'actions': []}),
            response({'status': 'success', 'retrieved_id': 2}), response({'UserID': 2, 'Username': 'reader'}),response(IMPORT_DISABLED)]
        got = c.probe()
        self.assertTrue(got['progress_sync_supported'])
        self.assertEqual(c.identity['native_user_id'], 2)
        for call in c.session.request.call_args_list:
            self.assertEqual(call.args[0], 'GET')
            self.assertNotIn('/get_key', call.args[1])

    def test_native_api_wrong_account_and_webkey_rejected(self):
        for rows in ([response({'status': 'success', 'retrieved_id': 1})],
                     [response({'status': 'success', 'retrieved_id': 2}), response({'UserID': 2, 'Username': 'other'})]):
            c = self.native_client()
            c.session.request.side_effect = rows
            with self.assertRaises(RequestError):
                c._ensure_native()

    def test_native_snapshot_actual_flag_wins_over_log_completion(self):
        c = self.native_client()
        c._native_episode = Mock(return_value={'episode_id': 7, 'user_id': 2, 'completed': False, 'position': 600, 'duration': 600})
        c._read_gpodder = Mock(return_value={'position': 600, 'status': 'finished', 'missing': False,
                                            'revision': 'abc', 'updated_at': 10, 'action': 'play', 'device': c.device_id, 'raw_position': 600})
        got = c.read_episode(FEED, EPISODE, 600)
        self.assertEqual(got['status'], 'in_progress')
        c._native_episode.return_value['completed'] = True
        self.assertEqual(c.read_episode(FEED, EPISODE, 600)['status'], 'finished')

    def test_native_episode_resolves_exact_urls_and_preserves_real_progress(self):
        c = self.native_client()
        c._native_user_id = 2
        c.session.request.side_effect = [
            response({'pods': [{'podcastid': 3, 'feedurl': FEED}]}),
            response({'total': 1, 'episodes': [{'Episodeid': 7, 'Episodeurl': EPISODE}]}),
            response({'episode': {'episodeid': 7, 'feedurl': FEED, 'episodeurl': EPISODE,
                'is_youtube': False, 'completed': True, 'episodeduration': 600, 'listenduration': 600}}),
        ]
        got = c._native_episode(FEED, EPISODE, 600)
        self.assertTrue(got['completed'])
        self.assertEqual(got['position'], 600)
        last = c.session.request.call_args
        self.assertTrue(last.args[1].endswith('/get_episode_metadata'))
        self.assertEqual(last.kwargs['auth'], ())
        self.assertEqual(last.kwargs['headers']['Api-Key'], 'secret-key')

    def test_native_episode_rejects_duplicate_and_truncated_inventory(self):
        for data in ({'total': 2, 'episodes': []},
                     {'total': 2, 'episodes': [{'Episodeid': 7, 'Episodeurl': EPISODE}, {'Episodeid': 8, 'Episodeurl': EPISODE}]}):
            c = self.native_client()
            c._native_user_id = 2
            c.session.request.side_effect = [response({'pods': [{'podcastid': 3, 'feedurl': FEED}]}), response(data)]
            with self.assertRaises(RequestError):
                c._native_episode(FEED, EPISODE, 600)

    def test_native_pagination_matches_090_inner_desc_limit_before_outer_sort(self):
        c = self.native_client()
        c._native_user_id = 2
        pages = []
        def upstream(method, path, **kwargs):
            if path.startswith('/api/data/return_pods/'):
                return {'pods':[{'podcastid':3,'feedurl':FEED}]}
            if path == '/api/data/podcast_episodes':
                p = kwargs['params']; pages.append((p['offset'],p['sort_order']))
                # Exact relevant 0.9.0 SQL shape: internal DESC with expanding
                # offset+limit cap, followed by requested outer order+offset.
                rows = list(range(450,0,-1))[:p['offset']+p['limit']]
                rows.sort(reverse=p['sort_order']=='desc')
                rows = rows[p['offset']:p['offset']+p['limit']]
                return {'total':450,'episodes':[{'Episodeid':ident,
                    'Episodeurl':EPISODE if ident==1 else 'https://media.example/'+str(ident)} for ident in rows]}
            if path == '/api/data/get_episode_metadata':
                return {'episode':{'episodeid':1,'feedurl':FEED,'episodeurl':EPISODE,
                    'is_youtube':False,'completed':False,'episodeduration':600,'listenduration':None}}
            raise AssertionError('Unexpected endpoint')
        c._request = upstream
        self.assertEqual(c._native_episode(FEED,EPISODE,600)['episode_id'],1)
        self.assertEqual(pages,[(0,'desc'),(200,'desc'),(400,'desc')])

    def test_empty_historical_url_does_not_block_unique_verified_target(self):
        c=self.native_client();c._native_user_id=2
        c.session.request.side_effect=[response({'pods':[{'podcastid':3,'feedurl':FEED}]}),
            response({'total':2,'episodes':[{'Episodeid':6,'Episodeurl':''},{'Episodeid':7,'Episodeurl':EPISODE}]}),
            response({'episode':{'episodeid':7,'feedurl':FEED,'episodeurl':EPISODE,
                'is_youtube':False,'completed':False,'episodeduration':600,'listenduration':120}})]
        result=c._native_episode(FEED,EPISODE,600)
        self.assertEqual(result['episode_id'],7)
        self.assertEqual(result['position'],120)
        self.assertEqual(c.session.request.call_count,3)
        self.assertEqual(c.session.request.call_args.kwargs['json']['episode_id'],7)

    def test_empty_historical_url_never_proves_target_absence(self):
        c=self.native_client();c._native_user_id=2
        c.session.request.side_effect=[response({'pods':[{'podcastid':3,'feedurl':FEED}]}),
            response({'total':1,'episodes':[{'Episodeid':6,'Episodeurl':''}]})]
        with self.assertRaises(EpisodeUnavailable) as caught:c._native_episode(FEED,EPISODE,600)
        self.assertEqual(caught.exception.reason,'episode_identity_unresolved')
        self.assertEqual(c._native_ids,{})
        self.assertEqual(c.session.request.call_count,2)

    def test_unresolved_empty_url_requires_complete_unique_valid_remaining_inventory(self):
        empty={'Episodeid':6,'Episodeurl':''}
        cases=[
            [{'total':2,'episodes':[empty,empty]}],
            [{'total':2,'episodes':[empty]},{'total':3,'episodes':[{'Episodeid':7,'Episodeurl':'https://media.example/other'}]}],
            [{'total':2,'episodes':[empty]},{'total':2,'episodes':[]}],
            [{'total':2,'episodes':[empty,{'Episodeid':7,'Episodeurl':None}]}],
        ]
        for pages in cases:
            with self.subTest(pages=pages):
                c=self.native_client();c._native_user_id=2
                c.session.request.side_effect=[response({'pods':[{'podcastid':3,'feedurl':FEED}]})]+[response(p) for p in pages]
                with self.assertRaises(RequestError) as caught:c._native_episode(FEED,EPISODE,600)
                self.assertNotIsInstance(caught.exception,EpisodeUnavailable)
                self.assertEqual(c._native_ids,{})

    def test_unresolved_empty_url_can_later_resolve_only_exact_target(self):
        c=self.native_client();c._native_user_id=2
        pods=response({'pods':[{'podcastid':3,'feedurl':FEED}]})
        c.session.request.side_effect=[pods,response({'total':1,'episodes':[{'Episodeid':6,'Episodeurl':''}]}),
            pods,response({'total':2,'episodes':[{'Episodeid':6,'Episodeurl':''},{'Episodeid':7,'Episodeurl':EPISODE}]}),
            response({'episode':{'episodeid':7,'feedurl':FEED,'episodeurl':EPISODE,
                'is_youtube':False,'completed':False,'episodeduration':600,'listenduration':120}})]
        with self.assertRaises(EpisodeUnavailable):c._native_episode(FEED,EPISODE,600)
        self.assertEqual(c._native_ids,{})
        self.assertEqual(c._native_episode(FEED,EPISODE,600)['episode_id'],7)

    def test_latest_valid_reset_or_play_supersedes_old_duration_only(self):
        old=action(726);old['total']=726
        for new in (dict(podcast=FEED,episode=EPISODE,action='new',timestamp='2026-11-01T12:01:00'),
                    dict(action(40),timestamp='2026-11-01T12:01:00')):
            with self.subTest(action=new['action']):
                self.actions([old,new])
                result=self.c._read_gpodder(FEED,EPISODE,600)
                self.assertEqual(result['position'],0 if new['action']=='new' else 40)
        # A later download does not supersede the incompatible listening action.
        self.actions([old,dict(podcast=FEED,episode=EPISODE,action='download',timestamp='2026-11-01T12:01:00')])
        with self.assertRaises(RequestError):self.c._read_gpodder(FEED,EPISODE,600)

    def test_historical_completion_requires_verified_matching_native_end(self):
        c=self.native_client();c._native_user_id=2;c._native_ids[(FEED,EPISODE)]=7
        old=action(3615);old['total']=3615
        c.session.request.side_effect=[response({'episode':{'episodeid':7,'feedurl':FEED,'episodeurl':EPISODE,
            'is_youtube':False,'completed':True,'episodeduration':3489,'listenduration':3615}}),
            response({'timestamp':1793534400,'actions':[old]})]
        snapshot=c.read_episode(FEED,EPISODE,3489.554286)
        self.assertEqual((snapshot['status'],snapshot['gpodder_status']),('finished','finished'))
        self.assertEqual(snapshot['position'],3489.554286)
        self.assertEqual(snapshot['gpodder_position'],3489.554286)
        self.assertEqual(snapshot['native_position'],3615)
        self.assertEqual(snapshot['gpodder_raw_position'],3615)
        # Canonical display does not claim a fresh native or gpodder write landed.
        snapshot['device']=c.device_id
        payload=c.prepare_action(FEED,EPISODE,{'status':'finished','position':3489.554286},3489.554286,STAMP)
        self.assertFalse(c.action_confirmed(snapshot,payload,3489.554286))
        self.assertFalse(c.action_confirmed(snapshot,dict(_native_action='completed',_episode_id=7,_user_id=2),3489.554286))
        self.assertEqual([x.args[0] for x in c.session.request.call_args_list],['POST','GET'])
        self.assertTrue(c.session.request.call_args_list[0].args[1].endswith('/get_episode_metadata'))

    def test_historical_completion_not_accepted_without_both_exact_ends(self):
        old=action(726);old['total']=726
        contexts=[None,dict(completed=False,position=726,duration=600),
            dict(completed=True,position=None,duration=600),dict(completed=True,position=725,duration=600),
            dict(completed=True,position=726,duration=610),dict(completed=True,position=726,duration=0)]
        for context in contexts:
            with self.subTest(context=context):
                self.actions([old])
                with self.assertRaises(RequestError):self.c._read_gpodder(FEED,EPISODE,600,verified_native=context)
        for position in (700,725,727):
            self.actions([dict(old,position=position)])
            with self.assertRaises(RequestError):self.c._read_gpodder(FEED,EPISODE,600,
                verified_native=dict(completed=True,position=position,duration=600))

    def test_historical_completion_cannot_mask_conflicting_latest_tie(self):
        old=action(726);old['total']=726
        context=dict(completed=True,position=726,duration=600)
        reset=dict(podcast=FEED,episode=EPISODE,action='new',timestamp=STAMP)
        for other in (reset,dict(old,position=400),action(120)):
            self.actions([old,other])
            with self.assertRaises(RequestError):self.c._read_gpodder(FEED,EPISODE,600,verified_native=context)
        self.actions([old,dict(old,device='other-phone')])
        result=self.c._read_gpodder(FEED,EPISODE,600,verified_native=context)
        self.assertEqual((result['status'],result['position'],result['raw_position']),('finished',600,726))

    def test_completed_receipt_never_bypasses_current_media_identity_or_duration(self):
        for update in ({'episodeurl':'https://media.example/different'}, {'episodeduration':3371}):
            c=self.native_client();c._native_user_id=2;c._native_ids[(FEED,EPISODE)]=7
            row={'episodeid':7,'feedurl':FEED,'episodeurl':EPISODE,'is_youtube':False,
                'completed':True,'episodeduration':3497,'listenduration':3497}
            row.update(update);c.session.request.return_value=response({'episode':row})
            with self.assertRaises(EpisodeUnavailable):c.read_episode(FEED,EPISODE,3497.96714)
            self.assertEqual(c.session.request.call_count,1)

    def test_only_empty_string_exception_not_other_invalid_foreign_urls(self):
        for url in (None,17,'https://media.example/file#fragment','not a URL','https://user:pw@media.example/file'):
            with self.subTest(url=url):
                c=self.native_client();c._native_user_id=2
                c.session.request.side_effect=[response({'pods':[{'podcastid':3,'feedurl':FEED}]}),
                    response({'total':2,'episodes':[{'Episodeid':6,'Episodeurl':url},{'Episodeid':7,'Episodeurl':EPISODE}]})]
                with self.assertRaises(RequestError):c._native_episode(FEED,EPISODE,600)
                self.assertEqual(c._native_ids,{})

    def test_empty_row_cannot_hide_duplicate_target_or_incomplete_inventory(self):
        empty={'Episodeid':6,'Episodeurl':''};target={'Episodeid':7,'Episodeurl':EPISODE}
        cases=[({'total':3,'episodes':[empty,target,dict(target,Episodeid=8)]},EpisodeUnavailable),
            ({'total':3,'episodes':[empty,target,target]},RequestError),
            ({'total':3,'episodes':[empty,target]},RequestError)]
        for data,error in cases:
            c=self.native_client();c._native_user_id=2
            c.session.request.side_effect=[response({'pods':[{'podcastid':3,'feedurl':FEED}]}),
                response(data),response({'total':3,'episodes':[]})]
            with self.assertRaises(error):c._native_episode(FEED,EPISODE,600)
            self.assertEqual(c._native_ids,{})

    def test_empty_foreign_row_still_requires_exact_final_metadata(self):
        c=self.native_client();c._native_user_id=2
        c.session.request.side_effect=[response({'pods':[{'podcastid':3,'feedurl':FEED}]}),
            response({'total':2,'episodes':[{'Episodeid':6,'Episodeurl':''},{'Episodeid':7,'Episodeurl':EPISODE}]}),
            response({'episode':{'episodeid':7,'feedurl':FEED,'episodeurl':'https://media.example/other',
                'is_youtube':False,'completed':False,'episodeduration':600,'listenduration':120}})]
        with self.assertRaises(EpisodeUnavailable) as caught:c._native_episode(FEED,EPISODE,600)
        self.assertEqual(caught.exception.reason,'media_identity_mismatch')

    def test_typed_media_absence_requires_valid_inventory(self):
        for pods,reason in [([], 'feed_missing'),
            ([{'podcastid':3,'feedurl':FEED},{'podcastid':4,'feedurl':FEED}], 'feed_ambiguous')]:
            c=self.native_client();c._native_user_id=2
            c.session.request.return_value=response({'pods':pods})
            with self.assertRaises(EpisodeUnavailable) as caught: c._native_episode(FEED,EPISODE,600)
            self.assertEqual(caught.exception.reason,reason)
        for invalid in ({}, {'pods':None}, {'pods':[{'podcastid':'3','feedurl':FEED}]},
                        {'pods':[{'podcastid':3}]}, {'pods':[{'podcastid':3,'feedurl':FEED}]*2}):
            c=self.native_client();c._native_user_id=2
            c.session.request.return_value=response(invalid)
            with self.assertRaises(RequestError) as caught: c._native_episode(FEED,EPISODE,600)
            self.assertNotIsInstance(caught.exception,EpisodeUnavailable)

    def test_typed_episode_missing_and_ambiguous_not_incomplete(self):
        for data,reason in [({'total':0,'episodes':[]},'episode_missing'),
            ({'total':2,'episodes':[{'Episodeid':7,'Episodeurl':EPISODE},{'Episodeid':8,'Episodeurl':EPISODE}]},'episode_ambiguous')]:
            c=self.native_client();c._native_user_id=2
            c.session.request.side_effect=[response({'pods':[{'podcastid':3,'feedurl':FEED}]}),response(data)]
            with self.assertRaises(EpisodeUnavailable) as caught:c._native_episode(FEED,EPISODE,600)
            self.assertEqual(caught.exception.reason,reason)
        for data in ({'total':1,'episodes':[]},{'total':1,'episodes':[{'Episodeid':7}]},
                     {'total':2,'episodes':[{'Episodeid':7,'Episodeurl':EPISODE}]*2}):
            c=self.native_client();c._native_user_id=2
            c.session.request.side_effect=[response({'pods':[{'podcastid':3,'feedurl':FEED}]}),response(data)]
            with self.assertRaises(RequestError) as caught:c._native_episode(FEED,EPISODE,600)
            self.assertNotIsInstance(caught.exception,EpisodeUnavailable)

    def test_typed_duration_and_identity_mismatch_after_valid_metadata(self):
        base={'episodeid':7,'feedurl':FEED,'episodeurl':EPISODE,'is_youtube':False,
              'completed':False,'episodeduration':600,'listenduration':120}
        for change,reason in [({'episodeduration':0,'listenduration':None},'duration_mismatch'),
                              ({'episodeduration':625},'duration_mismatch'),
                              ({'feedurl':'https://another.example/feed'},'media_identity_mismatch'),
                              ({'episodeid':8},'media_identity_mismatch'),
                              ({'is_youtube':True},'media_identity_mismatch')]:
            c=self.native_client();c._native_user_id=2;c._native_ids[(FEED,EPISODE)]=7
            c.session.request.return_value=response({'episode':dict(base,**change)})
            with self.assertRaises(EpisodeUnavailable) as caught:c._native_episode(FEED,EPISODE,600)
            self.assertEqual(caught.exception.reason,reason)

    def test_invalid_native_progress_never_becomes_optional_media_wait(self):
        base={'episodeid':7,'feedurl':FEED,'episodeurl':EPISODE,'is_youtube':False,
              'completed':False,'episodeduration':600,'listenduration':120}
        for change in ({'listenduration':-1},{'listenduration':float('nan')},{'listenduration':True},
                       {'listenduration':700},{'episodeduration':'625'},{'completed':'false'},
                       {'is_youtube':None},{'episodeid':True},{'episodeurl':None}):
            c=self.native_client();c._native_user_id=2;c._native_ids[(FEED,EPISODE)]=7
            c.session.request.return_value=response({'episode':dict(base,**change)})
            with self.assertRaises(RequestError) as caught:c._native_episode(FEED,EPISODE,600)
            self.assertNotIsInstance(caught.exception,EpisodeUnavailable)

    def test_explicit_native_completion_keeps_old_position_but_reads_current_end(self):
        c=self.native_client();c._native_user_id=2;c._native_ids[(FEED,EPISODE)]=7
        c.session.request.return_value=response({'episode':{'episodeid':7,'feedurl':FEED,'episodeurl':EPISODE,
            'is_youtube':False,'completed':True,'episodeduration':3489,'listenduration':3615}})
        c._read_gpodder=Mock(return_value={'missing':True,'revision':'missing','updated_at':None,
            'action':None,'device':None,'position':0,'status':'not_started','raw_position':None})
        result=c.read_episode(FEED,EPISODE,3489.554286)
        self.assertEqual(result['status'],'finished')
        self.assertEqual(result['position'],3489.554286)
        self.assertEqual(result['native_position'],3615)
        self.assertTrue(result['native_completed'])
        self.assertEqual(c.session.request.call_count,1)
        self.assertTrue(c.session.request.call_args.args[1].endswith('/get_episode_metadata'))
        self.assertFalse(c.action_confirmed(result,{'_native_action':'completed','_episode_id':7,'_user_id':2},3489.554286))

    def test_unfinished_position_overrun_still_blocks(self):
        for position in (3489+3,3615):
            c=self.native_client();c._native_user_id=2;c._native_ids[(FEED,EPISODE)]=7
            c.session.request.return_value=response({'episode':{'episodeid':7,'feedurl':FEED,'episodeurl':EPISODE,
                'is_youtube':False,'completed':False,'episodeduration':3489,'listenduration':position}})
            with self.assertRaises(RequestError) as caught:c._native_episode(FEED,EPISODE,3489.554286)
            self.assertNotIsInstance(caught.exception,EpisodeUnavailable)

    def test_completed_position_still_requires_valid_finite_nonnegative_number(self):
        for position in (-1,float('nan'),float('inf'),True,'3615'):
            c=self.native_client();c._native_user_id=2;c._native_ids[(FEED,EPISODE)]=7
            c.session.request.return_value=response({'episode':{'episodeid':7,'feedurl':FEED,'episodeurl':EPISODE,
                'is_youtube':False,'completed':True,'episodeduration':3489,'listenduration':position}})
            with self.assertRaises(RequestError) as caught:c._native_episode(FEED,EPISODE,3489.554286)
            self.assertNotIsInstance(caught.exception,EpisodeUnavailable)

    def test_completed_overrun_does_not_bypass_media_duration_mismatch(self):
        c=self.native_client();c._native_user_id=2;c._native_ids[(FEED,EPISODE)]=7
        c.session.request.return_value=response({'episode':{'episodeid':7,'feedurl':FEED,'episodeurl':EPISODE,
            'is_youtube':False,'completed':True,'episodeduration':3371,'listenduration':3497}})
        with self.assertRaises(EpisodeUnavailable) as caught:c._native_episode(FEED,EPISODE,3497.96714)
        self.assertEqual(caught.exception.reason,'duration_mismatch')

    def test_episode_unavailable_reason_is_closed_and_secret_safe(self):
        with self.assertRaisesRegex(ValueError,'Invalid PinePods media incompatibility reason'):
            EpisodeUnavailable(FEED)
        self.assertIsNone(EpisodeUnavailable('feed_missing').status)

    def test_finished_plan_uses_separate_play_then_real_completion(self):
        c = self.native_client()
        c._native_episode = Mock(return_value={'episode_id': 7, 'user_id': 2, 'completed': False})
        plan = c.prepare_actions(FEED, EPISODE, {'position': 600, 'status': 'finished'}, 600, STAMP)
        self.assertNotIn('_native_action', plan[0])
        self.assertEqual(plan[1]['_native_action'], 'completed')
        self.assertEqual(action_epoch(plan[1]) - action_epoch(plan[0]), 1)

    def test_native_write_is_one_mutation_and_only_after_identity_proof(self):
        c = self.native_client()
        c._native_user_id = 2
        c._native_ids[(FEED, EPISODE)] = 7
        step = {'podcast': FEED, 'episode': EPISODE, 'device': c.device_id, 'timestamp': STAMP,
                'action': 'new', '_native_action': 'uncompleted', '_episode_id': 7, '_user_id': 2}
        c.session.request.side_effect = [response(IMPORT_DISABLED),response({'detail': 'Episode marked as uncompleted.'})]
        c.write_action(step)
        self.assertEqual(c.session.request.call_count, 2)
        self.assertEqual(c.session.request.call_args_list[0].args[0],'GET')
        self.assertTrue(c.session.request.call_args.args[1].endswith('/mark_episode_uncompleted'))
        c._native_ids.clear()
        with self.assertRaises(RequestError):
            c.write_action(step)
        self.assertEqual(c.session.request.call_count, 2)

    def test_native_import_guard_rejects_active_missing_or_external_status(self):
        for value in [None,{},dict(IMPORT_DISABLED,external_url='http://localhost:8042'),
                      dict(IMPORT_DISABLED,sync_type='both'),dict(IMPORT_DISABLED,gpodder_enabled=False),
                      dict(IMPORT_DISABLED,external_enabled=True),dict(IMPORT_DISABLED,external_url=''),
                      {k:v for k,v in IMPORT_DISABLED.items() if k!='external_url'}]:
            with self.subTest(value=value):
                c=self.native_client();c.session.request.return_value=response(value)
                with self.assertRaisesRegex(RequestError,'native background import must be disabled'):
                    c._verify_native_import_disabled()

    def test_native_import_setting_is_never_cached_between_probes(self):
        c=self.native_client();c._native_user_id=2
        def rows(status):return [response([]),response({'timestamp':1,'actions':[]}),response(status)]
        c.session.request.side_effect=rows(IMPORT_DISABLED)+rows(dict(IMPORT_DISABLED,external_url='http://localhost:8042'))
        self.assertTrue(c.probe()['native_import_disabled'])
        with self.assertRaises(RequestError):c.probe()
        self.assertEqual(sum(call.args[1].endswith('/gpodder/status') for call in c.session.request.call_args_list),2)

    def test_import_reactivation_blocks_both_write_types_without_post(self):
        for native in (False,True):
            with self.subTest(native=native):
                c=self.native_client();c._native_user_id=2;c._native_ids[(FEED,EPISODE)]=7
                step=c.prepare_action(FEED,EPISODE,{'position':120,'status':'in_progress'},600,STAMP)
                if native:
                    step={'podcast':FEED,'episode':EPISODE,'device':c.device_id,'timestamp':STAMP,
                        'action':'new','_native_action':'uncompleted','_episode_id':7,'_user_id':2}
                c.session.request.return_value=response(dict(IMPORT_DISABLED,external_url='http://localhost:8042'))
                with self.assertRaises(RequestError):c.write_action(step)
                self.assertTrue(all(call.args[0]=='GET' for call in c.session.request.call_args_list))

    def test_echo_alone_never_confirms_native_completion_or_reset(self):
        c = self.native_client()
        native = {'_native_action': 'completed', '_episode_id': 7, '_user_id': 2}
        echo = {'status': 'finished', 'position': 600, 'native_episode_id': 7, 'native_user_id': 2, 'native_completed': False}
        self.assertFalse(c.action_confirmed(echo, native, 600))
        echo['native_completed'] = True
        self.assertFalse(c.action_confirmed(echo, native, 600))
        echo['native_position'] = 120
        self.assertFalse(c.action_confirmed(echo, native, 600))
        echo['native_position'] = 600
        self.assertTrue(c.action_confirmed(echo, native, 600))

    def test_go_25000_action_cap_cannot_be_mistaken_for_full_inventory(self):
        self.c.session.request.return_value = response({'timestamp': 100, 'actions': [action()] * 25000})
        with self.assertRaises(RequestError):
            self.c.actions()

    def test_server_timestamp_receipt_rejects_later_same_position(self):
        c = self.native_client()
        payload = c.prepare_action(FEED, EPISODE, {'position': 120, 'status': 'in_progress'}, 600, STAMP)
        payload['_server_timestamp'] = action_epoch(STAMP) + 5
        snapshot = {'action': 'play', 'device': c.device_id, 'updated_at': payload['_server_timestamp'],
                    'native_position': 120, 'gpodder_position': 120}
        self.assertTrue(c.action_confirmed(snapshot, payload, 600))
        snapshot['updated_at'] += 1
        self.assertFalse(c.action_confirmed(snapshot, payload, 600))

    def repair_responses(self, metadata):
        return [response({'status':'success','retrieved_id':2}),
                response({'UserID':2,'Username':'reader'}),
                response({'pods':[{'podcastid':11,'feedurl':FEED}]}),
                response({'total':1,'episodes':[{'Episodeid':7,'Episodeurl':EPISODE}]}),
                response({'episode':metadata})]

    def repair_metadata(self):
        return dict(episodeid=7,podcastid=11,feedurl=FEED,episodeurl=EPISODE,
                    is_youtube=False,is_downloaded=False,completed=True,
                    episodeduration=480,listenduration=480)

    def test_repair_inspection_reproves_ownership_and_keeps_normal_guard_strict(self):
        c=self.native_client();c._native_user_id=2;c._native_ids[(FEED,EPISODE)]=999
        row=self.repair_metadata()
        c.session.request.side_effect=self.repair_responses(row)
        got=c.inspect_episode_metadata(FEED,EPISODE,600)
        self.assertEqual(got['episode_id'],7)
        self.assertEqual(got['user_id'],2)
        self.assertEqual(got['duration'],480)
        self.assertEqual(got['position'],480)
        self.assertTrue(got['completed'])
        self.assertEqual(got['metadata'],row)
        self.assertEqual(c.session.request.call_count,5)
        c.session.request.side_effect=[response({'episode':row})]
        with self.assertRaises(EpisodeUnavailable) as exc:
            c._native_episode(FEED,EPISODE,600)
        self.assertEqual(exc.exception.reason,'duration_mismatch')

    def test_repair_inspection_cannot_replace_the_verified_personal_account(self):
        c=self.native_client();c._native_user_id=99
        for _ in range(2):
            c.session.request.side_effect=self.repair_responses(self.repair_metadata())
            with self.assertRaisesRegex(RequestError,'personal native account changed'):
                c.inspect_episode_metadata(FEED,EPISODE,600)
            self.assertEqual(c._native_ids,{})
            self.assertEqual(c._native_user_id,99)
        self.assertFalse(any(call.args[1].endswith('/update_episode_duration') for call in c.session.request.call_args_list))

    def test_repair_inspection_error_after_user_probe_restores_original_pin(self):
        for original in (None,99):
            c=self.native_client();c._native_user_id=original
            c.session.request.side_effect=self.repair_responses(dict(self.repair_metadata(),is_downloaded=None))
            with self.assertRaises(RequestError):c.inspect_episode_metadata(FEED,EPISODE,600)
            self.assertEqual(c._native_user_id,original)
            self.assertEqual(c._native_ids,{})

    def test_repair_inspection_requires_download_state_and_valid_raw_values(self):
        for changes in ({'is_downloaded':None},{'podcastid':True},{'podcastid':12},{'listenduration':-1},
                        {'listenduration':float('nan')},{'episodeduration':True}):
            c=self.native_client()
            c.session.request.side_effect=self.repair_responses(dict(self.repair_metadata(),**changes))
            with self.subTest(changes=changes),self.assertRaises(RequestError):
                c.inspect_episode_metadata(FEED,EPISODE,600)

    def test_duration_write_fresh_compare_single_post_exact_payload(self):
        c=self.native_client()
        c.session.request.side_effect=self.repair_responses(self.repair_metadata())
        before=c.inspect_episode_metadata(FEED,EPISODE,600)
        c.session.request.reset_mock()
        c.session.request.side_effect=self.repair_responses(self.repair_metadata())+[
            response(IMPORT_DISABLED),response({'detail':'Episode duration updated to 600'})]
        self.assertEqual(c.write_episode_duration(before,600),{'accepted':True})
        mutations=[call for call in c.session.request.call_args_list if call.args[1].endswith('/update_episode_duration')]
        self.assertEqual(len(mutations),1)
        self.assertEqual(mutations[0].kwargs['json'],{'episode_id':7,'new_duration':600,'is_youtube':False})
        self.assertFalse(any('mark_episode_' in call.args[1] for call in c.session.request.call_args_list))

    def test_duration_write_changed_active_or_downloaded_never_posts(self):
        for changes in ({'episodeduration':481},{'is_downloaded':True},
                        {'completed':False,'listenduration':12}):
            c=self.native_client();row=self.repair_metadata()
            c.session.request.side_effect=self.repair_responses(row)
            before=c.inspect_episode_metadata(FEED,EPISODE,600)
            c.session.request.reset_mock()
            c.session.request.side_effect=self.repair_responses(dict(row,**changes))
            with self.subTest(changes=changes),self.assertRaises(RequestError):
                c.write_episode_duration(before,600)
            self.assertFalse(any(call.args[1].endswith('/update_episode_duration') for call in c.session.request.call_args_list))

    def test_duration_write_transport_is_never_retried_and_wrong_ack_is_error(self):
        for answer in (requests.ConnectionError('secret-url'), response({'detail':'different'})):
            c=self.native_client();row=self.repair_metadata()
            c.session.request.side_effect=self.repair_responses(row)
            before=c.inspect_episode_metadata(FEED,EPISODE,600)
            c.session.request.reset_mock()
            c.session.request.side_effect=self.repair_responses(row)+[response(IMPORT_DISABLED),answer]
            with self.assertRaises(RequestError):c.write_episode_duration(before,600)
            self.assertEqual(sum(call.args[1].endswith('/update_episode_duration') for call in c.session.request.call_args_list),1)


if __name__ == '__main__':
    unittest.main()
