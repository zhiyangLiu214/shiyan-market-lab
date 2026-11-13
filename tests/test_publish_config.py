import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import yeren_site


class PublishConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.object(yeren_site,'ROOT',self.root).start()
        patch.dict(os.environ,{'SHIYAN_BASE_URL':'','SHIYAN_NETRC':str(self.root/'absent.netrc')}).start()

    def test_default_is_local_and_has_no_credentials(self):
        url,headers=yeren_site.site_access()
        self.assertEqual(url,'http://127.0.0.1:8766/api/terminal/notes')
        self.assertNotIn('Authorization',headers)

    def test_private_config_preserves_explicit_site_and_matching_auth(self):
        (self.root/'.runtime').mkdir()
        credentials=self.root/'private.netrc';credentials.write_text('machine example.com login test password example-only\n');credentials.chmod(0o600)
        (self.root/'.runtime/notes-publish.json').write_text(json.dumps(dict(base_url='https://example.com',netrc_path=str(credentials))))
        os.environ.pop('SHIYAN_NETRC')
        url,headers=yeren_site.site_access()
        self.assertEqual(url,'https://example.com/api/terminal/notes')
        self.assertTrue(headers['Authorization'].startswith('Basic '))

    def test_remote_without_matching_credentials_is_rejected(self):
        os.environ['SHIYAN_BASE_URL']='https://example.com'
        with self.assertRaises(ValueError):yeren_site.site_access()

    def test_embedded_credentials_and_query_are_rejected(self):
        for value in ('https://user:secret@example.com','https://example.com?token=example','file:///tmp/site'):
            os.environ['SHIYAN_BASE_URL']=value
            with self.assertRaises(ValueError):yeren_site.site_access()


if __name__=='__main__':unittest.main()
