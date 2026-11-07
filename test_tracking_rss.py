import unittest
from tracking_rss import parse_feed,TrackingResolver
F='https://feed.example.test/rss';U='https://media.example.test/new.mp3'
class ResolverTests(unittest.TestCase):
 def resolver(self,xml,aliases=()):
  r=TrackingResolver(aliases);r.cache[F]=parse_feed(xml.encode());return r
 def test_exact_current_enclosure_carries_stable_non_url_guid_for_stock_floppy(self):
  r=self.resolver('<rss><channel><item><guid>opaque-guid</guid><enclosure url="'+U+'"/></item></channel></rss>')
  result=r.resolve({'podcast':F,'episode':U,'timestamp':'2026-11-07T10:00:00','position':100,'total':100})
  self.assertEqual(result['url'],'opaque-guid');self.assertEqual(result['episode'],U)
 def test_verified_guid_recovers_rotated_enclosure_without_title_matching(self):
  r=self.resolver('<rss><channel><item><guid>id</guid><enclosure url="'+U+'"/></item></channel></rss>')
  self.assertEqual(r.resolve({'podcast':F,'episode':'https://media.example.test/old.mp3','guid':'id'})['episode'],U)
 def test_explicit_alias_uses_exact_same_feed(self):
  old='https://media.example.test/old.mp3';xml='<rss><channel><item><guid>id</guid><enclosure url="'+U+'"/></item></channel></rss>'
  r=self.resolver(xml,[{'feed':F,'old':old,'new':U,'guid':'id'}]);self.assertEqual(r.resolve({'podcast':F,'episode':old})['episode'],U)
  with self.assertRaises(ValueError):self.resolver(xml).resolve({'podcast':F,'episode':old})
 def test_duplicate_guid_is_not_guessed(self):
  r=self.resolver('<rss><channel><item><guid>id</guid><enclosure url="'+U+'"/></item><item><guid>id</guid><enclosure url="https://media.example.test/b.mp3"/></item></channel></rss>')
  with self.assertRaises(ValueError):r.resolve({'podcast':F,'episode':U})
 def test_unknown_episode_is_deferred_instead_of_creating_unknown_metadata(self):
  r=self.resolver('<rss><channel><item><title>same title</title><guid>id</guid><enclosure url="'+U+'"/></item></channel></rss>')
  with self.assertRaises(ValueError):r.resolve({'podcast':F,'episode':'https://media.example.test/else.mp3'})
 def test_untrusted_xml_entities_are_rejected(self):
  with self.assertRaises(ValueError):parse_feed(b'<!DOCTYPE rss [<!ENTITY a "x">]><rss/>')
 def test_atom_enclosures_are_supported(self):
  self.assertEqual(parse_feed(('<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>urn:one</id><link rel="enclosure" href="'+U+'"/></entry></feed>').encode()),[{'episode':U,'guid':'urn:one'}])
if __name__=='__main__':unittest.main()
