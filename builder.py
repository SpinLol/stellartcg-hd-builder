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
import urllib.request
import urllib.parse
import urllib.error
import zipfile
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from PIL import Image, ImageOps
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

MAX_SOURCE = 32 * 1024 * 1024
MAX_CATALOG = 32 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 16_777_216
POLICY = "native-hd-max2048-webp95-no-upscale-v2"
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
    maximum = min(2048, max(image.size))
    if mode == "contain":
        scale = min(1, 2048 / image.width, 2048 / image.height)
        size = (max(1, int(image.width * scale + .5)), max(1, int(image.height * scale + .5)))
        resized = image.resize(size, Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (maximum, maximum))
        canvas.paste(resized, ((maximum - size[0]) // 2, (maximum - size[1]) // 2))
    elif mode in ("fill", "stretch"):
        # Physical product UVs already encode proportions; preserve the server's face policy.
        scale = min(1, 2048 / image.width, 2048 / image.height)
        canvas = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))), Image.Resampling.LANCZOS)
    else:
        raise ValueError("Unknown layout mode")
    output = io.BytesIO()
    canvas.save(output, "WEBP", quality=95, method=6, exact=True)
    return output.getvalue()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("Artwork providers must not redirect downloads")


def remote(url, maximum=MAX_SOURCE):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"images.pokemontcg.io", "api.tcgdex.net", "assets.tcgdex.net"} or parsed.port or parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise ValueError("Untrusted high-resolution artwork provider")
    request = urllib.request.Request(url, headers={"User-Agent": "StellarTCG-ArtworkPublisher/2", "Accept": "application/json,image/webp,image/png"})
    for attempt in range(3):
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
                if int(response.headers.get("Content-Length", "0")) > maximum:
                    raise ValueError("Provider image exceeds limit")
                result = response.read(maximum + 1)
            break
        except urllib.error.HTTPError as failure:
            if failure.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
            time.sleep(2 ** attempt)
        except (TimeoutError, urllib.error.URLError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    if len(result) > maximum:
        raise ValueError("Provider image exceeds limit")
    return result


def printing_url(printing):
    identifier = printing.get("source_card_id", "")
    source = printing.get("metadata_source", "")
    image = printing.get("image_source", "")
    if image.startswith("https://images.pokemontcg.io/"):
        return image if "_hires." in image else image.rsplit(".", 1)[0] + "_hires.png"
    if image.startswith("https://assets.tcgdex.net/"):
        return re.sub(r"/(?:low|high)\.(?:png|webp|jpg)$", "/high.webp", image)
    if source.startswith("https://raw.githubusercontent.com/PokemonTCG/pokemon-tcg-data/") and re.fullmatch(r"[a-z0-9.]+-[A-Za-z0-9]+", identifier):
        set_id, number = identifier.rsplit("-", 1)
        if source.endswith("/" + set_id + ".json"):
            return f"https://images.pokemontcg.io/{set_id}/{number}_hires.png"
    if source.startswith("https://api.tcgdex.net/v2/en/cards/") and source.rsplit("/", 1)[-1] == identifier:
        return source
    return None


def artwork_signature(data):
    with Image.open(io.BytesIO(data)) as raw:
        if raw.width * raw.height > Image.MAX_IMAGE_PIXELS or getattr(raw, "n_frames", 1) != 1:
            raise ValueError("Unsafe reference image")
        image = ImageOps.exif_transpose(raw).convert("RGBA")
        bounds = image.getchannel("A").getbbox()
        if bounds:
            image = image.crop(bounds)
        image = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        value = sum((1 << (y * 8 + x)) for y in range(8) for x in range(8) if image.getpixel((x, y)) > image.getpixel((x+1, y)))
        return f"{value:016x}", max(raw.size)


def alternate_printing(printing):
    """Known equivalent provider set IDs, never a fuzzy search for another printing."""
    identifier = printing.get("source_card_id", "")
    if not re.fullmatch(r"me(?:1|2|2pt5|3|4|5)-[0-9]+", identifier):
        return None
    set_id, number = identifier.rsplit("-", 1)
    sets = {"me1": "me01", "me2": "me02", "me2pt5": "me02.5", "me3": "me03", "me4": "me04", "me5": "me05"}
    name = printing.get("name", "").strip()
    if not name:
        return None
    equivalent = sets[set_id] + "-" + number.zfill(3)
    return {"url": "https://api.tcgdex.net/v2/en/cards/" + equivalent, "id": equivalent, "name": name}


def high_resolution(row):
    url = row.get("remote")
    if not url:
        return None
    def scan(address, identifier, name=None):
        if address.startswith("https://api.tcgdex.net/"):
            details = json.loads(remote(address, 1 << 20))
            if details.get("id") != identifier or name and details.get("name", "").strip().casefold() != name.strip().casefold():
                raise ValueError("Provider printing ID or name mismatch")
            image = details.get("image") or ""
            if not image:
                raise ValueError("Provider has no image for this printing")
            if not image.startswith("https://assets.tcgdex.net/"):
                raise ValueError("Untrusted provider image URL")
            address = image + "/high.webp"
        return remote(address)
    try:
        data = scan(url, row["printing_id"])
    except urllib.error.HTTPError as missing:
        alternative = row.get("alternate")
        if missing.code != 404 or not alternative:
            raise
        data = scan(alternative["url"], alternative["id"], alternative["name"])
    signature, dimension = artwork_signature(data)
    if (int(signature, 16) ^ int(row["reference_dhash"], 16)).bit_count() > 16:
        raise ValueError("Higher-resolution scan does not match the configured artwork")
    if dimension <= row.get("minimum_dimension", 256):
        return None
    return data


def source_bytes(path, row):
    if row.get("archive"):
        with zipfile.ZipFile(path) as archive:
            if archive.getinfo(row["entry"]).file_size > MAX_SOURCE:
                raise ValueError("Source face exceeds byte limit")
            return archive.read(row["entry"])
    if path.stat().st_size > MAX_SOURCE:
        raise ValueError("Original exceeds byte limit")
    return path.read_bytes()


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

    def snapshot(self, bucket, key):
        from botocore.exceptions import ClientError
        try:
            r = self.s3.get_object(Bucket=bucket, Key=key)
        except ClientError as error:
            if error.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None, None
            raise
        with r["Body"] as stream:
            if r["ContentLength"] > MAX_CATALOG:
                raise ValueError("Source catalog exceeds limit")
            data = stream.read(MAX_CATALOG + 1)
        if len(data) > MAX_CATALOG:
            raise ValueError("Source catalog exceeds limit")
        return data, r["ETag"]

    def conditional(self, bucket, key, data, etag):
        from botocore.exceptions import ClientError
        try:
            self.s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType="application/json",
                CacheControl="no-cache, max-age=0, must-revalidate",
                **({"IfMatch": etag} if etag else {"IfNoneMatch": "*"}))
            return True
        except ClientError as error:
            if error.response["ResponseMetadata"]["HTTPStatusCode"] in (409, 412):
                return False
            raise

    def objects(self, bucket):
        result = {}
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            for item in page.get("Contents", []):
                result[item["Key"]] = item["Size"]
        return result


