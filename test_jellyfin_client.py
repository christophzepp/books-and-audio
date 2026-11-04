import copy
from datetime import datetime, timezone
import unittest

from jellyfin_client import JellyfinClient, utc_timestamp


class TimestampTests(unittest.TestCase):
    def test_whole_second_utc_uses_real_z_instead_of_local_offset_kind(self):
        self.assertEqual(utc_timestamp('2026-11-01T14:42:45+00:00'),'2026-11-01T14:42:45.0000000Z')
        self.assertEqual(utc_timestamp('2026-11-01T14:42:45.000000+00:00'),'2026-11-01T14:42:45.0000000Z')

    def test_explicit_offsets_preserve_instant_in_summer_winter_and_date_boundary(self):
        for incoming,expected in (
            ('2026-11-01T15:42:45+01:00','2026-11-01T14:42:45.0000000Z'),
            ('2026-07-01T16:42:45+02:00','2026-07-01T14:42:45.0000000Z'),
            ('2026-11-01T00:15:00+05:30','2026-10-31T18:45:00.0000000Z'),
            ('2026-11-01T23:00:00-02:00','2026-11-02T01:00:00.0000000Z')):
            with self.subTest(value=incoming):
                self.assertEqual(utc_timestamp(incoming),expected)
                self.assertEqual(datetime.fromisoformat(incoming).astimezone(timezone.utc),datetime.fromisoformat(expected.replace('Z','+00:00')))

    def test_existing_fractional_precision_is_preserved_including_100ns(self):
        self.assertEqual(utc_timestamp('2026-11-01T15:42:45.1234567+01:00'),'2026-11-01T14:42:45.1234567Z')
        self.assertEqual(utc_timestamp('2026-11-01T14:42:45.0000001Z'),'2026-11-01T14:42:45.0000001Z')
        self.assertEqual(utc_timestamp('2026-11-01T14:42:45.1Z'),'2026-11-01T14:42:45.1000000Z')

    def test_update_normalizes_wire_body_without_mutating_caller_or_other_fields(self):
        client=JellyfinClient('http://jellyfin','dummy','a'*32,'reader','12.4.0')
        self.addCleanup(client.s.close)
        calls=[];client.request=lambda *args:calls.append(args)
        values={'LastPlayedDate':'2026-11-01T14:42:45+00:00','PlaybackPositionTicks':43097469390,'Played':True,'PlayCount':1}
        original=copy.deepcopy(values)
        client.update('b'*32,values)
        self.assertEqual(values,original)
        method,path,params,body=calls[0]
        self.assertEqual(method,'POST');self.assertEqual(path,'/UserItems/'+'b'*32+'/UserData')
        self.assertEqual(params,{'userId':'a'*32})
        self.assertEqual(body,dict(original,LastPlayedDate='2026-11-01T14:42:45.0000000Z'))

    def test_invalid_or_naive_dates_are_rejected_before_any_request(self):
        client=JellyfinClient('http://jellyfin','dummy','a'*32,'reader','12.4.0')
        self.addCleanup(client.s.close)
        calls=[];client.request=lambda *args:calls.append(args)
        for value in ('2026-11-01T14:42:45','bad','2026-13-01T14:42:45Z',123,'2026-11-01T14:42:45.12345678Z',
                      '2026-11-01T14:42:45+01:60','2026-11-01T14:42:45-01:60','2026-11-01T14:42:45+24:00',
                      '0001-01-01T00:00:00+01:00','9999-12-31T23:59:59-01:00'):
            with self.subTest(value=value),self.assertRaises(ValueError):
                client.update('b'*32,{'LastPlayedDate':value})
        self.assertFalse(calls)

    def test_null_or_omitted_timestamp_retains_existing_client_behavior(self):
        client=JellyfinClient('http://jellyfin','dummy','a'*32,'reader','12.4.0')
        self.addCleanup(client.s.close)
        calls=[];client.request=lambda *args:calls.append(args)
        for value in ({'LastPlayedDate':None},{'Played':False}):
            client.update('b'*32,value)
            self.assertEqual(calls[-1][3],value)


if __name__=='__main__':unittest.main()
