# StellarTCG optional HD artwork

Every server owns its Cloudflare account, buckets, domain, GitHub repository and signing key.
There is no shared Stellar download service, tracking service or embedded credential.

## Data flow

1. Export logical texture IDs and original file references with `/tcg admin hdartwork export` (permission `stellartcg.admin.ops`). This works with the released server mod; its source code is not required.
2. On an operator workstation, `builder.py sources` uploads original images directly to a **private** R2 source bucket. No HD image is generated in this step. File paths and server data are not uploaded.
3. A manually dispatched GitHub Actions workflow fetches higher-resolution scans of the exact configured printings from Pokémon TCG or TCGdex, checks their visual fingerprint against the existing art, and encodes WebP quality 95 at the scan's native resolution, capped at 2048. **Images are never enlarged.** Cards without an available matching higher-resolution scan keep standard artwork. Authored variant artwork is not replaced with another printing. Changed custom originals can also be used when already higher resolution. The workflow uses at most four image workers in memory. No large archive, Actions artifact or Actions cache is created.
4. Once all objects exist, a signed catalog is published atomically. Interrupted builds leave the last published catalog usable. Progress checkpoints allow re-running to reuse completed work as well as unchanged objects from earlier publications. Removed logical textures disappear from the next catalog; historical blobs are retained for rollback rather than silently deleted.
5. The Minecraft server advertises only the HTTPS catalog URL, public verification key and limits. **It never downloads, generates, stages or stores HD files.**
6. Engine 1.71.0 defaults to Standard without interrupting joining players. Players switch to HD or back to Standard with `/tcgartwork`; existing saved preferences remain effective. Engine verifies the signed catalog and immediately installs the complete available HD library directly from your domain using at most four image workers, checking SHA-256. Standard artwork stays active until the download is complete. The saved preference and verified files are reused on later joins; only missing or changed images are fetched. Disk limits still apply to the whole installation, while decoded CPU/GPU textures remain bounded and load only when viewed. Both download progress displays use grayscale card backs that fill with color according to the transfer percentage.

For example, a 733×1024 scan becomes a 1024-square canvas with transparent side borders, preserving its original pixels. It does not become an invented 2048 scan. Known equivalent Mega Evolution set IDs can fall back to TCGdex when the first provider lacks a scan; both the exact card name and visual fingerprint must match. A native 600×825 scan remains at that resolution. Product faces preserve the current UV/layout policy and upgrade only when an actually higher-resolution original is available. Automatically fitted product photos keep the already flattened face so HD does not undo perspective corrections. Card effects remain client GPU effects, not animated texture sheets. Provider requests allow only specific HTTPS artwork hosts, reject redirects and byte/pixel overflows, and validate both printing IDs and a visual fingerprint. Missing or mismatched scans are counted in `standard_fallbacks`; the workflow has a **retry missing scans** option.

## Set up your own deployment

