# Query.Farm extension deploy

`.github/workflows/_query_farm_deploy.yml` signs the binaries produced by
`build.yml` (preferred) or the legacy `_extension_distribution.yml` and publishes them to the Query.Farm extension
repository: R2 bucket `query-farm-duckdb-extensions`, served at
`https://duckdb-extensions.query.farm`. The logic lives in
`scripts/query_farm/deploy.py` (unit tests: `scripts/query_farm/test_deploy.py`).

## What it does

1. **Context check.** Refuses `pull_request` / `pull_request_target` events.
   If `duckdb_version` is not a release label (`^v[0-9]+\.[0-9]+\.[0-9]+$`, e.g.
   `v2.0.0`), the deploy job is skipped with a notice. This keeps
   `v2.0-cyanoptera` and any other dev or alpha builds out of the repository.
2. **Download** the build artifacts of the current run: `<group>-extensions-<arch>`
   from `build.yml` (the extension must sit at `<duckdb_version>/<arch>/` inside it;
   other extensions and test-support bundles in it are ignored) or
   `<extension_name>-<duckdb_version>-extension-<arch>` from `_extension_distribution.yml`.
   Two artifacts providing the same arch fail the deploy. Then drop the architectures in `exclude_archs`. By default
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
   `dry_run`, it only prints each key, size and sha256. Otherwise each object is
   checked after upload: `head-object` must show the expected size, content type,
   cache-control and sha256 metadata, and the object fetched from the public URL
   must have the same sha256.

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
    uses: Query-farm/extension-ci-tools/.github/workflows/build.yml@query-farm
    with:
      duckdb_version: v2.0.0          # tag; resolved to its commit, and the footer label
      groups: |
        main:
          config: extension_config.cmake
          toolchain: main
      # exclude_archs: wasm_mvp;wasm_eh;wasm_threads
      # test_config: '{"test_env_variables":{"NAME":"value"}}'
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

The legacy build job (`_extension_distribution.yml@query-farm` with `extension_name`,
`duckdb_version` and `ci_tools_version: query-farm`) works with the same deploy job.

### build.yml in this fork

`build.yml` builds Linux inside DuckDB's published `ghcr.io/duckdb/duckdb-ci` images
(manylinux_2_28, alpine for musl) instead of building a Docker image per run.
Query.Farm differences from upstream:

- extension-ci-tools defaults to `Query-farm/extension-ci-tools@query-farm`; vcpkg is pinned
  to DuckDB v2.0.0's commit and cannot be overridden.
- `duckdb_version` may be a tag or branch; it is resolved to a commit (DuckDB's CMake needs one).
- DuckDB is built from source by default (`prebuilt_source` defaults to `disabled`): a staged
  prebuilt DuckDB carries a pre-release label (e.g. `v2.0.0-alpha49307`) into the extension footer.
- Remote ccache (`ccache_remote_token` secret, `ccache_remote_url` input) with the pinned ccache
  from `scripts/setup_ccache.sh`, per-run ccache statistics, stable emsdk paths for wasm.
- vcpkg binary cache (`scripts/setup_vcpkg_cache.sh`): DuckDB's public read-only cache
  (`vcpkg_public_cache_url`, as in the legacy pipeline) first, then a files cache kept in the
  GitHub Actions cache (key `qf-vcpkg-v1-<os>-<group>-<image or arch>-<vcpkg commit>-<run>`,
  restored by prefix, saved only when the build produced new packages). duckdb-ci's own
  per-run vcpkg cache save is disabled. An optional writable http cache (`vcpkg_remote_url`,
  written with `ccache_remote_token`) replaces the GitHub cache when set; it is off by default.
- `test_config` input (test environment variables).
- Tests run in each build job; the separate artifact test jobs are opt-in (`test_jobs: true`).
- Artifacts are named `<group>-extensions-<arch>` and contain an extension repository tree.

Other inputs: `exclude_archs` (default `wasm_mvp;wasm_eh;wasm_threads`),
`deploy_latest` / `deploy_versioned` (default true), `require_release_label`
(default true; set false only for testing, together with `dry_run`),
`key_prefix` (default empty; e.g. `_pilot/` puts every key under a throwaway
prefix for test uploads), `repository_url`, `r2_bucket`, `r2_account_id`, `ci_tools_version`,
`override_ci_tools_repository`.

## Key rotation

Publish the new public key in the well-known file first. The file lists the primary
and a standby key. Wait for its cache to expire, then swap the
`QUERY_FARM_EXTENSION_SIGNING_KEY` secret. If the secret's key is not published,
step 5 fails the deploy.
