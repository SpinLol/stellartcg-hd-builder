import base64
import io
import json
import os
import unittest
from unittest.mock import patch
from PIL import Image
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from builder import build, digest, encoded, render, POLICY, printing_url, high_resolution, artwork_signature


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
            self.assertEqual((500, 500), image.size)
            self.assertEqual(0, image.getpixel((0, 100))[3])
            self.assertEqual(255, image.getpixel((250, 250))[3])

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

    def test_exact_printing_resolves_without_guessing_another_set(self):
        printing={"source_card_id":"sv3pt5-1","metadata_source":"https://raw.githubusercontent.com/PokemonTCG/pokemon-tcg-data/master/cards/en/sv3pt5.json"}
        self.assertEqual("https://images.pokemontcg.io/sv3pt5/1_hires.png",printing_url(printing))
        printing["metadata_source"]="https://raw.githubusercontent.com/PokemonTCG/pokemon-tcg-data/master/cards/en/sv1.json"
        self.assertIsNone(printing_url(printing))

    def test_higher_scan_must_match_existing_artwork(self):
        reference=artwork_signature(self.original)[0]
        out=io.BytesIO();Image.new("RGB",(600,1000),(200,30,20)).save(out,"PNG")
        row={"remote":"https://images.pokemontcg.io/sv3pt5/1_hires.png","reference_dhash":reference,"minimum_dimension":256}
        with patch("builder.remote",return_value=out.getvalue()):
            self.assertIsNotNone(high_resolution(row))
        wrong=Image.new("L",(900,800));wrong.putdata([255-int(x/899*255) for y in range(800) for x in range(900)]);out=io.BytesIO();wrong.save(out,"PNG")
        with patch("builder.remote",return_value=out.getvalue()):
            with self.assertRaises(ValueError): high_resolution(row)

    def test_low_resolution_does_not_get_fabricated_hd(self):
        source=json.loads(self.store.get("sources","channels/preview.json"))
        for row in source["assets"].values():row["minimum_dimension"]=512
        self.store.put("sources","channels/preview.json",encoded(source))
        report=build(self.store,"preview",self.pem)
        self.assertEqual(0,report["generated"])
        self.assertEqual(2,report["standard_fallbacks"])

    def test_interrupted_large_build_resumes_checkpoint_without_reprocessing(self):
        source=json.loads(self.store.get("sources","channels/preview.json"))
        row=next(iter(source["assets"].values()))
        source["assets"]={f"stellartcg:atlas/{i:03}":dict(row,test_index=i) for i in range(110)}
        self.store.put("sources","channels/preview.json",encoded(source))
        image=render(self.original)
        actual_get=self.store.get
        calls=0
        def interrupted(bucket,key,maximum=32 << 20):
            nonlocal calls
            if key.startswith("sources/"):
                calls+=1
                if calls==104:raise OSError("Interrupted test build")
            return actual_get(bucket,key,maximum)
        with patch.object(self.store,"get",side_effect=interrupted),patch("builder.render",return_value=image):
            with self.assertRaises(OSError):build(self.store,"preview",self.pem,workers=1)
        self.assertIsNone(actual_get("finished","channels/preview.json"))
        self.assertEqual(100,len(json.loads(actual_get("finished","indexes/preview.json"))))
        with patch("builder.render",return_value=image) as renderer:
            report=build(self.store,"preview",self.pem)
        self.assertEqual(100,report["reused"])
        self.assertEqual(10,renderer.call_count)


if __name__ == "__main__":
    unittest.main()
