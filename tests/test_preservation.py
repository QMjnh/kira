from __future__ import annotations

import hashlib
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from kira.errors import KiraError
from kira.media import scan_photo_directory
from kira.store import KiraStore
from kira.preservation import Preservation
import kira.preservation as preservation_module
import kira.store as store_module


class PreservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kira-preservation-test-')
        self.root = Path(self.temp.name).resolve()
        self.store = KiraStore(self.root / 'working')

    def tearDown(self):
        assert self.root.is_relative_to(Path(tempfile.gettempdir()).resolve())
        self.temp.cleanup()

    def job(self, name='camera', source=None):
        source = source or self.root / name
        source.mkdir()
        (source / 'DSC_1042.JPG').write_bytes(b'original')
        asset = scan_photo_directory(str(source))['assets'][0]
        job = self.store.create_job_from_selection(name,str(source),[asset['id']],'jpeg')
        return job['id'], source

    def stage(self, jid, content=b'edited', name='DSC_1042.jpg'):
        started = self.store.start_upload(jid,'returns',name,len(content),0,hashlib.sha256(content).hexdigest())
        if not started['complete']:
            offset = started['offset']
            self.store.append_upload(jid,started['upload_id'],offset,len(content)-offset,BytesIO(content[offset:]))
        return started['upload_id']

    def test_actual_cross_volume_publication(self):
        repo = Path(__file__).resolve().parents[1]
        if repo.drive == self.root.drive:
            self.skipTest('Two writable volumes not available')
        with tempfile.TemporaryDirectory(prefix='kira-cross-volume-',dir=repo) as name:
            source = Path(name).resolve() / 'camera'
            assert source.is_relative_to(repo)
            jid, _ = self.job(source=source)
            uid = self.stage(jid)
            record = self.store.complete_upload(jid,uid)
            self.assertEqual(Path(record['storage_path']).read_bytes(),b'edited')
            self.assertEqual(record['verification'],'sender_and_receiver')

    def test_bundle_merges_concurrent_return_and_keeps_immutable_original(self):
        jid, source = self.job()
        uid = self.stage(jid)
        replace = store_module.os.replace
        fired = False
        def interleave(src,dst):
            nonlocal fired
            result = replace(src,dst)
            if Path(dst).name == 'originals.zip' and not fired:
                fired = True
                self.store.complete_upload(jid,uid)
            return result
        with patch.object(store_module.os,'replace',side_effect=interleave):
            bundle, _ = self.store.create_bundle(jid)
        self.assertEqual(len(self.store.get_job(jid)['returns']),1)
        (source / 'DSC_1042.JPG').write_bytes(b'new edits outside Kira')
        bundle, _ = self.store.create_bundle(jid)
        with zipfile.ZipFile(bundle) as archive:
            self.assertEqual(archive.read('DSC_1042.JPG'),b'original')

    def test_retry_after_final_bytes_published_but_manifest_commit_fails(self):
        jid, source = self.job()
        uid = self.stage(jid)
        with patch.object(self.store,'_save_manifest',side_effect=OSError('injected failure')):
            with self.assertRaises(OSError):
                self.store.complete_upload(jid,uid)
        self.assertEqual((source / 'selected/DSC_1042.jpg').read_bytes(),b'edited')
        # Reconstruct the service to exercise persisted recovery rather than memory.
        self.store = KiraStore(self.root / 'working')
        record = self.store.complete_upload(jid,uid)
        self.assertEqual(len(self.store.get_job(jid)['returns']),1)
        self.assertEqual(Path(record['storage_path']).read_bytes(),b'edited')
        self.assertFalse((source / 'selected/DSC_1042__v2.jpg').exists())

    def test_checksum_rejects_combined_partial_upload(self):
        jid, source = self.job()
        expected = hashlib.sha256(b'CCCCDDDD').hexdigest()
        start = self.store.start_upload(jid,'returns','export.jpg',8,0,expected)
        self.store.append_upload(jid,start['upload_id'],0,4,BytesIO(b'AAAA'))
        self.store.append_upload(jid,start['upload_id'],4,4,BytesIO(b'DDDD'))
        with self.assertRaisesRegex(KiraError,'checksum'):
            self.store.complete_upload(jid,start['upload_id'])
        self.assertFalse((source / 'selected/export.jpg').exists())
        self.assertEqual(self.store.get_job(jid)['returns'],[])
        restarted = self.store.start_upload(jid,'returns','export.jpg',8,0,expected)
        self.assertEqual(restarted['offset'],0)
        self.store.append_upload(jid,restarted['upload_id'],0,8,BytesIO(b'CCCCDDDD'))
        record = self.store.complete_upload(jid,restarted['upload_id'])
        self.assertEqual(Path(record['storage_path']).read_bytes(),b'CCCCDDDD')

    def test_same_metadata_different_content_never_resumes_or_skips(self):
        jid, _ = self.job()
        first = self.stage(jid,b'AAAABBBB','export.jpg')
        self.store.complete_upload(jid,first)
        second = self.stage(jid,b'CCCCDDDD','export.jpg')
        self.assertNotEqual(first,second)
        record = self.store.complete_upload(jid,second)
        self.assertEqual(Path(record['storage_path']).read_bytes(),b'CCCCDDDD')
        # No digest from a legacy client means a fresh, non-resuming session.
        a = self.store.start_upload(jid,'returns','legacy.jpg',8,0)
        b = self.store.start_upload(jid,'returns','legacy.jpg',8,0)
        self.assertNotEqual(a['upload_id'],b['upload_id'])

    def test_completed_upload_repairs_missing_target_but_rejects_changed_target(self):
        jid, _ = self.job()
        uid = self.stage(jid)
        record = self.store.complete_upload(jid,uid)
        path = Path(record['storage_path'])
        path.unlink()
        self.store.complete_upload(jid,uid)
        self.assertEqual(path.read_bytes(),b'edited')
        path.write_bytes(b'changed by user')
        with self.assertRaisesRegex(KiraError,'destination changed'):
            self.store.complete_upload(jid,uid)
        self.assertEqual(path.read_bytes(),b'changed by user')

    def test_zip_retains_every_component_with_paths_and_member_hashes(self):
        jid, source = self.job()
        out = BytesIO()
        contents = {'exports/DSC_1042.jpg':b'edit','settings/DSC_1042.xmp':b'settings',
                    'project/layered.psd':b'project','live.MOV':b'video'}
        with zipfile.ZipFile(out,'w') as archive:
            for path,data in contents.items():archive.writestr(path,data)
        uid = self.stage(jid,out.getvalue(),'session.zip')
        uploaded = self.store.complete_upload(jid,uid)
        result = self.store.organize_source_folder(jid)
        self.assertEqual(result['status'],'complete',result)
        returned = self.store.get_job(jid)['returns'][0]
        self.assertEqual(len(returned['members']),4)
        for member in returned['members']:
            expected = contents[member['archive_member']]
            self.assertEqual(Path(member['storage_path']).read_bytes(),expected)
            self.assertEqual(member['sha256'],hashlib.sha256(expected).hexdigest())
        self.assertTrue(Path(uploaded['storage_path']).exists())
        again = self.store.organize_source_folder(jid)
        self.assertEqual(again['status'],'complete',again)
        self.assertFalse(list(source.rglob('*__v2*')))
        # Current integrity is checked rather than trusting the old report.
        missing = Path(returned['members'][0]['storage_path'])
        missing.unlink()
        self.store.organize_source_folder(jid)
        self.assertEqual(missing.read_bytes(),b'edit')

    def test_bad_crc_never_publishes_partial_members_and_retry_does_not_duplicate(self):
        jid, source = self.job()
        out = BytesIO()
        with zipfile.ZipFile(out,'w') as archive:
            archive.writestr('first.jpg',b'edit')
            archive.writestr('second.jpg',b'uniquely corrupt payload')
        data = out.getvalue().replace(b'uniquely corrupt payload',b'Uniquely corrupt payload',1)
        uid = self.stage(jid,data,'broken.zip')
        self.store.complete_upload(jid,uid)
        for _ in range(2):
            result = self.store.organize_source_folder(jid)
            self.assertEqual(result['status'],'complete_with_warnings')
        self.assertFalse(list((source / 'selected').rglob('first.jpg')))
        self.assertFalse(list((source / 'selected').rglob('second.jpg')))
        self.assertFalse(list(source.rglob('*__v2*')))

    def test_unsafe_archive_paths_are_retained_without_extraction(self):
        jid, source = self.job()
        out = BytesIO()
        with zipfile.ZipFile(out,'w') as archive:archive.writestr('../escape.psd',b'document')
        uid = self.stage(jid,out.getvalue(),'unsafe.zip')
        receipt = self.store.complete_upload(jid,uid)
        result = self.store.organize_source_folder(jid)
        self.assertEqual(result['status'],'complete_with_warnings')
        self.assertTrue(Path(receipt['storage_path']).exists())
        self.assertFalse((source / 'escape.psd').exists())

    def test_missing_source_does_not_adopt_wrong_same_name_target(self):
        jid, source = self.job()
        (source / 'DSC_1042.JPG').unlink()
        wrong = source / 'selected/pre-edit/DSC_1042.JPG'
        wrong.parent.mkdir(parents=True)
        wrong.write_bytes(b'wrong file')
        result = self.store.organize_source_folder(jid)
        self.assertEqual(result['status'],'complete_with_warnings')
        self.assertEqual(wrong.read_bytes(),b'wrong file')

    def test_cleanup_preserves_generic_returns_history_and_restore(self):
        jid = self.store.create_job('generic')['id']
        uid = self.stage(jid)
        returned = self.store.complete_upload(jid,uid)
        self.store.delete_job(jid)
        history = self.store.preservation.history(jid)
        self.assertEqual(len(history),1)
        destination = self.root / 'restore/export.jpg'
        result = self.store.preservation.restore(history[0]['id'],destination)
        self.assertEqual(destination.read_bytes(),b'edited')
        self.assertEqual(result['sha256'],returned['sha256'])
        self.assertFalse(self.store._job_dir(jid).exists())

    def test_cleanup_refuses_the_only_partial_upload(self):
        jid, _ = self.job()
        started = self.store.start_upload(jid,'returns','export.jpg',8,0)
        self.store.append_upload(jid,started['upload_id'],0,4,BytesIO(b'part'))
        with self.assertRaisesRegex(KiraError,'partial upload'):
            self.store.delete_job(jid)
        self.assertTrue(self.store._job_dir(jid).exists())

    def test_database_can_be_rebuilt_from_permanent_manifests(self):
        jid, _ = self.job()
        uid = self.stage(jid)
        self.store.complete_upload(jid,uid)
        self.store.preservation.database.unlink()
        catalog = Preservation(self.root / 'working/library')
        self.assertEqual(catalog.rebuild(),1)
        self.assertEqual(len(catalog.history(jid)),1)

    def test_companion_naming_conventions_stay_with_the_capture(self):
        source = self.root / 'sidecars'
        source.mkdir()
        for name in ['DSC0001.JPG','DSC0001.ARW','DSC0001.xmp','DSC0001.JPG.xmp','DSC0001.psd']:
            (source / name).write_bytes(name.encode())
        assets = scan_photo_directory(str(source))['assets']
        self.assertEqual(len(assets),1)
        self.assertEqual(len(assets[0]['other_files']),3)
        jid = self.store.create_job_from_selection('with companions',str(source),[assets[0]['id']],'jpeg')['id']
        result = self.store.organize_source_folder(jid)
        self.assertEqual(result['status'],'complete',result)
        for name in ['DSC0001.JPG','DSC0001.xmp','DSC0001.JPG.xmp','DSC0001.psd']:
            self.assertEqual((source / 'selected/pre-edit' / name).read_bytes(),name.encode())

    def test_destination_failure_keeps_upload_and_checkpoint_for_retry(self):
        jid, source = self.job()
        uid = self.stage(jid)
        publish = preservation_module.publish_new
        def fail_external(temporary,destination):
            if destination.parent == source / 'selected':
                raise OSError('injected destination full or disconnected')
            return publish(temporary,destination)
        with patch.object(preservation_module,'publish_new',side_effect=fail_external):
            with self.assertRaises(OSError):self.store.complete_upload(jid,uid)
        self.assertEqual(self.store.get_job(jid)['returns'],[])
        self.assertEqual(len(self.store.preservation.history(jid)),1)
        self.assertEqual((self.store._job_dir(jid) / 'uploads' / (uid+'.part')).read_bytes(),b'edited')
        record = self.store.complete_upload(jid,uid)
        self.assertEqual(Path(record['storage_path']).read_bytes(),b'edited')
        self.assertEqual(len(self.store.get_job(jid)['returns']),1)

    def test_receipt_failure_does_not_publish_or_remove_staging(self):
        jid, source = self.job()
        uid = self.stage(jid)
        with patch.object(self.store.preservation,'receipt',side_effect=OSError('receipt write failed')):
            with self.assertRaises(OSError):self.store.complete_upload(jid,uid)
        self.assertFalse((source / 'selected/DSC_1042.jpg').exists())
        self.assertTrue((self.store._job_dir(jid) / 'uploads' / (uid+'.part')).exists())
        self.store.complete_upload(jid,uid)
        self.assertEqual((source / 'selected/DSC_1042.jpg').read_bytes(),b'edited')

    def test_organization_recovers_move_before_manifest_commit(self):
        jid, source = self.job()
        save = self.store._save_manifest
        calls = 0
        def fail_final(manifest):
            nonlocal calls
            calls += 1
            if calls == 2:raise OSError('injected final manifest failure')
            return save(manifest)
        with patch.object(self.store,'_save_manifest',side_effect=fail_final):
            with self.assertRaises(OSError):self.store.organize_source_folder(jid)
        self.store = KiraStore(self.root / 'working')
        result = self.store.organize_source_folder(jid)
        self.assertEqual(result['status'],'complete',result)
        record = self.store.get_job(jid)['files'][0]
        self.assertEqual(Path(record['source_path']).read_bytes(),b'original')
        self.assertEqual(record['sha256'],hashlib.sha256(b'original').hexdigest())
        self.assertFalse(list(source.rglob('*__v2*')))

    def test_source_changed_during_move_remains_available_at_destination(self):
        jid, source = self.job()
        publish = preservation_module.publish_new
        def change_source(temporary,destination):
            if temporary == source / 'DSC_1042.JPG':
                temporary.write_bytes(b'new external edit')
            return publish(temporary,destination)
        with patch.object(preservation_module,'publish_new',side_effect=change_source):
            result = self.store.organize_source_folder(jid)
        self.assertEqual(result['status'],'complete_with_warnings')
        self.assertEqual((source / 'selected/pre-edit/DSC_1042.JPG').read_bytes(),b'new external edit')
        checkpoint = self.store.preservation.verified_object(hashlib.sha256(b'original').hexdigest())
        self.assertEqual(checkpoint.read_bytes(),b'original')

    def test_restore_refuses_to_overwrite_a_different_file(self):
        jid, _ = self.job()
        uid = self.stage(jid)
        self.store.complete_upload(jid,uid)
        saved = self.store.preservation.history(jid)[0]
        destination = self.root / 'existing.jpg'
        destination.write_bytes(b'other content')
        with self.assertRaises(KiraError):self.store.preservation.restore(saved['id'],destination)
        self.assertEqual(destination.read_bytes(),b'other content')

    def test_individual_download_is_bound_to_a_retained_original(self):
        jid, source = self.job()
        record = self.store.get_job(jid)['files'][0]
        original, _ = self.store.resolve_file(jid,'originals',record['id'])
        (source / 'DSC_1042.JPG').write_bytes(b'external edit')
        again, _ = self.store.resolve_file(jid,'originals',record['id'])
        self.assertEqual(again.read_bytes(),b'original')
        self.assertEqual(original,again)

    def test_startup_finishes_a_partially_rebuilt_catalog(self):
        jid, _ = self.job()
        for data in (b'edit one',b'edit two'):
            self.store.complete_upload(jid,self.stage(jid,data))
        catalog = self.store.preservation
        saved = catalog.history(jid)
        with catalog.transaction() as db:
            db.execute('DELETE FROM revisions WHERE id=?',(saved[0]['id'],))
        restarted = Preservation(catalog.root)
        self.assertEqual(len(restarted.history(jid)),2)

    def test_corrupted_bundle_cache_is_rebuilt_from_retained_originals(self):
        jid, _ = self.job()
        bundle, _ = self.store.create_bundle(jid)
        bundle.write_bytes(b'corrupt cached ZIP')
        rebuilt, _ = self.store.create_bundle(jid)
        with zipfile.ZipFile(rebuilt) as archive:
            self.assertEqual(archive.read('DSC_1042.JPG'),b'original')

    def test_only_one_server_can_write_a_working_directory(self):
        from kira.main import build_server
        root = self.root / 'server-working'
        server = build_server('127.0.0.1',0,root)
        try:
            with self.assertRaisesRegex(KiraError,'already using'):
                build_server('127.0.0.1',0,root)
        finally:
            server.server_close()
        restarted = build_server('127.0.0.1',0,root)
        restarted.server_close()

    def test_writer_lease_waits_for_active_requests_on_close(self):
        from kira.writer import WriterLease
        root = self.root / 'leased'
        lease = WriterLease(root)
        lease.retain_request()
        lease.close()
        with self.assertRaises(KiraError):WriterLease(root)
        lease.release_request()
        restarted = WriterLease(root)
        restarted.close()


if __name__ == '__main__':
    unittest.main()
