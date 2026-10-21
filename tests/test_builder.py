import base64
import io
import json
import os
import unittest
from unittest.mock import patch
from PIL import Image
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from builder import build, digest, encoded, render, POLICY


class Store:
    sources, output = "sources", "finished"
    def __init__(self):
        self.files, self.writes = {}, []
    def get(self, bucket, key, maximum=32 << 20):
        return self.files.get((bucket, key))
    def put(self, bucket, key, data, immutable=False):
        self.files[bucket, key] = data
        self.writes.append((bucket, key))
    def objects(self, bucket):
        return {key: len(data) for (b, key), data in self.files.items() if b == bucket}


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.summary = patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""})
        self.summary.start()
        self.addCleanup(self.summary.stop)
        self.key = Ed25519PrivateKey.generate()
        self.pem = self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        out = io.BytesIO()
        Image.new("RGBA", (300, 500), (200, 30, 20, 255)).save(out, "PNG")
        self.original = out.getvalue()
        self.store = Store()
        self.store.put("sources", "sources/" + digest(self.original), self.original)
        row = {"source": digest(self.original), "mode": "contain", "trim": False}
        self.store.put("sources", "channels/preview.json", encoded({"format": 1, "policy": POLICY, "content_hash": "test", "assets": {"stellartcg:atlas/test": row, "stellartcg:live/test": row}}))

    def test_transparency_dimensions_and_determinism(self):
        data = render(self.original)
        self.assertEqual(data, render(self.original))
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual((2048, 2048), image.size)
            self.assertEqual(0, image.getpixel((0, 100))[3])
            self.assertEqual(255, image.getpixel((1024, 1024))[3])

    def test_publish_is_signed_deduplicated_and_incremental(self):
        first = build(self.store, "preview", self.pem)
        self.assertEqual(1, first["generated"])
        self.assertEqual(1, first["reused"])
        document = json.loads(self.store.get("finished", "channels/preview.json"))
        self.key.public_key().verify(base64.b64decode(document["signature"]), base64.b64decode(document["payload"]))
        self.assertEqual(2, len(json.loads(base64.b64decode(document["payload"]))["assets"]))
        with patch("builder.render", side_effect=AssertionError("Unchanged source was re-encoded")):
            second = build(self.store, "preview", self.pem)
        self.assertEqual(0, second["generated"])
        self.assertEqual(2, second["reused"])

    def test_bad_source_never_replaces_published_catalog(self):
        build(self.store, "preview", self.pem)
        before = self.store.get("finished", "channels/preview.json")
        row = {"source": "0" * 64, "mode": "contain", "trim": False}
        self.store.put("sources", "channels/preview.json", encoded({"format": 1, "policy": POLICY, "content_hash": "changed", "assets": {"stellartcg:atlas/new": row}}))
        with self.assertRaises(ValueError):
            build(self.store, "preview", self.pem)
        self.assertEqual(before, self.store.get("finished", "channels/preview.json"))

    def test_storage_budget_keeps_old_catalog(self):
        with self.assertRaises(ValueError):
            build(self.store, "preview", self.pem, budget=1)
        self.assertIsNone(self.store.get("finished", "channels/preview.json"))

    def test_bad_source_format_is_rejected(self):
        with self.assertRaises(Exception):
            render(b"not an image")


if __name__ == "__main__":
    unittest.main()

