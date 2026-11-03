import hashlib
import io
import os
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0,os.path.dirname(os.path.dirname(__file__)))
import archive
from app import create_app

class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ,{
            'DATABASE_PATH':self.tmp.name+'/archive.sqlite',
            'WORKSPACE_DOMAIN':'example.com','MAILBOXES':'alice@example.com,bob@example.com',
            'SECRET_KEY':'s'*40,'S3_BUCKET':'test','PUBLIC_URL':'https://archive.example.com'})
        self.env.start()
        self.app = create_app()
        self.client = self.app.test_client()
        self.raw = b'From: sender@example.com\r\nTo: alice@example.com\r\nSubject: Budget\r\nContent-Type: text/plain\r\n\r\nConfidential forecast'
        self.db = archive.connect()
        for owner in ['alice@example.com','bob@example.com']:
            mid = 'alice-message' if owner.startswith('alice') else 'bob-message'
            self.db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)',
                (owner,mid,'test-key',hashlib.sha256(self.raw).hexdigest(),len(self.raw),1,
                 'Budget','sender@example.com',owner,'Confidential forecast','[]',1,1))
            self.db.execute('INSERT INTO search VALUES(?,?,?,?,?,?)',
                (owner,mid,'Budget','sender@example.com',owner,'Confidential forecast'))
        self.db.commit()
        with self.client.session_transaction() as s:
            s['owner']='alice@example.com'
            s['csrf']='test-csrf'
    def tearDown(self):
        self.db.close()
        self.env.stop()
        self.tmp.cleanup()
    def test_sync_commits_only_after_s3_readback(self):
        from unittest.mock import MagicMock
        api = MagicMock()
        api.list.return_value.execute.return_value={'messages':[{'id':'new-message'}]}
        import base64
        api.get.return_value.execute.return_value={
            'raw':base64.urlsafe_b64encode(self.raw).decode(),
            'internalDate':'1000','labelIds':['INBOX']}
        s3=MagicMock()
        s3.get_object.return_value={'Body':io.BytesIO(b'corrupted upload')}
        with patch('archive.gmail',return_value=api),patch('archive.storage',return_value=s3):
            with self.assertRaises(ValueError):
                archive.sync('alice@example.com')
        self.assertIsNone(self.db.execute("SELECT 1 FROM messages WHERE id='new-message'").fetchone())
    def test_cleanup_rechecks_object_before_trash(self):
        from unittest.mock import MagicMock
        self.db.execute('INSERT INTO runs(owner,started,finished) VALUES(?,?,?)',
                        ('alice@example.com',archive.now(),archive.now()))
        self.db.commit()
        api=MagicMock()
        with patch.dict(os.environ,{'ENABLE_CLEANUP':'true'}),patch('archive.gmail',return_value=api),patch('archive.storage'),patch('archive.verified_bytes',side_effect=ValueError('corrupt')):
            with self.assertRaises(ValueError):
                archive.cleanup('alice@example.com',True)
            api.trash.assert_not_called()
    def test_cross_user_routes_deny_before_storage(self):
        with patch('app.storage') as storage:
            for suffix in ['', '/download','/attachment/0']:
                self.assertEqual(self.client.get('/message/bob-message'+suffix).status_code,404)
            storage.assert_not_called()
    def test_search_scoped_to_owner(self):
        result = self.client.get('/?q=forecast')
        self.assertEqual(result.status_code,200)
        self.assertIn(b'alice-message',result.data)
        self.assertNotIn(b'bob-message',result.data)
    def test_admin_denied(self):
        self.assertEqual(self.client.get('/admin').status_code,403)
    def test_unverified_cleanup_never_calls_google(self):
        with patch('archive.gmail') as google:
            with self.assertRaises(ValueError):
                archive.cleanup('alice@example.com',apply=True)
            google.assert_not_called()
    def test_cleanup_requires_recent_successful_scan(self):
        with self.assertRaises(ValueError):
            archive.cleanup('alice@example.com')
    def test_corrupt_object_rejected(self):
        class S3:
            def get_object(self,**kwargs):
                return {'Body':io.BytesIO(b'corrupt')}
        row=self.db.execute('SELECT * FROM messages LIMIT 1').fetchone()
        with self.assertRaises(ValueError):
            archive.verified_bytes(S3(),row)
    def test_html_safe_text(self):
        _,text=archive.parsed(b'Content-Type: text/html\r\n\r\n<p>Hello</p><img src="https://tracker.example/x">')
        self.assertEqual(text,'Hello')
    def test_mailbox_allowlist(self):
        for owner in ['mallory@example.com','alice@other.com']:
            with self.assertRaises(ValueError):
                archive.validate_owner(owner)
    def test_logout_csrf(self):
        self.assertEqual(self.client.post('/logout',data={'csrf':'wrong'}).status_code,403)
        self.assertEqual(self.client.post('/logout',data={'csrf':'test-csrf'}).status_code,302)
    def test_original_download_integrity(self):
        with patch('app.storage') as factory:
            factory.return_value.get_object.return_value={'Body':io.BytesIO(self.raw)}
            result=self.client.get('/message/alice-message/download')
            self.assertEqual(result.status_code,200)
            self.assertEqual(result.data,self.raw)
            self.assertIn('attachment',result.headers['Content-Disposition'])
    def test_deenrolled_user_redirected_not_500(self):
        with self.client.session_transaction() as s:
            s['owner']='ghost@example.com'  # not in MAILBOXES allowlist
        result=self.client.get('/')
        self.assertEqual(result.status_code,302)
        self.assertIn('/login',result.headers['Location'])
    def test_hsts_header_present(self):
        self.assertIn('max-age',self.client.get('/').headers.get('Strict-Transport-Security',''))
    def test_open_domain_allows_any_domain_user(self):
        with patch.dict(os.environ,{'OPEN_DOMAIN':'true'}):
            self.assertEqual(archive.validate_owner('anyone@example.com'),'anyone@example.com')
            with self.assertRaises(ValueError):
                archive.validate_owner('anyone@other.com')  # wrong domain still rejected
    def test_select_candidates_rules(self):
        base,_=archive.select_candidates(self.db,'alice@example.com',{})
        _,all_c=archive.select_candidates(self.db,'alice@example.com',{})
        self.assertEqual(len(all_c),1)  # the seeded 1970 message is older than the default 1yr cutoff
        self.assertTrue(all(r['owner']=='alice@example.com' for r in all_c))  # owner-scoped
        self.assertEqual(len(archive.select_candidates(self.db,'alice@example.com',{'senders_include':['nomatch@']})[1]),0)
        self.assertEqual(len(archive.select_candidates(self.db,'alice@example.com',{'senders_include':['sender@']})[1]),1)
        self.assertEqual(len(archive.select_candidates(self.db,'alice@example.com',{'senders_exclude':['sender@']})[1]),0)
        self.assertEqual(len(archive.select_candidates(self.db,'alice@example.com',{'min_size':10**9})[1]),0)
        self.assertEqual(len(archive.select_candidates(self.db,'alice@example.com',{'before':'1970-01-01'})[1]),0)
    def test_backup_remove_hides_from_archive(self):
        self.assertIn(b'alice-message',self.client.get('/?q=forecast').data)
        r=self.client.post('/backup/remove',data={'csrf':'test-csrf','ids':'alice-message','next':'/'})
        self.assertEqual(r.status_code,302)
        self.assertNotIn(b'alice-message',self.client.get('/?q=forecast').data)
        self.assertNotIn(b'alice-message',self.client.get('/').data)
        self.assertEqual(self.client.get('/message/alice-message').status_code,404)
        self.assertEqual(self.client.get('/message/alice-message/download').status_code,404)
        self.assertTrue(self.db.execute("SELECT 1 FROM removed WHERE owner=? AND id=?",('alice@example.com','alice-message')).fetchone())
    def test_backup_remove_owner_scoped(self):
        self.client.post('/backup/remove',data={'csrf':'test-csrf','ids':'bob-message','next':'/'})
        self.assertIsNone(self.db.execute("SELECT 1 FROM removed WHERE id='bob-message'").fetchone())
    def test_backup_remove_csrf(self):
        self.assertEqual(self.client.post('/backup/remove',data={'csrf':'wrong','ids':'alice-message'}).status_code,403)
    def test_html_body_sanitized(self):
        h=archive.html_body(b'Content-Type: text/html\r\n\r\n<p>Hi</p><script>alert(1)</script>'
                            b'<a href="javascript:x()">y</a><img src="http://tracker/x">')
        self.assertIsNotNone(h)
        self.assertIn('<p>Hi</p>',h)
        self.assertNotIn('<script',h.lower())
        self.assertNotIn('javascript:',h.lower())
        self.assertIn('Content-Security-Policy',h)
        self.assertIsNone(archive.html_body(b'Content-Type: text/plain\r\n\r\nhello'))
    def test_message_view_renders_html_preview(self):
        htmlraw=b'From: a@example.com\r\nTo: alice@example.com\r\nSubject: HTML\r\nContent-Type: text/html\r\n\r\n<p>Hello <b>b</b></p><script>bad()</script>'
        self.db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)',
            ('alice@example.com','html-msg','k',hashlib.sha256(htmlraw).hexdigest(),len(htmlraw),2,
             'HTML','a@example.com','alice@example.com','Hello b','[]',1,1))
        self.db.commit()
        with patch('app.storage') as f:
            f.return_value.get_object.return_value={'Body':io.BytesIO(htmlraw)}
            r=self.client.get('/message/html-msg')
        self.assertEqual(r.status_code,200)
        self.assertIn(b'srcdoc',r.data)
        self.assertIn(b'Hello',r.data)
        self.assertNotIn(b'bad()',r.data)
    def test_rules_persistence(self):
        archive.save_rules(self.db,'alice@example.com',{'months':6,'senders_include':['x@y']},'alice@example.com')
        r=archive.get_rules(self.db,'alice@example.com')
        self.assertEqual(r['months'],6); self.assertEqual(r['senders_include'],['x@y'])
        self.assertEqual(archive.get_rules(self.db,'nobody@example.com'),{})
    def test_settings_user_can_save_own_rules(self):
        self.assertEqual(self.client.get('/settings').status_code,200)
        r=self.client.post('/settings',data={'csrf':'test-csrf','action':'save','months':'12'})
        self.assertEqual(r.status_code,200); self.assertIn(b'saved',r.data.lower())
        self.assertEqual(archive.get_rules(self.db,'alice@example.com').get('months'),12)
    def test_admin_can_save_mailbox_rules(self):
        with patch.dict(os.environ,{'ADMIN_EMAILS':'alice@example.com'}):
            r=self.client.post('/admin/cleanup/bob@example.com',data={'csrf':'test-csrf','action':'save','months':'24'})
            self.assertEqual(r.status_code,200)
        self.assertEqual(archive.get_rules(self.db,'bob@example.com').get('months'),24)
    def test_sync_query_from_rules(self):
        self.assertEqual(archive.sync_query({}),'-in:chats -in:drafts')
        q=archive.sync_query({'before':'2024-01-01','senders_include':['a@x','b@y'],
                              'senders_exclude':['c@z'],'min_size':5000000})
        self.assertIn('before:2024/01/01',q)
        self.assertIn('from:a@x',q); self.assertIn(' OR ',q); self.assertIn('-from:c@z',q)
        self.assertIn('larger:5000000',q)
        self.assertIn('older_than:6m',archive.sync_query({'months':6}))
    def test_cleanup_admin_requires_admin(self):
        self.assertEqual(self.client.get('/admin/cleanup/alice@example.com').status_code,403)
    def test_cleanup_preview_as_admin_trashes_nothing(self):
        with patch.dict(os.environ,{'ADMIN_EMAILS':'alice@example.com'}):
            with patch('archive.gmail') as g, patch('app.storage') as s:
                r=self.client.post('/admin/cleanup/alice@example.com',data={'csrf':'test-csrf','action':'preview'})
                self.assertEqual(r.status_code,200)
                self.assertIn(b'match',r.data)
                g.assert_not_called(); s.assert_not_called()
    def test_cleanup_apply_needs_confirmation(self):
        with patch.dict(os.environ,{'ADMIN_EMAILS':'alice@example.com'}):
            with patch('archive.gmail') as g:
                r=self.client.post('/admin/cleanup/alice@example.com',
                    data={'csrf':'test-csrf','action':'apply','confirm':'wrong','limit':'5'})
                self.assertIn(b'did not match',r.data); g.assert_not_called()
    def test_cleanup_apply_disabled_blocks(self):
        with patch.dict(os.environ,{'ADMIN_EMAILS':'alice@example.com'}):
            self.db.execute('INSERT INTO runs(owner,started,finished) VALUES(?,?,?)',
                            ('alice@example.com',archive.now(),archive.now())); self.db.commit()
            with patch('archive.gmail') as g:
                r=self.client.post('/admin/cleanup/alice@example.com',
                    data={'csrf':'test-csrf','action':'apply','confirm':'TRASH alice@example.com','limit':'5'})
                self.assertIn(b'Cleanup disabled',r.data); g.assert_not_called()

if __name__=='__main__':
    unittest.main()
