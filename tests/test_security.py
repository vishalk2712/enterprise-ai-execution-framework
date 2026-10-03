"""Authenticated HTTP permissions, tenant pins and vault failure boundaries."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

from test_distributed import ExecutionFixture
from enterprise_ai.engine import Engine
from enterprise_ai.security import AccessControl, PERMISSIONS, password_hash, bootstrap
from enterprise_ai.secret_store import SecretStore
from enterprise_ai.server import demo_csv, make_server
from enterprise_ai.mock_erp import MockERP
from enterprise_ai.worker import WorkerClient,run_worker

PASSWORD='synthetic-acceptance-password-123'
ENCODED=password_hash(PASSWORD)


def identities(tenant='local'):
    return {'tenant_id':tenant,'principals':[{'id':r,'roles':[r],'password_hash':ENCODED} for r in PERMISSIONS]+
            [{'id':'dual','roles':['steward','approver'],'password_hash':ENCODED}]}


class AccessTests(ExecutionFixture):
    def setUp(self):
        super().setUp()
        self.access=AccessControl(identities(),'local')
        self.protected=make_server(self.engine,0,security=self.access)
        threading.Thread(target=self.protected.serve_forever,daemon=True).start()
        self.addCleanup(self.protected.server_close)
        self.addCleanup(self.protected.shutdown)
        self.secured_origin='http://127.0.0.1:'+str(self.protected.server_port)
        self.opener=build_opener(ProxyHandler({}))

    def request(self,path,data=None,cookie=None,extra=None):
        headers={'Content-Type':'application/json','Origin':self.secured_origin,**(extra or {})}
        if cookie: headers['Cookie']=cookie
        req=Request(self.secured_origin+path,json.dumps(data).encode() if data is not None else None,headers)
        try: response=self.opener.open(req,timeout=5)
        except HTTPError as error: response=error
        with response:
            return response.code,json.loads(response.read()),response.headers

    def login(self,role):
        status,_,headers=self.request('/api/session/login',{'username':role,'password':PASSWORD})
        self.assertEqual(status,200)
        self.assertIn('HttpOnly',headers['Set-Cookie'])
        return headers['Set-Cookie'].split(';')[0]

    def test_anonymous_cannot_read_data_exports_or_use_worker_credential_as_user(self):
        for path in ('/api/state','/api/export','/api/audit-export','/api/portal','/api/evaluations','/api/investigations','/api/review-candidates'):
            self.assertEqual(self.request(path)[0],401,path)
            self.assertEqual(self.request(path,extra={'Authorization':'Bearer '+self.token})[0],401,path)
        self.assertEqual(self.request('/api/worker/claim',{},self.login('approver'))[0],403)

    def test_steward_stages_and_different_approver_releases(self):
        steward,approver=self.login('steward'),self.login('approver')
        entity=self.engine.state()['entities'][0]['entity_id']
        status,action,_=self.request('/api/actions',{'entity_id':entity},steward)
        self.assertEqual(status,200)
        self.assertEqual(action['created_by'],'steward')
        path='/api/actions/'+action['action_id']+'/approve'
        self.assertEqual(self.request(path,{},steward)[0],403)
        self.assertEqual(self.request(path,{},approver)[0],200)
        payload=json.loads(self.engine.db.execute("SELECT payload FROM audit WHERE event='action_approved'").fetchone()[0])
        self.assertEqual(payload['actor'],'approver')
        self.assertEqual(self.request('/api/actions',{'entity_id':entity},approver)[0],403)
        self.assertEqual(self.request('/api/demo',{},approver)[0],403)

    def test_labels_use_authenticated_actor_and_approvers_cannot_review(self):
        item=self.engine.evaluations()['evaluations'][0]
        data={'evaluation_id':item['evaluation_id'],'human_label':'Unsure','reviewer':'forged-admin','reason':'Synthetic test evidence only'}
        self.assertEqual(self.request('/api/reviews',data,self.login('approver'))[0],403)
        status,result,_=self.request('/api/reviews',data,self.login('steward'))
        self.assertEqual(status,200)
        self.assertEqual(result['reviewer'],'steward')

    def test_dual_role_author_cannot_approve_own_action(self):
        cookie=self.login('dual')
        entity=self.engine.state()['entities'][0]['entity_id']
        _,action,_=self.request('/api/actions',{'entity_id':entity},cookie)
        self.assertEqual(self.request('/api/actions/'+action['action_id']+'/approve',{},cookie)[0],400)
        self.assertEqual(self.engine._action(action['action_id'])['status'],'pending')

    def test_viewer_and_tenant_forgery_are_denied(self):
        cookie=self.login('viewer')
        self.assertEqual(self.request('/api/state',cookie=cookie)[0],200)
        self.assertEqual(self.request('/api/state',cookie=cookie,extra={'X-Tenant-Id':'other'})[0],403)
        for path in ('/api/demo','/api/reviews','/api/actions','/api/investigations','/api/actions/id/approve','/api/actions/id/execute','/api/actions/id/retry'):
            self.assertEqual(self.request(path,{},cookie)[0],403,path)
        self.assertEqual(self.request('/api/demo',{'tenant_id':'other'},self.login('steward'))[0],403)

    def test_logout_expiry_restart_and_cross_tenant_sessions(self):
        cookie=self.login('viewer')
        other=AccessControl(identities('other'),'other')
        self.assertIsNone(other.principal(cookie))
        self.assertEqual(self.request('/api/session/logout',{},cookie)[0],200)
        self.assertEqual(self.request('/api/state',cookie=cookie)[0],401)
        cookie=self.login('viewer')
        token=cookie.split('=',1)[1]
        self.access.sessions[token]['expires']=0
        self.assertEqual(self.request('/api/state',cookie=cookie)[0],401)
        self.assertIsNone(AccessControl(identities(),'local').principal(self.login('viewer')))

    def test_investigator_is_required_and_cannot_release_actions(self):
        action=self.approved()
        with self.engine.db: self.engine.db.execute('UPDATE execution_jobs SET attempts=3')
        self.client.request('claim',{})
        data={'action_id':action['action_id'],'reason':'Checked the immutable destination evidence'}
        self.assertEqual(self.request('/api/investigations',data,self.login('approver'))[0],403)
        status,result,_=self.request('/api/investigations',data,self.login('investigator'))
        self.assertEqual((status,result['resolution']),(200,'INVESTIGATED_NO_RECEIPT'))
        self.assertEqual(self.request('/api/actions/id/approve',{},self.login('investigator'))[0],403)

    def test_login_requires_same_origin_and_worker_tokens_do_not_cross_servers(self):
        self.assertEqual(self.request('/api/session/login',{'username':'viewer','password':PASSWORD},extra={'Origin':'http://evil.example'})[0],403)
        bad=WorkerClient(self.origin,'another-tenant-token-'+'y'*32)
        with self.assertRaises(HTTPError): bad.request('identity')

    def test_independent_tenant_server_rejects_foreign_cookie_and_data(self):
        engine=Engine(tenant_id='other')
        self.addCleanup(engine.close)
        suppliers,spend=demo_csv()
        engine.analyze(suppliers.replace('Northbridge','Other Tenant'),spend)
        other=make_server(engine,0,security=AccessControl(identities('other'),'other'))
        threading.Thread(target=other.serve_forever,daemon=True).start()
        self.addCleanup(other.server_close); self.addCleanup(other.shutdown)
        origin='http://127.0.0.1:'+str(other.server_port)
        with self.assertRaises(HTTPError) as error:
            self.opener.open(Request(origin+'/api/state',headers={'Cookie':self.login('viewer')}),timeout=5)
        self.assertEqual(error.exception.code,401)
        own=self.request('/api/state',cookie=self.login('viewer'))[1]
        self.assertNotIn('Other Tenant',json.dumps(own))


class TenantAndKeyTests(unittest.TestCase):
    def test_tenant_and_bank_key_pins_prevent_database_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Path(directory)/'tenant.sqlite'
            engine=Engine(db,tenant_id='a',bank_link_key=b'x'*32)
            engine.analyze(*demo_csv()); engine.close()
            for tenant,key in (('b',b'x'*32),('a',b'y'*32),('a',None)):
                with self.assertRaises(ValueError): Engine(db,tenant_id=tenant,bank_link_key=key)
            Engine(db,tenant_id='a',bank_link_key=b'x'*32).close()

    def test_ingestion_pseudonymizes_bank_hash_in_records_evidence_and_exports(self):
        import csv,io
        original='a'*64
        suppliers,spend=demo_csv()
        rows=list(csv.DictReader(io.StringIO(suppliers)))
        output=io.StringIO()
        writer=csv.DictWriter(output,fieldnames=[*rows[0],'bank_account_hash']); writer.writeheader()
        for row in rows: writer.writerow({**row,'bank_account_hash':original})
        suppliers=output.getvalue()
        with tempfile.TemporaryDirectory() as directory:
            engine=Engine(Path(directory)/'tenant.sqlite',tenant_id='a',bank_link_key=b'x'*32)
            try:
                engine.analyze(suppliers,spend)
                self.assertNotIn(original,engine.export_audit())
                self.assertNotIn(original,json.dumps(engine._records('suppliers')))
                dumped='\n'.join(engine.db.iterdump())
                self.assertNotIn(original,dumped)
                self.assertNotIn((b'x'*32).hex(),dumped)
                other=Engine(tenant_id='b',bank_link_key=b'x'*32)
                try:
                    other.analyze(suppliers,spend)
                    left=next(r['bank_account_hash'] for r in engine._records('suppliers') if r['bank_account_hash'])
                    right=next(r['bank_account_hash'] for r in other._records('suppliers') if r['bank_account_hash'])
                    self.assertNotEqual(left,right)
                finally: other.close()
            finally: engine.close()

    def test_erp_tenant_pin_and_identity_configuration_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'erp.sqlite'
            MockERP(path,[],tenant_id='a').close()
            with self.assertRaises(ValueError): MockERP(path,[],tenant_id='b')
        engine=Engine(tenant_id='a')
        try:
            with self.assertRaises(ValueError): make_server(engine,0,security=AccessControl(identities('b'),'b'))
        finally: engine.close()

    def test_bootstrap_never_overwrites_and_login_throttles(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'auth.json'
            bootstrap(path,'a')
            initial=path.read_bytes()
            with self.assertRaises(ValueError): bootstrap(path,'a')
            self.assertEqual(path.read_bytes(),initial)
        access=AccessControl(identities(),'local')
        for _ in range(5):
            with self.assertRaises(ValueError): access.login('viewer','wrong')
        with self.assertRaisesRegex(ValueError,'temporarily'): access.login('viewer',PASSWORD)

    def test_enabling_rbac_invalidates_legacy_approvals_and_cannot_downgrade(self):
        engine=Engine()
        try:
            engine.analyze(*demo_csv())
            action=engine.approve_action(engine.stage_action(engine.state()['entities'][0]['entity_id'])['action_id'])
            server=make_server(engine,0,security=AccessControl(identities(),'local'))
            server.server_close()
            self.assertEqual(engine._action(action['action_id'])['status'],'stale')
            with self.assertRaises(ValueError): make_server(engine,0)
        finally: engine.close()


class VaultBoundaryTests(unittest.TestCase):
    def test_remote_plaintext_and_credential_urls_are_rejected(self):
        for url in ('http://vault.example','https://user:password@vault.example','https://vault.example/path','https://vault.example?token=x'):
            with self.assertRaises(ValueError): SecretStore({'provider':'hashicorp','url':url})
        with self.assertRaises(ValueError): SecretStore({'provider':'azure','url':'https://untrusted.example'})

    def test_kv_read_failure_is_generic_without_file_fallback(self):
        store=SecretStore({'provider':'hashicorp','url':'http://127.0.0.1:8200','mount':'secret','token_env':'OUTCOME_TEST_TOKEN'})
        with patch.dict(os.environ,{'OUTCOME_TEST_TOKEN':'synthetic-only-token'}),patch.object(store.opener,'open',side_effect=OSError('sensitive-server-response')):
            with self.assertRaisesRegex(ValueError,'no local fallback') as error: store.get('tenant/worker')
            self.assertNotIn('sensitive',str(error.exception))
        for ref in ('../secret','a//b','a?token=x'):
            with self.assertRaises(ValueError): store.get(ref)

    def test_azure_configured_client_response_and_failure(self):
        store=SecretStore({'provider':'azure','url':'https://test.vault.azure.net'})
        class Client:
            def get_secret(self,name):
                if name=='unavailable': raise OSError('sensitive-sdk-response')
                return type('Secret',(),{'value':'synthetic-azure-result'})()
        store.client=Client()
        self.assertEqual(store.get('worker-token'),'synthetic-azure-result')
        with self.assertRaisesRegex(ValueError,'no local fallback'): store.get('unavailable')


@unittest.skipUnless(os.environ.get('OUTCOME_TEST_VAULT')=='1','Optional genuine HashiCorp Vault service test')
class RealVaultTests(ExecutionFixture):
    def test_real_kv_v2_credential_resolution_and_bad_token(self):
        url=os.environ.get('OUTCOME_TEST_VAULT_URL','http://127.0.0.1:8200')
        token=os.environ['OUTCOME_VAULT_TOKEN']
        value=self.token
        opener=build_opener(ProxyHandler({}))
        request=Request(url+'/v1/secret/data/acceptance/worker',json.dumps({'data':{'value':value}}).encode(),{'X-Vault-Token':token,'Content-Type':'application/json'},method='POST')
        with opener.open(request,timeout=5) as response: self.assertEqual(response.code,200)
        store=SecretStore({'provider':'hashicorp','url':url})
        self.assertEqual(store.get('acceptance/worker'),value)
        self.approved()
        result=run_worker(self.origin,'nonexistent-token-file',once=True,token=store.get('acceptance/worker'))
        self.assertEqual(result['state'],'verified')
        with patch.dict(os.environ,{'OUTCOME_VAULT_TOKEN':'wrong-token'}):
            with self.assertRaisesRegex(ValueError,'no local fallback'): store.get('acceptance/worker')
