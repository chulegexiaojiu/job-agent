"""Source replacement must invalidate review without erasing delivery history."""
import json
from pathlib import Path
import tempfile
import unittest
from PIL import Image
from job_agent.store import Store
from job_agent.delivery import Executor, Ledger, Policy, DeliveryBlocked, validated_bundle, prepare
from job_agent.source_revision import replace_source
from test_delivery import Adapter, build_job


class SourceRevisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.store = Store(self.root/'workspace.sqlite3')
        self.ledger = Ledger(self.store); self.job, _ = build_job(self.store, self.root)
        self.url = self.store.job(self.job)['url']
        self.image = self.root/'new.png'; Image.new('RGB', (400, 400), 'white').save(self.image)

    def replace(self, **kwargs):
        return replace_source(self.store, self.root, self.job, kwargs.get('url',self.url),
                              kwargs.get('text','New complete synthetic JD'), kwargs.get('screenshot',self.image))

    def test_revision_archives_exports_blocks_old_plan_and_preserves_job_id(self):
        plan=prepare(self.ledger,self.root,self.job,'boss_greeting')
        old=self.store.job(self.job); before=Path(old['detail']['exports']['resume']).read_bytes()
        revision=Path(self.replace())
        row=self.store.job(self.job)
        self.assertEqual(row['status'],'queued');self.assertEqual(row['url'],self.url)
        self.assertIsNone(row['quality']);self.assertIsNone(row['match']);self.assertNotIn('research',row['detail'])
        self.assertEqual(Path(row['detail']['stale_exports']['resume']).read_bytes(),before)
        self.assertEqual(json.loads((revision/'previous.json').read_text(encoding='utf-8'))['detail'],old['detail'])
        self.assertEqual(Path(row['screenshot']).read_bytes(),self.image.read_bytes())
        self.assertEqual(self.ledger.action(plan['id'])['status'],'blocked')
        with self.assertRaises(DeliveryBlocked):validated_bundle(self.store,self.root,self.job)
        self.assertEqual(self.store.add_job(self.url,'duplicate')[1],False)
        self.assertEqual(len(self.store.jobs()),1)

    def test_bad_url_image_and_active_work_leave_source_unchanged(self):
        original=self.store.job(self.job)
        for args in ({'url':'https://example.test/other'}, {'text':''}, {'screenshot':self.root/'missing.png'}):
            with self.subTest(args=list(args)):
                with self.assertRaises((ValueError,OSError)):self.replace(**args)
                self.assertEqual(self.store.job(self.job),original)
        self.store.update(self.job,status='running')
        with self.assertRaises(ValueError):self.replace()
        self.store.update(self.job,status='draft_ready')
        plan=prepare(self.ledger,self.root,self.job,'boss_greeting');self.ledger.reserve(plan['id'],Policy())
        with self.assertRaises(ValueError):self.replace()
        self.assertEqual(self.store.job(self.job)['jd'],original['jd'])

    def test_reanalysis_with_same_resume_cannot_reuse_previous_source_plan(self):
        plan=prepare(self.ledger,self.root,self.job,'boss_greeting')
        original=self.store.job(self.job);revision=self.replace()
        detail=original['detail'];detail['source_revision']=revision
        self.store.update(self.job,status='draft_ready',company=original['company'],title=original['title'],detail=detail)
        adapter=Adapter()
        self.assertEqual(Executor(self.ledger,self.root,Policy(),adapter).run(plan['id']),'blocked')
        self.assertEqual(adapter.calls,[])
        updated=prepare(self.ledger,self.root,self.job,'boss_greeting')
        self.assertEqual(updated['payload']['source_revision'],revision)
        self.assertEqual(Executor(self.ledger,self.root,Policy(),adapter).run(updated['id']),'previewed')

    def test_unknown_delivery_and_quota_survive_source_replacement(self):
        plan=prepare(self.ledger,self.root,self.job,'boss_greeting');self.ledger.reserve(plan['id'],Policy())
        self.ledger.transition(plan['id'],'unknown',allowed={'reserved'})
        counts=self.ledger.counts();self.replace()
        self.assertEqual(self.ledger.action(plan['id'])['status'],'unknown')
        self.assertEqual(self.ledger.counts(),counts)

if __name__=='__main__':unittest.main()
