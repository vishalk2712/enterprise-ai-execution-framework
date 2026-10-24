import sqlite3
import unittest
from outcome_core.canonical import fingerprint
from outcome_core import outbox
from enterprise_ai import matching, secret_store
from outcome_core.matching import levenshtein_similarity
from outcome_core.secrets import SecretStore


class SharedCoreTests(unittest.TestCase):
    def test_compatibility_facades(self):
        self.assertIs(matching.levenshtein_similarity, levenshtein_similarity)
        self.assertIs(secret_store.SecretStore, SecretStore)

    def test_canonical_hash(self):
        self.assertEqual(fingerprint({'b': 1, 'a': 'é'}), fingerprint({'a': 'é', 'b': 1}))
        with self.assertRaises(ValueError): fingerprint({'score': float('nan')})

    def test_outbox_approval_dedup_and_ambiguous_dispatch(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.executescript(outbox.SCHEMA)
        payload = {'repo': 'owner/repo', 'title': 'SSO'}
        args = (db, 'c1', 'github.create_issue', payload, fingerprint(payload), 'human', '2026-10-24')
        with db:
            key = outbox.enqueue(*args)
            self.assertEqual(key, outbox.enqueue(*args))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM outbox_actions').fetchone()[0], 1)
            self.assertTrue(outbox.claim(db, key))
            self.assertFalse(outbox.claim(db, key))
            outbox.finish(db, key, 'UNKNOWN')
            self.assertFalse(outbox.claim(db, key))
        with self.assertRaises(ValueError):
            outbox.enqueue(db, 'c1', 'github.create_issue', {**payload, 'title': 'changed'}, fingerprint(payload), 'human', 'now')
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE outbox_actions SET payload_json='{}'")

    def test_outbox_rolls_back_with_host_transaction(self):
        db = sqlite3.connect(':memory:'); self.addCleanup(db.close)
        db.executescript(outbox.SCHEMA)
        with self.assertRaises(RuntimeError):
            with db:
                outbox.enqueue(db, 'c', 'target', {}, fingerprint({}), 'human', 'now')
                raise RuntimeError()
        self.assertEqual(db.execute('SELECT COUNT(*) FROM outbox_actions').fetchone()[0], 0)
