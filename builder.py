"""Independent, incremental R2 artwork pipeline. Never generates HD on a Minecraft server.

Only publish_sources reads the operator's read-only content export. build runs on Actions.
The public repository contains recipes, never credentials, originals or server/player data.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import warnings
from PIL import Image, ImageOps
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

MAX_SOURCE = 32 * 1024 * 1024
MAX_CATALOG = 32 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 16_777_216
POLICY = "hd2048-webp95-lanczos-v1"
HASH = re.compile(r"[a-f0-9]{64}\Z")
LOGICAL = re.compile(r"stellartcg:[a-z0-9_./-]{1,180}\Z")


def encoded(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def channel(value):
    if not re.fullmatch(r"[a-z0-9_-]{1,32}", value):
        raise ValueError("Invalid channel")
    return value


def render(source, mode="contain", trim=False):
    if len(source) > MAX_SOURCE:
        raise ValueError("Original exceeds byte limit")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(source)) as original:
            if getattr(original, "n_frames", 1) != 1:
                raise ValueError("Animated originals are unsupported")
            if original.width * original.height > Image.MAX_IMAGE_PIXELS:
                raise ValueError("Original exceeds pixel limit")
            image = ImageOps.exif_transpose(original).convert("RGBA")
    if trim:
        bounds = image.getchannel("A").getbbox()
        if bounds:
            image = image.crop(bounds)
    if mode == "contain":
        scale = min(2048 / image.width, 2048 / image.height)
        size = (max(1, int(image.width * scale + .5)), max(1, int(image.height * scale + .5)))
        resized = image.resize(size, Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (2048, 2048))
        canvas.paste(resized, ((2048 - size[0]) // 2, (2048 - size[1]) // 2))
    elif mode in ("fill", "stretch"):
        # Physical product UVs already encode proportions; preserve the server's face policy.
        canvas = image.resize((2048, 2048), Image.Resampling.LANCZOS)
    else:
        raise ValueError("Unknown layout mode")
    output = io.BytesIO()
    canvas.save(output, "WEBP", quality=95, method=6, exact=True)
    return output.getvalue()


class R2:
    def __init__(self, account, sources, output):
        import boto3
        from botocore.config import Config
        if not re.fullmatch(r"[a-f0-9]{32}", account):
            raise ValueError("Invalid Cloudflare account ID")
        self.sources, self.output = sources, output
        self.s3 = boto3.client("s3", endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
                               region_name="auto", config=Config(retries={"max_attempts": 5, "mode": "standard"},
                               connect_timeout=10, read_timeout=90))

    def get(self, bucket, key, maximum=MAX_CATALOG):
        from botocore.exceptions import ClientError
        try:
            r = self.s3.get_object(Bucket=bucket, Key=key)
        except ClientError as error:
            if error.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise
        with r["Body"] as stream:
            if r["ContentLength"] > maximum:
                raise ValueError("Remote object exceeds limit")
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("Remote object exceeds limit")
        return data

    def put(self, bucket, key, data, immutable=False):
        self.s3.put_object(Bucket=bucket, Key=key, Body=data,
            ContentType="image/webp" if key.endswith(".webp") else "application/json" if key.endswith(".json") else "application/octet-stream",
            CacheControl="public,max-age=31536000,immutable" if immutable else "no-cache, max-age=0, must-revalidate",
            Metadata={"sha256": digest(data)}, StorageClass="STANDARD")

    def objects(self, bucket):
        result = {}
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            for item in page.get("Contents", []):
                result[item["Key"]] = item["Size"]
        return result


def publish_sources(store, file, name, limit=0, budget=8 << 30):
    """Stream unchanged originals directly to a PRIVATE bucket; no HD encoding here."""
    document = json.loads(Path(file).read_text(encoding="utf-8-sig"))
    if document.get("format") != 1 or len(document["assets"]) > 200000:
        raise ValueError("Invalid source export")
    if limit and name == "stable":
        raise ValueError("A partial sample must use a preview channel")
    existing = store.objects(store.sources)
    used = sum(existing.values()) + sum(store.objects(store.output).values())
    rows, seen, uploaded = {}, {}, 0
    for logical, row in sorted(document["assets"].items()):
        if not LOGICAL.fullmatch(logical) or ".." in logical:
            raise ValueError("Invalid logical ID")
        if limit and len(rows) >= limit:
            break
        path = Path(row["file"])
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SOURCE:
            raise ValueError("Invalid original source file")
        # File paths never leave this machine. Source catalog contains hashes only.
        if str(path) not in seen:
            data = path.read_bytes()
            h = digest(data)
            key = f"sources/{h}"
            if key not in existing:
                if used + len(data) > budget:
                    raise ValueError("R2 storage safety budget exceeded; source channel unchanged")
                store.put(store.sources, key, data, True)
                existing[key] = len(data)
                used += len(data)
                uploaded += 1
            seen[str(path)] = h
        rows[logical] = {"source": seen[str(path)], "mode": row["mode"], "trim": bool(row["trim"])}
    catalog = {"format": 1, "policy": POLICY, "content_hash": document["content_hash"], "assets": rows}
    data = encoded(catalog)
    if len(data) > MAX_CATALOG:
        raise ValueError("Source catalog exceeds limit")
    store.put(store.sources, f"channels/{channel(name)}.json", data)
    print(json.dumps({"source_textures": len(rows), "new_originals": uploaded, "source_catalog_sha256": digest(data)}))


def build(store, name, signing_key, budget=8 << 30):
    source = store.get(store.sources, f"channels/{channel(name)}.json")
    if source is None:
        raise ValueError("Upload a source catalog first")
    data = json.loads(source)
    if data.get("format") != 1 or data.get("policy") != POLICY or len(data["assets"]) > 200000:
        raise ValueError("Unsupported source catalog")
    private = serialization.load_pem_private_key(signing_key.encode(), password=None)
    if not isinstance(private, Ed25519PrivateKey):
        raise ValueError("Expected an Ed25519 signing key")
    existing = store.objects(store.output)
    used = sum(existing.values()) + sum(store.objects(store.sources).values())
    index_bytes = store.get(store.output, f"indexes/{name}.json")
    previous = json.loads(index_bytes) if index_bytes else {}
    index, assets, generated, reused = {}, {}, 0, 0
    for logical, row in sorted(data["assets"].items()):
        if not LOGICAL.fullmatch(logical) or ".." in logical or not HASH.fullmatch(row["source"]):
            raise ValueError("Invalid source catalog row")
        fingerprint = digest(encoded([POLICY, row]))
        asset = index.get(fingerprint, previous.get(fingerprint))
        if asset and existing.get(f"objects/{asset['sha256']}.webp") == asset["bytes"]:
            reused += 1
        else:
            original = store.get(store.sources, f"sources/{row['source']}", MAX_SOURCE)
            if original is None or digest(original) != row["source"]:
                raise ValueError("Missing or corrupt original; published catalog unchanged")
            image = render(original, row["mode"], row["trim"])
            asset = {"sha256": digest(image), "bytes": len(image), "width": 2048, "height": 2048}
            key = f"objects/{asset['sha256']}.webp"
            if key not in existing:
                if used + len(image) > budget:
                    raise ValueError("R2 storage safety budget exceeded; published catalog unchanged")
                store.put(store.output, key, image, True)
                existing[key] = len(image)
                used += len(image)
            generated += 1
        index[fingerprint] = asset
        assets[logical] = asset
    payload = encoded({"format": 1, "policy": POLICY, "content_hash": data["content_hash"], "assets": assets})
    envelope = encoded({"payload": base64.b64encode(payload).decode(), "signature": base64.b64encode(private.sign(payload)).decode()})
    if len(envelope) > 48 << 20:
        raise ValueError("Signed catalog exceeds client limit")
    # Publication is the last write. An interrupted run cannot expose a partial catalog.
    revision = digest(payload)
    store.put(store.output, f"catalogs/{revision}.json", envelope, True)
    store.put(store.output, f"indexes/{name}.json", encoded(index))
    old = store.get(store.output, f"channels/{name}.json", 48 << 20)
    if old and old != envelope:
        store.put(store.output, f"channels/{name}-previous.json", old)
    store.put(store.output, f"channels/{name}.json", envelope)
    report = {"logical_textures": len(assets), "generated": generated, "reused": reused,
              "unique_hd_bytes": sum(existing.get(k, 0) for k in {f"objects/{a['sha256']}.webp" for a in assets.values()}),
              "combined_bucket_bytes": used, "revision": revision, "channel": name}
    print(json.dumps(report, indent=2))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write("## Verified HD artwork publication\n\n```json\n" + json.dumps(report, indent=2) + "\n```\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["sources", "build", "keygen"])
    parser.add_argument("--account", default=os.environ.get("R2_ACCOUNT_ID"))
    parser.add_argument("--sources-bucket", default=os.environ.get("R2_SOURCE_BUCKET", "stellartcg-hd-source"))
    parser.add_argument("--output-bucket", default=os.environ.get("R2_OUTPUT_BUCKET", "stellartcg-hd"))
    parser.add_argument("--channel", default="preview", type=channel)
    parser.add_argument("--source-export")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--maximum-storage-gib", type=float, default=8)
    args = parser.parse_args()
    if args.operation == "keygen":
        private = Ed25519PrivateKey.generate()
        directory = Path(args.source_export or "private")
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "signing-key.pem").open("xb") as f:
            f.write(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        (directory / "public-key.txt").write_text(base64.b64encode(private.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode())
        print("Created private signing key and public key; keep private directory out of Git")
        return
    if not 0.05 <= args.maximum_storage_gib <= 1024 or args.limit < 0:
        raise ValueError("Invalid storage or sample limit")
    store = R2(args.account, args.sources_bucket, args.output_bucket)
    budget = int(args.maximum_storage_gib * (1 << 30))
    if args.operation == "sources":
        publish_sources(store, args.source_export, args.channel, args.limit, budget)
    else:
        build(store, args.channel, os.environ["HD_SIGNING_KEY"], budget)


if __name__ == "__main__":
    main()