def publish_sources(store, file, name, limit=0, budget=8 << 30, content_root=None):
    """Stream unchanged originals directly to a PRIVATE bucket; no HD encoding here."""
    document = json.loads(Path(file).read_text(encoding="utf-8-sig"))
    if document.get("format") != 1 or len(document["assets"]) > 200000:
        raise ValueError("Invalid source export")
    if limit and name == "stable":
        raise ValueError("A partial sample must use a preview channel")
    existing = store.objects(store.sources)
    used = sum(existing.values()) + sum(store.objects(store.output).values())
    rows, seen, uploaded = {}, {}, 0
    root = Path(content_root or document.get("content_root", Path(file).parent)).resolve()
    for logical, row in sorted(document["assets"].items()):
        if not LOGICAL.fullmatch(logical) or ".." in logical:
            raise ValueError("Invalid logical ID")
        if limit and len(rows) >= limit:
            break
        path = (root / row.get("file", row.get("archive", ""))).resolve()
        if not path.is_relative_to(root) or path.is_symlink() or not path.is_file():
            raise ValueError("Invalid original source file")
        # File paths never leave this machine. Source catalog contains hashes only.
        identity = str(path) + "|" + row.get("entry", "")
        data = None
        if identity not in seen:
            data = source_bytes(path, row)
            h = digest(data)
            signature, dimension = artwork_signature(data)
            seen[identity] = (h, signature, dimension)
        h, signature, dimension = seen[identity]
        minimum = row.get("minimum_dimension", 256)
        url = printing_url(row.get("printing", {}))
        uploaded_hash = ""
        if dimension > minimum:
            key = f"sources/{h}"
            if key not in existing:
                if data is None:
                    data = source_bytes(path, row)
                if digest(data) != h:
                    raise ValueError("Original changed during upload; source channel unchanged")
                if used + len(data) > budget:
                    raise ValueError("R2 storage safety budget exceeded; source channel unchanged")
                store.put(store.sources, key, data, True)
                existing[key] = len(data)
                used += len(data)
                uploaded += 1
            uploaded_hash = h
        item = {"source": uploaded_hash, "original_sha256": h, "mode": row["mode"], "trim": bool(row["trim"]), "minimum_dimension": minimum}
        if url:
            item.update(remote=url, printing_id=row["printing"]["source_card_id"], reference_dhash=signature)
            alternative = alternate_printing(row["printing"])
            if alternative:
                item["alternate"] = alternative
        rows[logical] = item
        if len(rows) % 500 == 0:
            print(json.dumps({"phase": "source_metadata", "processed": len(rows), "total": len(document["assets"]), "new_originals": uploaded}), flush=True)
    key = f"channels/{channel(name)}.json"
    for attempt in range(5):
        old, etag = store.snapshot(store.sources, key)
        previous = json.loads(old) if old else {"assets": {}}
        # Explicit custom uploads survive future official-source exports, including changed logical IDs.
        custom = {r["admin_upload"]["card"]: r for r in previous["assets"].values()
                  if r.get("admin_upload", {}).get("card")}
        merged = dict(rows)
        for logical, previous_row in previous["assets"].items():
            if previous_row.get("admin_upload") and logical not in merged:
                merged[logical] = previous_row  # An older export cannot silently discard a new custom set.
        for logical, row in rows.items():
            manual = previous["assets"].get(logical, {})
            card = document["assets"].get(logical, {}).get("custom_card")
            if card in custom:
                manual = custom[card]
            if manual.get("admin_upload"):
                merged[logical] = dict(manual, minimum_dimension=row["minimum_dimension"])
        catalog = {"format": 1, "policy": POLICY, "content_hash": document["content_hash"], "assets": merged}
        data = encoded(catalog)
        if len(data) > MAX_CATALOG:
            raise ValueError("Source catalog exceeds limit")
        if store.conditional(store.sources, key, data, etag):
            break
    else:
        raise ValueError("Concurrent HD inbox updates; source export retained for retry")
    print(json.dumps({"source_textures": len(rows), "new_originals": uploaded, "source_catalog_sha256": digest(data)}))


