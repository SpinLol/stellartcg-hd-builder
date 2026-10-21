# StellarTCG optional HD artwork

Every server owns its Cloudflare account, buckets, domain, GitHub repository and signing key.
There is no shared Stellar download service, tracking service or embedded credential.

## Data flow

1. Export logical texture IDs and original file references with `exportHdArtwork`.
2. On an operator workstation, `builder.py sources` uploads original images directly to a **private** R2 source bucket. No HD image is generated in this step. File paths and server data are not uploaded.
3. A manually dispatched GitHub Actions workflow downloads changed originals, generates 2048-square WebP quality 95, and uploads immutable hash-named objects to your public output bucket. It processes one image at a time in memory. No large archive, Actions artifact or Actions cache is created.
4. Once all objects exist, a signed catalog is published atomically. Interrupted builds leave the last published catalog usable. Re-running reuses the previous index and unchanged objects. Removed logical textures disappear from the next catalog; historical blobs are retained for rollback rather than silently deleted.
5. The Minecraft server advertises only the HTTPS catalog URL, public verification key and limits. **It never downloads, generates, stages or stores HD files.**
6. Players opt in using `/tcgartwork`. Engine verifies the signed catalog, fetches only viewed textures directly from your domain, checks SHA-256, and uses a bounded disk cache plus the existing bounded GPU cache. Standard artwork remains the fallback.

2048 pixels improves sampling of existing high-resolution originals. Upscaling a small original cannot recover missing text or artwork detail. Product faces preserve the current UV/layout policy. Automatically fitted product photos may use the already flattened normal face as their source so HD does not undo perspective corrections. Card effects remain client GPU effects, not animated texture sheets.

## Set up your own deployment

- Fork or copy this standalone directory into **your own** GitHub repository. A public recipe repository can use standard GitHub-hosted runners without Actions minute charges; do not publish originals, credentials or private mod source in it.
- Create two R2 **Standard** buckets, for example `my-tcg-originals` (private, no public domain) and `my-tcg-hd` (finished artwork only). Connect an unused domain such as `cards.example.com` to the finished bucket; require TLS 1.2 or newer. Leave `r2.dev` public access disabled.
- Create an R2 Object Read & Write token scoped to these two buckets only. It does not need DNS, account administration, billing, Workers, or other-bucket access.
- Create an Ed25519 key using `python builder.py keygen --source-export private`. Back up the private key securely; never commit it.
- Configure repository **variables** `R2_ACCOUNT_ID`, `R2_SOURCE_BUCKET`, `R2_OUTPUT_BUCKET` and **secrets** `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `HD_SIGNING_KEY` (the PEM contents).
- Use Python 3.12 with `pip install -r requirements.txt` on your operator workstation. Set AWS credential environment variables for the R2 token. For a private repository, check your Actions allowance before a full build.

## Publish/update artwork

From the StellarTCG source workspace (Java 21), with an existing output directory outside the server directory:

```powershell
.\gradlew.bat exportHdArtwork '-PtcgContentRoot=E:/path/to/server/config/stellartcg' '-PtcgHdOutput=E:/operator-work/hd/source-export.json'
```

The export validates the content and reads the existing normal pack. It never rebuilds that pack. Fitted normal product faces are copied into the operator export directory, never back into the server.

Upload a small sample first:

```powershell
python builder.py sources --account YOUR_ACCOUNT_ID --sources-bucket YOUR_PRIVATE_BUCKET --output-bucket YOUR_FINISHED_BUCKET --source-export E:/operator-work/hd/source-export.json --channel preview --limit 12
```

Run **Build HD artwork** in your repository's Actions tab, selecting `preview`. Validate visuals and cache behavior before promoting a full source catalog:

```powershell
python builder.py sources --account YOUR_ACCOUNT_ID --sources-bucket YOUR_PRIVATE_BUCKET --output-bucket YOUR_FINISHED_BUCKET --source-export E:/operator-work/hd/source-export.json --channel stable
```

Dispatch `stable`. Configure `config/stellartcg/hd-artwork.json`:

```json
{
  "enabled": true,
  "catalog_url": "https://cards.example.com/channels/stable.json",
  "public_key": "CONTENTS_OF_PUBLIC_KEY_TXT",
  "client_disk_cache_mib": 2048,
  "maximum_asset_mib": 16
}
```

Use `/tcg admin hdartwork reload` (permission `stellartcg.admin.ops`) to advertise changes **without rebuilding or reloading the normal resource pack**. Engine 1.69.0+ is needed for optional HD; older clients retain their standard artwork. Each player must opt in independently.

## Storage, cost and recovery

The workflow's default safety budget is **8 GiB combined across the two configured buckets**, including existing historical files. This is a software guard, not an account-wide billing cap. Set it deliberately before expanding the library. R2's free allocation is shared with other buckets and includes request limits. Source uploads and unchanged builds reuse data, but storage and requests beyond your account allowance can still cost money. There is no synthetic activity, scheduled keepalive or full-library client redownload.

Use a separate `preview` channel for experiments. Stable builds cannot upload a deliberately partial source sample. `channels/stable-previous.json` retains the previous signed catalog; set that URL and reload discovery to roll back. Signing keys and original images should be backed up independently. R2 is persistent storage, not the only backup.

For reclaiming historical objects, first identify all hashes referenced by your active, previous, preview catalogs and indexes, then perform a reviewed cleanup. Automatic destructive cloud garbage collection is intentionally excluded. No lifecycle rule should delete active `objects/` or private `sources/` automatically.

Client downloads reject HTTP, redirects, credentials in URLs, invalid signatures, oversize assets and invalid hashes. A failed download backs off and retains standard artwork. Cached HD is bounded separately from normal pack downloads. GPU uploads remain bounded; large collections do not decode every HD card at once.