- Fork or copy this standalone directory into **your own** GitHub repository. A public recipe repository can use standard GitHub-hosted runners without Actions minute charges; do not publish originals, credentials or private mod source in it.
- Create two R2 **Standard** buckets, for example `my-tcg-originals` (private, no public domain) and `my-tcg-hd` (finished artwork only). Connect an unused domain such as `cards.example.com` to the finished bucket; require TLS 1.2 or newer. Leave `r2.dev` public access disabled.
- Create an R2 Object Read & Write token scoped to these two buckets only. It does not need DNS, account administration, billing, Workers, or other-bucket access.
- Create an Ed25519 key using `python builder.py keygen --source-export private`. Back up the private key securely; never commit it.
- Configure repository **variables** `R2_ACCOUNT_ID`, `R2_SOURCE_BUCKET`, `R2_OUTPUT_BUCKET` and **secrets** `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `HD_SIGNING_KEY` (the PEM contents).
- Use Python 3.12 with `pip install -r requirements.txt` on your operator workstation. Set AWS credential environment variables for the R2 token. For a private repository, check your Actions allowance before a full build.

## Publish/update artwork

### Automatic updates (StellarTCG 1.51.0+)

Configure your own R2 credentials using `hd-upload.json` and `hd-upload-secrets.json` below.
Set `enabled: true` and `auto_sync_content: true`. Once new cards/products are loaded
by a successful content reload or restart and the standard artwork publication finishes,
the mod automatically synchronizes the current logical texture catalog and required
original images to your private source bucket. No export command, Python installation
or manual source upload is needed on the server.

Update your builder repository with this version of `builder.py`, its tests, and
`.github/workflows/hd-artwork.yml`. The workflow checks the configured stable channel
every 30 minutes and only builds a source revision that has not already been published.
Builder recipe or test updates on `main` also run verification and the same incremental check.
GitHub may delay scheduled runs. Configure repository variables `HD_AUTO_CHANNEL`
(default `stable`) and `HD_MAXIMUM_STORAGE_GIB` (default `8`) if needed. GitHub can disable
scheduled workflows on inactive public repositories; its Actions page allows re-enabling
them. For prompt publication without depending on a schedule, enable `dispatch_workflow`
and configure the optional repository-scoped Actions-write token.

The default 30-second content debounce batches rapid edits. Originals stream one at a time
through bounded RAM; fitted product faces are read from the existing standard ZIP chunks
without extracting anything. The cloud computes reference fingerprints, finds exact
official scans, encodes HD and signs the completed catalog. The Minecraft server never
downloads, upscales, renders or stores HD outputs. Its `hd-sync-state.json` contains only
file stamps, hashes and pending-build metadata. Regular content files are never deleted.
Only verified custom inbox originals follow the existing inbox-removal policy.

Unchanged source objects are reused, custom inbox overrides survive official catalog
refreshes, interrupted uploads preserve the last usable HD catalog, and pending immediate
build dispatches survive restarts. A single synchronization can upload at most
`maximum_sync_upload_mib` (default 2048 MiB) of new originals; verified partial uploads are
reused by retries. This is a per-sync safety limit; the cloud workflow still enforces its
separate total bucket budget. Existing HD clients detect new catalogs automatically and
download only missing/changed images; standard artwork remains available meanwhile.

Commands (`stellartcg.admin.ops`):

- `/tcg admin hdartwork status`: synchronization / publication status.
- `/tcg admin hdartwork sync`: request a source synchronization immediately.
- `/tcg admin hdartwork upload`: process inbox artwork and pending content changes.

Set `auto_sync_content: false` to retain manual source publishing. The standalone
workstation export procedure below remains supported.

Run `/tcg admin hdartwork export` in game or in your server console. It writes **metadata only** to `config/stellartcg/hd-source-export.json`; there are no new image files and no resource-pack rebuild. Use the standalone Python publisher on a workstation with a copy of your TCG configuration, or run only its `sources` operation on the server to stream existing sources directly to R2. The `sources` operation never fetches high-resolution scans or renders images. High-resolution retrieval and generation happen exclusively in GitHub Actions.

If your copied configuration is in a different location, pass `--content-root PATH_TO_COPIED_CONFIG_STELLARTCG` to the publisher. Normal fitted faces are read directly from the existing pack/shared ZIP chunks; no extracted image directory is created.

Developers can alternatively export from a source workspace (Java 21), with an existing output directory outside the server directory:

```powershell
.\gradlew.bat exportHdArtwork '-PtcgContentRoot=E:/path/to/server/config/stellartcg' '-PtcgHdOutput=E:/operator-work/hd/source-export.json'
```

The export validates the content and reads the existing normal pack. It never rebuilds that pack, downloads scans or generates textures.

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

Use `/tcg admin hdartwork reload` (permission `stellartcg.admin.ops`) to advertise changes **without rebuilding or reloading the normal resource pack**. Engine 1.71.0+ uses `/tcgartwork` for manual selection and complete-library installation, with no join-time popup. Versions 1.69.1–1.70.0 offered first-join selection; 1.69.0 used downloads when viewing. Older clients retain their standard artwork. Each player must opt in independently. The base pack remains necessary for models and images without a matching HD scan. Set `client_disk_cache_mib` large enough for the complete available HD library; the download screen reports required space when the configured limit is too low.

## Optional custom-set upload inbox (TCG 1.49.0)

Official scan lookup is unchanged. For a custom set only, an administrator can create
`content/sets/<set-folder>/hd-artwork/` and place `001.png`, `002.jpg`, or `003.webp` inside it.
The filename must match the card's local JSON `id` exactly, including leading zeros.
The mod never creates these folders. Standard card fronts must remain outside this inbox.
Use one static, genuinely higher-resolution front image per card; no enlargement is performed.
The replacement covers that card's variants which use the same base front, preserving their finishes.
Separate artwork assigned to a variant keeps its existing behavior.

Configure `config/stellartcg/hd-upload.json` for **your own** private R2 source bucket:

```json
{
  "enabled": true,
  "account_id": "YOUR_32_CHARACTER_CLOUDFLARE_ACCOUNT_ID",
  "source_bucket": "your-private-hd-source-bucket",
  "channel": "stable",
  "poll_seconds": 60,
  "quiet_seconds": 20,
  "maximum_upload_mib": 32,
  "files_per_batch": 20,
  "dispatch_workflow": false,
  "auto_sync_content": true,
  "sync_debounce_seconds": 30,
  "maximum_sync_upload_mib": 2048,
  "github_repository": "YOUR_NAME/YOUR_HD_BUILDER_REPOSITORY",
  "github_workflow": "hd-artwork.yml",
  "github_ref": "main"
}
```

Keep credentials in `hd-upload-secrets.json` beside that config, **outside `content/`**:

```json
{
  "AWS_ACCESS_KEY_ID": "YOUR_BUCKET_SCOPED_ACCESS_KEY",
  "AWS_SECRET_ACCESS_KEY": "YOUR_BUCKET_SCOPED_SECRET",
  "GITHUB_TOKEN": "OPTIONAL_FINE_GRAINED_ACTIONS_WRITE_TOKEN"
}
```

Alternatively use `STELLARTCG_R2_ACCESS_KEY_ID`, `STELLARTCG_R2_SECRET_ACCESS_KEY` and
`STELLARTCG_HD_GITHUB_TOKEN` environment variables. Restrict the R2 token to your private source
bucket and the GitHub token to Actions write access on your builder repository. These secrets
are never sent to players or included in generated packs; protect them and exclude them from
shared configuration exports. The existing Actions signing key remains in GitHub Secrets.

The mod checks only admin-created inboxes, waits for files to settle, validates each static
image in bounded RAM, uploads its original bytes, downloads the remote object to verify its
full SHA-256, conditionally merges the private source catalog, and verifies the committed
mapping. Only then does it remove the corresponding local original. Files that change,
fail, are too small, conflict or have unknown IDs are retained. Tiny metadata receipts in
`hd-upload-receipts/` allow safe recovery after interruption; they contain no images or secrets.
Keep an independent backup of your custom originals. No server HD pack or persistent image
cache is created, and this process does not rebuild the normal resource pack.

- `/tcg admin hdartwork upload` checks the inbox immediately, using the same quiet-file safeguard.
- `/tcg admin hdartwork status` shows the upload status.
- Both use `stellartcg.admin.ops` (or operator access).
- Config changes are picked up automatically on the next poll or upload command.

The uploaded original is stored safely before local deletion, but appears in players' HD
library **after the GitHub cloud workflow publishes it**. With `dispatch_workflow: false`,
run **Build HD artwork** manually for the configured channel. Set it to `true` and configure
the optional GitHub token to queue the build after an upload batch. A failed dispatch leaves
the originals safely in R2 and reports that the workflow must be run manually. Update the
operator's `builder.py` when using source exports so future official uploads preserve custom
overrides and concurrent inbox changes.

Engine 1.69.3 also redraws standard and custom grading-company slab frames, edges and printed
labels at higher resolution locally when the player's HD library is active. It preserves
the existing design and transparent card window. This adds no slab images to R2 or the server
pack. Standard-mode players retain the existing slab textures. Generated slab textures share
the existing bounded visible-artwork cache and are generated off the render thread.

## Storage, cost and recovery

### Optional HD product previews and 3D faces

TCG 1.52.0 and Engine 1.72.0 extend the same inbox to product artwork. See
`HD_CUSTOM_ARTWORK.md` in the mod distribution for full examples. Place images in
`content/sets/<set>/hd-artwork/products/<product-id>/` using `main`, `front`, `back`,
`sides` (or `left` and `right`), `top`, `bottom`, and `model` for complete UV atlases.
An optional `wrapper-2/` subfolder addresses the second wrapper.

Source exports contain `product_uploads` metadata templates, not extra default images.
The builder preserves explicit uploads by product ID, wrapper and face across future
source refreshes. Product faces use independent aliases, so shared standard image hashes
cannot accidentally replace other faces or products. There is no separate R2 setup,
new workflow input or product-specific credential. The normal signed, incremental WebP95
cloud build publishes these uploads alongside cards.

The workflow's default safety budget is **8 GiB combined across the two configured buckets**, including existing historical files. This is a software guard, not an account-wide billing cap. Set it deliberately before expanding the library. R2's free allocation is shared with other buckets and includes request limits. Source uploads and unchanged builds reuse data, but storage and requests beyond your account allowance can still cost money. Existing remote scans are reused through the build index; unavailable printings are not constantly re-fetched unless the admin requests a retry. There is no synthetic activity, scheduled keepalive or full-library client redownload.

Use a separate `preview` channel for experiments. Stable builds cannot upload a deliberately partial source sample. `channels/stable-previous.json` retains the previous signed catalog; set that URL and reload discovery to roll back. Signing keys and original images should be backed up independently. R2 is persistent storage, not the only backup.

For reclaiming historical objects, first identify all hashes referenced by your active, previous, preview catalogs and indexes, then perform a reviewed cleanup. Automatic destructive cloud garbage collection is intentionally excluded. No lifecycle rule should delete active `objects/` or private `sources/` automatically.

Client downloads reject HTTP, redirects, credentials in URLs, invalid signatures, oversize assets and invalid hashes. A failed download backs off and retains standard artwork. Cached HD is bounded separately from normal pack downloads. GPU uploads remain bounded; large collections do not decode every HD card at once.