def prepare_asset(store, row):
    """One bounded worker: fetch and encode in RAM; the coordinator alone publishes."""
    # The server uploads a bounded original reference, never decodes or fetches HD.
    # Compute precisely the same Pillow fingerprint used by the workstation publisher.
    if row.get("printing"):
        row = dict(row)
        printing = row["printing"]
        url = printing_url(printing)
        if url:
            reference_hash = row.get("reference_source", "")
            if not HASH.fullmatch(reference_hash):
                raise ValueError("Invalid original fingerprint reference")
            reference = store.get(store.sources, f"sources/{reference_hash}", MAX_SOURCE)
            if reference is None or digest(reference) != reference_hash:
                raise ValueError("Missing or corrupt fingerprint reference; published HD unchanged")
            row.update(remote=url, printing_id=printing["source_card_id"],
                       reference_dhash=artwork_signature(reference)[0])
            alternative = alternate_printing(printing)
            if alternative:
                row["alternate"] = alternative
    original = None
    if row.get("remote"):
        try:
            original = high_resolution(row)
        except Exception as failure:
            print(json.dumps({"printing": row.get("printing_id", ""), "standard_fallback_reason": str(failure)[:180]}), flush=True)
    if original is None and row.get("source"):
        original = store.get(store.sources, f"sources/{row['source']}", MAX_SOURCE)
        if original is None or digest(original) != row["source"]:
            raise ValueError("Missing or corrupt original; published catalog unchanged")
    if original is None or artwork_signature(original)[1] <= row.get("minimum_dimension", 256):
        return {"skipped": True}, None
    image = render(original, row["mode"], row["trim"])
    with Image.open(io.BytesIO(image)) as rendered:
        dimensions = rendered.size
    return {"sha256": digest(image), "bytes": len(image), "width": dimensions[0], "height": dimensions[1]}, image


