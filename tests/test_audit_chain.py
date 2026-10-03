"""Operational log tampering, rollback, checkpoints and legacy anchoring."""
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from enterprise_ai.engine import Engine
from enterprise_ai.server import demo_csv


class AuditChainTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(); self.addCleanup(self.engine.close)
        self.engine.analyze(*demo_csv())

    def test_export_checkpoint_verifies_after_further_events(self):
        checkpoint = self.engine.verify_audit()
        self.engine.query('total spend')
        result = self.engine.verify_audit(checkpoint)
        self.assertGreater(result['sequence'], checkpoint['sequence'])
        exported = [json.loads(line) for line in self.engine.export_audit().splitlines()]
        self.assertEqual(exported[-1]['table'], 'audit_checkpoint')

    def test_event_edit_is_detected_and_export_refused(self):
        self.engine.db.execute("UPDATE audit SET event='forged' WHERE sequence=1")
        with self.assertRaisesRegex(ValueError, 'sequence 1'):
            self.engine.export_audit()

    def test_tail_deletion_cannot_pass_with_retained_head(self):
        self.engine.db.execute('DELETE FROM audit_chain WHERE sequence=1')
        self.engine.db.execute('DELETE FROM audit WHERE sequence=1')
        with self.assertRaises(ValueError):
            self.engine.verify_audit()

    def test_transaction_rollback_keeps_log_and_chain_consistent(self):
        before = self.engine.verify_audit()
        with self.assertRaises(RuntimeError):
            with self.engine.db:
                self.engine._log('rollback_probe', {})
                raise RuntimeError('roll back')
        self.assertEqual(self.engine.verify_audit()['head_hash'], before['head_hash'])

    def test_foreign_or_changed_checkpoint_is_rejected(self):
        checkpoint = self.engine.verify_audit()
        with self.assertRaises(ValueError):
            self.engine.verify_audit({**checkpoint, 'head_hash': '0'*64})
        with self.assertRaises(ValueError):
            self.engine.verify_audit({**checkpoint, 'chain_id': 'other'})

    def test_legacy_history_is_anchored_and_restart_detects_edits(self):
        with TemporaryDirectory() as folder:
            path = Path(folder)/'legacy.sqlite'
            db = sqlite3.connect(path)
            db.execute('CREATE TABLE audit(sequence INTEGER PRIMARY KEY AUTOINCREMENT,timestamp TEXT NOT NULL,event TEXT NOT NULL,payload TEXT NOT NULL)')
            db.execute("INSERT INTO audit(timestamp,event,payload) VALUES('legacy','legacy','{}')")
            db.commit(); db.close()
            engine = Engine(str(path))
            self.assertEqual(engine.verify_audit()['legacy_events'], 1)
            engine.db.execute("UPDATE audit SET payload='{\"edited\":true}' WHERE sequence=1")
            engine.db.commit(); engine.close()
            with self.assertRaises(ValueError):
                Engine(str(path))
