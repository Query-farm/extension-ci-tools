# Query.Farm extension deploy

`.github/workflows/_query_farm_deploy.yml` signs the binaries produced by
`_extension_distribution.yml` and publishes them to the Query.Farm extension
repository: R2 bucket `query-farm-duckdb-extensions`, served at
`https://duckdb-extensions.query.farm`. The logic lives in
`scripts/query_farm/deploy.py` (unit tests: `scripts/query_farm/test_deploy.py`).

## What it does

1. **Context check.** Refuses `pull_request` / `pull_request_target` events.
   If `duckdb_version` is not a release label (`^v[0-9]+\.[0-9]+\.[0-9]+$`, e.g.
   `v2.0.0`), the deploy job is skipped with a notice. This keeps
   `v2.0-cyanoptera` and any other dev or alpha builds out of the repository.
2. **Download** every artifact named `<extension_name>-<duckdb_version>-extension-<arch>`
   from the current run and drop the architectures in `exclude_archs`. By default
   that is all wasm archs, because DuckDB-Wasm repository support is unverified.
3. **Footer checks** for each arch. The deploy fails if the footer magic is wrong,
   if the footer platform differs from the artifact's arch, or if the footer DuckDB
   version differs from `duckdb_version`. C API (`C_STRUCT`) extensions record the
   C API version there instead, so they skip that last check. All archs must also
   agree on the extension version.
4. **Sign** the same way as DuckDB's `extension-upload-single.sh`. The 256-byte
   signature slot is dropped, the rest is hashed (sha256 per 1 MiB chunk, then
   sha256 of the joined digests), the hash is signed with RSA/SHA-256 and the
   signature is appended.
5. **Verify** against the keys fetched live from
   `<repository_url>/.well-known/duckdb-extension-repo.json`. The deploy fails if
   the signing key's public key is not listed there. That catches a rotated secret
   whose key was never published.
6. **gzip -9** each signed file, upload the `.gz` files and `manifest.json` as the
   workflow artifact `<name>-<duckdb_version>-signed`, then upload to R2. With
   `dry_run`, it only prints each key, size and sha256.

| path | when | Cache-Control |
|---|---|---|
| `<name>/<extension_version>/<duckdb_version>/<platform>/<name>.duckdb_extension.gz` | `deploy_versioned` | `public, max-age=31536000, immutable` |
| `<duckdb_version>/<platform>/<name>.duckdb_extension.gz` | `deploy_latest` | `public, max-age=300` |

Objects are uploaded with `Content-Type: application/octet-stream` and no
`Content-Encoding`, because DuckDB gunzips them itself. Versioned objects go up
before latest objects. `extension_version` comes from the binary footer: the git
tag, or the short commit hash when HEAD has no tag.

## Secrets

| caller secret | passed as | notes |
|---|---|---|
| `QUERY_FARM_EXTENSION_SIGNING_KEY` | `signing_key` | org secret, PEM RSA private key (primary repo key) |
| `QUERY_FARM_R2_ACCESS_KEY_ID` | `r2_access_key_id` | R2 API token with write access to the bucket; optional with `dry_run` |
| `QUERY_FARM_R2_SECRET_ACCESS_KEY` | `r2_secret_access_key` | as above |

Secrets go to the reusable workflow explicitly, as in the snippet below. Do not use
`secrets: inherit`. The key is written to a mode-0600 file in `$RUNNER_TEMP` and
deleted when the step ends. It is never printed.

## Gating (required)

Only deploy from trusted contexts. Gate the caller job so it never runs for pull
requests, and ideally only for pushes to the release branch, tags, or a manual
dispatch. The workflow also refuses `pull_request` events itself, as a second guard.

## Caller snippet

```yaml
jobs:
  duckdb-stable-build:
    name: Build extension binaries
    uses: Query-farm/extension-ci-tools/.github/workflows/_extension_distribution.yml@query-farm
    with:
      duckdb_version: v2.0.0
      ci_tools_version: query-farm
      extension_name: hashfuncs
    secrets:
      ccache_remote_token: ${{ secrets.QUERY_FARM_CCACHE_TOKEN }}

  duckdb-deploy:
    name: Deploy to the Query.Farm repository
    needs: duckdb-stable-build
    if: >-
      github.event_name != 'pull_request' &&
      (github.event_name == 'workflow_dispatch' || github.ref == 'refs/heads/v2.0' || startsWith(github.ref, 'refs/tags/'))
    uses: Query-farm/extension-ci-tools/.github/workflows/_query_farm_deploy.yml@query-farm
    with:
      extension_name: hashfuncs
      duckdb_version: v2.0.0          # must equal the build job's duckdb_version
      # dry_run: true                 # sign + verify + list, no upload
    secrets:
      signing_key: ${{ secrets.QUERY_FARM_EXTENSION_SIGNING_KEY }}
      r2_access_key_id: ${{ secrets.QUERY_FARM_R2_ACCESS_KEY_ID }}
      r2_secret_access_key: ${{ secrets.QUERY_FARM_R2_SECRET_ACCESS_KEY }}
```

Other inputs: `exclude_archs` (default `wasm_mvp;wasm_eh;wasm_threads`),
`deploy_latest` / `deploy_versioned` (default true), `require_release_label`
(default true; set false only for testing, together with `dry_run`),
`repository_url`, `r2_bucket`, `r2_account_id`, `ci_tools_version`,
`override_ci_tools_repository`.

## Key rotation

Publish the new public key in the well-known file first. The file lists the primary
and a standby key. Wait for its cache to expire, then swap the
`QUERY_FARM_EXTENSION_SIGNING_KEY` secret. If the secret's key is not published,
step 5 fails the deploy.