def build(store, name, signing_key, budget=8 << 30, retry_missing=False, workers=4, only_if_changed=False):
    source = store.get(store.sources, f"channels/{channel(name)}.json")
    if source is None:
        raise ValueError("Upload a source catalog first")
    data = json.loads(source)
    if data.get("format") != 1 or data.get("policy") != POLICY or len(data["assets"]) > 200000:
        raise ValueError("Unsupported source catalog")
    source_revision = digest(source)
    if only_if_changed and not retry_missing:
        completed = store.get(store.output, f"builds/{name}.json", 1 << 20)
        if completed and json.loads(completed).get("source_revision") == source_revision:
            report = {"channel": name, "unchanged": True, "source_revision": source_revision}
            print(json.dumps(report))
            return report
    private = serialization.load_pem_private_key(signing_key.encode(), password=None)
    if not isinstance(private, Ed25519PrivateKey):
        raise ValueError("Expected an Ed25519 signing key")
    existing = store.objects(store.output)
    used = sum(existing.values()) + sum(store.objects(store.sources).values())
    index_bytes = store.get(store.output, f"indexes/{name}.json")
    previous = json.loads(index_bytes) if index_bytes else {}
    if not 1 <= workers <= 4:
        raise ValueError("HD worker count must be 1..4")
    index, assets, generated, reused, skipped, processed, checkpoint = {}, {}, 0, 0, 0, 0, 0
    groups = {}
    for logical, row in sorted(data["assets"].items()):
        if not LOGICAL.fullmatch(logical) or ".." in logical or row.get("source") and not HASH.fullmatch(row["source"]):
            raise ValueError("Invalid source catalog row")
        fingerprint = digest(encoded([POLICY, row]))
        if fingerprint not in groups:
            groups[fingerprint] = (row, [])
        groups[fingerprint][1].append(logical)

    def commit_result(fingerprint, logicals, asset, image):
        nonlocal used, generated, reused, skipped, processed, checkpoint
        if asset.get("skipped"):
            skipped += len(logicals)
        else:
            key = f"objects/{asset['sha256']}.webp"
            if image is not None and key not in existing:
                if used + len(image) > budget:
                    raise ValueError("R2 storage safety budget exceeded; published catalog unchanged")
                store.put(store.output, key, image, True)
                existing[key] = len(image)
                used += len(image)
            if image is not None:
                generated += 1
                reused += len(logicals) - 1
            else:
                reused += len(logicals)
            for logical in logicals:
                assets[logical] = asset
        index[fingerprint] = asset
        processed += len(logicals)
        if processed - checkpoint >= 100:
            store.put(store.output, f"indexes/{name}.json", encoded({**previous, **index}))
            checkpoint = processed
            print(json.dumps({"phase": "native_hd_scans", "processed": processed, "total": len(data["assets"]), "generated": generated, "reused": reused, "standard_fallbacks": skipped}), flush=True)

    queue = []
    for fingerprint, (row, logicals) in groups.items():
        asset = previous.get(fingerprint)
        if asset and asset.get("skipped") and not retry_missing:
            commit_result(fingerprint, logicals, asset, None)
        elif asset and not asset.get("skipped") and existing.get(f"objects/{asset['sha256']}.webp") == asset["bytes"]:
            commit_result(fingerprint, logicals, asset, None)
        else:
            queue.append((fingerprint, row, logicals))
    # At most four in-flight image buffers; never submit the whole catalog as image futures.
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="native-hd")
    iterator, pending = iter(queue), {}
    def submit():
        item = next(iterator, None)
        if item is not None:
            fingerprint, row, logicals = item
            pending[executor.submit(prepare_asset, store, row)] = (fingerprint, logicals)
    try:
        for unused in range(workers): submit()
        while pending:
            completed, unused = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                fingerprint, logicals = pending.pop(future)
                asset, image = future.result()
                commit_result(fingerprint, logicals, asset, image)
                submit()
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
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
    # Written after atomic publication only: interrupted builds remain retryable.
    store.put(store.output, f"builds/{name}.json", encoded({"source_revision": source_revision, "revision": revision}))
    report = {"logical_textures": len(assets), "generated": generated, "reused": reused, "standard_fallbacks": skipped,
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
    parser.add_argument("--content-root", help="Override the exported server config root, for a copied content directory")
    parser.add_argument("--retry-missing", action="store_true", help="Recheck provider scans that were unavailable or rejected previously")
    parser.add_argument("--only-if-changed", action="store_true", help="Skip scheduled checks when the source channel was already published successfully")
    parser.add_argument("--workers", type=int, default=4, choices=range(1,5))
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
        publish_sources(store, args.source_export, args.channel, args.limit, budget, args.content_root)
    else:
        build(store, args.channel, os.environ["HD_SIGNING_KEY"], budget, args.retry_missing, args.workers, args.only_if_changed)


if __name__ == "__main__":
    main()
