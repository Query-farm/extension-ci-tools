#!/usr/bin/env python3
"""Sign DuckDB extension binaries and publish them to the Query.Farm repository.

Used by .github/workflows/_query_farm_deploy.yml. Subcommands:

  check-context  decide whether this run may deploy (event + release label)
  prepare        validate footers, sign, verify against the published keys, gzip
  upload         upload the prepared files to R2 (or list them in dry-run mode)

Signing mirrors DuckDB's scripts/extension-upload-single.sh: the trailing
256-byte signature slot is dropped, the remainder is hashed with the
compute-extension-hash.sh algorithm (sha256 of each 1 MiB chunk, then sha256
of the concatenated digests) and signed with RSA/SHA-256 via
``openssl pkeyutl -sign -pkeyopt digest:sha256``; the 256-byte signature is
appended.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

FOOTER_SIZE = 512
SIGNATURE_SIZE = 256
FIELD_SIZE = 32
EXPECTED_MAGIC = "4"
HASH_CHUNK_SIZE = 1024 * 1024
RELEASE_LABEL = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")  # used with fullmatch
SAFE_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")  # used with fullmatch
WELL_KNOWN_PATH = "/.well-known/duckdb-extension-repo.json"
LATEST_CACHE_CONTROL = "public, max-age=300"
VERSIONED_CACHE_CONTROL = "public, max-age=31536000, immutable"
ARTIFACT_MARKER = "-extension-"


class DeployError(RuntimeError):
    """A check failed; the message is meant for the workflow log."""


# --------------------------------------------------------------------------
# Footer parsing (mirrors ExtensionHelper::ParseExtensionMetaData)
# --------------------------------------------------------------------------


@dataclass
class Footer:
    magic: str
    platform: str
    duckdb_version: str  # for C_STRUCT extensions: the C API version
    extension_version: str
    abi_type: str


def _field(raw: bytes) -> str:
    return raw.rstrip(b"\0").decode("utf-8", errors="replace")


def parse_footer(data: bytes) -> Footer:
    """Parse the 256-byte metadata block that precedes the signature."""
    if len(data) < FOOTER_SIZE:
        raise DeployError(
            f"file is {len(data)} bytes, smaller than the {FOOTER_SIZE}-byte extension footer"
        )
    metadata = data[-FOOTER_SIZE:-SIGNATURE_SIZE]
    fields = [metadata[i * FIELD_SIZE : (i + 1) * FIELD_SIZE] for i in range(8)]
    fields.reverse()
    magic = _field(fields[0])
    if magic != EXPECTED_MAGIC:
        raise DeployError(
            f"extension footer magic is {magic!r}, expected {EXPECTED_MAGIC!r}; "
            "not a DuckDB extension with metadata"
        )
    abi = _field(fields[4]) or "CPP"
    return Footer(
        magic=magic,
        platform=_field(fields[1]),
        duckdb_version=_field(fields[2]),
        extension_version=_field(fields[3]),
        abi_type=abi,
    )


# --------------------------------------------------------------------------
# Hashing, signing and verification
# --------------------------------------------------------------------------


def extension_hash(payload: bytes) -> bytes:
    """compute-extension-hash.sh: sha256 over the sha256 of each 1 MiB chunk."""
    digests = b"".join(
        hashlib.sha256(payload[offset : offset + HASH_CHUNK_SIZE]).digest()
        for offset in range(0, len(payload), HASH_CHUNK_SIZE)
    )
    return hashlib.sha256(digests).digest()


def _openssl(args: list[str], stdin: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["openssl", *args], input=stdin, capture_output=True, check=False
    )
    if result.returncode != 0:
        # openssl never echoes key material in its errors, but keep it short.
        raise DeployError(
            f"openssl {args[0]} failed: {result.stderr.decode(errors='replace').strip()[:500]}"
        )
    return result.stdout


def public_key_der(pem_path: Path, private: bool) -> bytes:
    args = ["pkey", "-in", str(pem_path), "-pubout", "-outform", "DER"]
    if not private:
        args.insert(1, "-pubin")
    return _openssl(args)


def fingerprint(der: bytes) -> str:
    return "sha256:" + hashlib.sha256(der).hexdigest()


def sign_extension(data: bytes, key_path: Path, work: Path) -> bytes:
    """Return the signed extension: data minus its signature slot, plus a fresh signature."""
    payload = data[:-SIGNATURE_SIZE]
    digest_file = work / "hash.bin"
    digest_file.write_bytes(extension_hash(payload))
    signature = _openssl(
        [
            "pkeyutl",
            "-sign",
            "-in",
            str(digest_file),
            "-inkey",
            str(key_path),
            "-pkeyopt",
            "digest:sha256",
        ]
    )
    if len(signature) != SIGNATURE_SIZE:
        raise DeployError(
            f"signature is {len(signature)} bytes, expected {SIGNATURE_SIZE} (is the key RSA-2048?)"
        )
    return payload + signature


def verify_extension(data: bytes, public_pem: Path, work: Path) -> bool:
    payload, signature = data[:-SIGNATURE_SIZE], data[-SIGNATURE_SIZE:]
    digest_file = work / "verify-hash.bin"
    sig_file = work / "verify-sig.bin"
    digest_file.write_bytes(extension_hash(payload))
    sig_file.write_bytes(signature)
    result = subprocess.run(
        [
            "openssl",
            "pkeyutl",
            "-verify",
            "-pubin",
            "-inkey",
            str(public_pem),
            "-sigfile",
            str(sig_file),
            "-in",
            str(digest_file),
            "-pkeyopt",
            "digest:sha256",
        ],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


@dataclass
class PublishedKey:
    fingerprint: str
    pem_path: Path


def load_published_keys(document: dict, work: Path) -> list[PublishedKey]:
    keys = document.get("signature_keys")
    if not isinstance(keys, list) or not keys:
        raise DeployError("well-known file has no 'signature_keys' list")
    published = []
    for index, pem in enumerate(keys):
        if not isinstance(pem, str) or "BEGIN PUBLIC KEY" not in pem:
            raise DeployError(f"signature_keys[{index}] is not a PEM public key")
        path = work / f"published-{index}.pem"
        path.write_text(pem if pem.endswith("\n") else pem + "\n")
        published.append(PublishedKey(fingerprint(public_key_der(path, private=False)), path))
    return published


def fetch_well_known(repository_url: str) -> dict:
    url = repository_url.rstrip("/") + WELL_KNOWN_PATH
    request = urllib.request.Request(
        url, headers={"User-Agent": "query-farm-extension-deploy", "Cache-Control": "no-cache"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as error:  # noqa: BLE001 - surface any fetch/parse problem
        raise DeployError(f"could not fetch the repository key list from {url}: {error}") from error


# --------------------------------------------------------------------------
# Artifact discovery
# --------------------------------------------------------------------------


def split_archs(value: str) -> set[str]:
    return {part.strip() for part in re.split(r"[;,\s]+", value or "") if part.strip()}


def discover(artifacts_dir: Path, extension_name: str, duckdb_version: str) -> dict[str, Path]:
    """Map arch -> extension binary for downloaded `<name>-<version>-extension-<arch>` dirs."""
    prefix = f"{extension_name}-{duckdb_version}{ARTIFACT_MARKER}"
    found: dict[str, Path] = {}
    for directory in sorted(p for p in artifacts_dir.iterdir() if p.is_dir()):
        if not directory.name.startswith(prefix):
            continue
        arch = directory.name[len(prefix) :]
        if arch.endswith(".wasm"):
            arch = arch[: -len(".wasm")]
        candidates = [
            directory / f"{extension_name}.duckdb_extension",
            directory / f"{extension_name}.duckdb_extension.wasm",
        ]
        matches = [c for c in candidates if c.is_file()]
        if not matches:
            matches = sorted(
                p
                for p in directory.rglob(f"{extension_name}.duckdb_extension*")
                if p.is_file() and p.name.endswith((".duckdb_extension", ".duckdb_extension.wasm"))
            )
        if len(matches) != 1:
            raise DeployError(
                f"artifact {directory.name}: expected exactly one {extension_name}.duckdb_extension, "
                f"found {len(matches)}"
            )
        found[arch] = matches[0]
    return found


# --------------------------------------------------------------------------
# Subcommands
# --------------------------------------------------------------------------


def _github_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def _summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


def is_release_label(version: str) -> bool:
    return bool(RELEASE_LABEL.fullmatch(version))


def check_context(event_name: str, duckdb_version: str, require_release_label: bool) -> bool:
    if event_name in ("pull_request", "pull_request_target"):
        raise DeployError(
            f"refusing to deploy from a '{event_name}' event; gate the deploy job with "
            "`if: github.event_name != 'pull_request'`"
        )
    if is_release_label(duckdb_version):
        print(f"DuckDB version {duckdb_version!r} is a release label; deploying.")
        return True
    if require_release_label:
        message = (
            f"Skipping deploy: duckdb_version {duckdb_version!r} is not a released DuckDB "
            "version label (vMAJOR.MINOR.PATCH). Pre-release builds are never published to "
            "the Query.Farm repository."
        )
        print(f"::notice title=Query.Farm deploy skipped::{message}")
        _summary(f"### Query.Farm deploy skipped\n\n{message}")
        return False
    print(
        f"::warning title=Release label check disabled::duckdb_version {duckdb_version!r} is "
        "not a release label; continuing because require_release_label is false."
    )
    return True


@dataclass
class Prepared:
    arch: str
    extension_version: str
    abi_type: str
    file: str  # gzip file, relative to the output dir
    signed_sha256: str
    signed_size: int
    gz_sha256: str
    gz_size: int
    key_fingerprint: str


def prepare(
    artifacts_dir: Path,
    out_dir: Path,
    extension_name: str,
    duckdb_version: str,
    exclude_archs: set[str],
    key_path: Path,
    well_known: dict,
    require_release_label: bool,
) -> list[Prepared]:
    for label, value in (("extension_name", extension_name), ("duckdb_version", duckdb_version)):
        if not SAFE_SEGMENT.fullmatch(value):
            raise DeployError(f"{label} {value!r} is not a safe path segment")
    if require_release_label and not is_release_label(duckdb_version):
        raise DeployError(f"duckdb_version {duckdb_version!r} is not a release label")

    binaries = discover(artifacts_dir, extension_name, duckdb_version)
    if not binaries:
        raise DeployError(
            f"no artifacts named {extension_name}-{duckdb_version}-extension-<arch> in {artifacts_dir}"
        )
    selected = {arch: path for arch, path in binaries.items() if arch not in exclude_archs}
    skipped = sorted(set(binaries) - set(selected))
    if skipped:
        print(f"Excluded architectures: {', '.join(skipped)}")
    if not selected:
        raise DeployError("every downloaded architecture is excluded; nothing to deploy")

    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[Prepared] = []
    with tempfile.TemporaryDirectory(prefix="qf-deploy-") as tmp:
        work = Path(tmp)
        published = load_published_keys(well_known, work)
        print("Published repository keys: " + ", ".join(k.fingerprint for k in published))
        signing_fp = fingerprint(public_key_der(key_path, private=True))
        print(f"Signing key fingerprint: {signing_fp}")
        match = next((k for k in published if k.fingerprint == signing_fp), None)
        if match is None:
            raise DeployError(
                f"the signing key ({signing_fp}) is not listed in the repository's "
                "well-known file; publish its public key before deploying (rotated secret?)"
            )

        errors: list[str] = []
        footers: dict[str, Footer] = {}
        for arch, path in sorted(selected.items()):
            data = path.read_bytes()
            try:
                footer = parse_footer(data)
            except DeployError as error:
                errors.append(f"{arch}: {error}")
                continue
            footers[arch] = footer
            print(
                f"[{arch}] footer: platform={footer.platform} duckdb_version={footer.duckdb_version} "
                f"extension_version={footer.extension_version} abi={footer.abi_type}"
            )
            if footer.platform != arch:
                errors.append(f"{arch}: footer platform is {footer.platform!r}, artifact is for {arch!r}")
            if footer.abi_type == "C_STRUCT":
                print(f"[{arch}] C API extension (capi version {footer.duckdb_version}); "
                      "skipping the duckdb_version footer check")
            elif footer.duckdb_version != duckdb_version:
                errors.append(
                    f"{arch}: footer duckdb_version is {footer.duckdb_version!r}, expected {duckdb_version!r}"
                )
            if footer.abi_type not in ("CPP", "C_STRUCT", "C_STRUCT_UNSTABLE"):
                errors.append(f"{arch}: unknown extension ABI type {footer.abi_type!r}")
            if not SAFE_SEGMENT.fullmatch(footer.extension_version):
                errors.append(f"{arch}: extension_version {footer.extension_version!r} is not a safe path segment")
        versions = {f.extension_version for f in footers.values()}
        if len(versions) > 1:
            errors.append(f"architectures disagree on extension_version: {sorted(versions)}")
        if errors:
            raise DeployError("footer checks failed:\n  " + "\n  ".join(errors))

        for arch, path in sorted(selected.items()):
            footer = footers[arch]
            signed = sign_extension(path.read_bytes(), key_path, work)
            if parse_footer(signed) != footer:
                raise DeployError(f"[{arch}] footer changed while signing")
            if not verify_extension(signed, match.pem_path, work):
                raise DeployError(f"[{arch}] signature does not verify against published key {match.fingerprint}")
            print(f"[{arch}] signed and verified against published key {match.fingerprint}")

            relative = Path(arch) / f"{extension_name}.duckdb_extension.gz"
            target = out_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as raw:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as gz:
                    gz.write(signed)
            compressed = target.read_bytes()
            if gzip.decompress(compressed) != signed:
                raise DeployError(f"[{arch}] gzip round-trip mismatch")
            results.append(
                Prepared(
                    arch=arch,
                    extension_version=footer.extension_version,
                    abi_type=footer.abi_type,
                    file=relative.as_posix(),
                    signed_sha256=hashlib.sha256(signed).hexdigest(),
                    signed_size=len(signed),
                    gz_sha256=hashlib.sha256(compressed).hexdigest(),
                    gz_size=len(compressed),
                    key_fingerprint=match.fingerprint,
                )
            )
    manifest = {
        "extension_name": extension_name,
        "duckdb_version": duckdb_version,
        "files": [asdict(r) for r in results],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return results


KEY_PREFIX = re.compile(r"(?:[A-Za-z0-9_][A-Za-z0-9._-]*/)+")  # used with fullmatch


def validate_key_prefix(prefix: str) -> str:
    """Empty, or one or more 'segment/' parts; never '.well-known' or other dot-led segments."""
    if prefix and not KEY_PREFIX.fullmatch(prefix):
        raise DeployError(
            f"key_prefix {prefix!r} must be empty or look like 'segment/' (letters, digits, '._-', "
            "ending in '/', not starting with '.')"
        )
    return prefix


def object_plan(manifest: dict, deploy_latest: bool, deploy_versioned: bool, key_prefix: str = "") -> list[dict]:
    prefix = validate_key_prefix(key_prefix)
    name = manifest["extension_name"]
    duckdb_version = manifest["duckdb_version"]
    plan = []
    for entry in manifest["files"]:
        filename = f"{name}.duckdb_extension.gz"
        if deploy_versioned:
            plan.append(
                {
                    **entry,
                    "key": f"{prefix}{name}/{entry['extension_version']}/{duckdb_version}/{entry['arch']}/{filename}",
                    "cache_control": VERSIONED_CACHE_CONTROL,
                }
            )
        if deploy_latest:
            plan.append(
                {
                    **entry,
                    "key": f"{prefix}{duckdb_version}/{entry['arch']}/{filename}",
                    "cache_control": LATEST_CACHE_CONTROL,
                }
            )
    # Versioned objects first so "latest" never points at something missing a versioned copy.
    plan.sort(key=lambda item: (item["cache_control"] == LATEST_CACHE_CONTROL, item["key"]))
    return plan


def _aws(args: list[str], endpoint: str) -> subprocess.CompletedProcess:
    return subprocess.run(["aws", *args, "--endpoint-url", endpoint], capture_output=True, text=True, check=False)


def verify_uploaded(item: dict, bucket: str, endpoint: str, repository_url: str) -> None:
    """Check the stored object's headers, then fetch it over the public URL and compare sha256."""
    head = _aws(["s3api", "head-object", "--bucket", bucket, "--key", item["key"], "--output", "json"], endpoint)
    if head.returncode != 0:
        raise DeployError(f"head-object {item['key']} failed: {head.stderr.strip()[:500]}")
    meta = json.loads(head.stdout)
    problems = []
    if meta.get("ContentLength") != item["gz_size"]:
        problems.append(f"size {meta.get('ContentLength')} != {item['gz_size']}")
    if meta.get("ContentType") != "application/octet-stream":
        problems.append(f"content-type {meta.get('ContentType')!r}")
    if meta.get("ContentEncoding"):
        problems.append(f"unexpected content-encoding {meta.get('ContentEncoding')!r}")
    if meta.get("CacheControl") != item["cache_control"]:
        problems.append(f"cache-control {meta.get('CacheControl')!r}")
    if meta.get("Metadata", {}).get("sha256") != item["gz_sha256"]:
        problems.append("sha256 metadata mismatch")
    # Query string sidesteps any cached copy of a previous upload at the same URL.
    url = f"{repository_url.rstrip('/')}/{item['key']}?verify={item['gz_sha256'][:16]}"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "query-farm-extension-deploy"})
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read()
            encoding = response.headers.get("Content-Encoding")
    except Exception as error:  # noqa: BLE001
        raise DeployError(f"could not fetch {url}: {error}") from error
    public_sha = hashlib.sha256(body).hexdigest()
    if public_sha != item["gz_sha256"]:
        problems.append(f"public URL sha256 {public_sha} != {item['gz_sha256']}")
    if encoding:
        problems.append(f"public URL served Content-Encoding {encoding!r}")
    if problems:
        raise DeployError(f"verification of {item['key']} failed: " + "; ".join(problems))
    print(f"Verified: s3://{bucket}/{item['key']} (headers + metadata) and {url} sha256={public_sha}")


def upload(
    out_dir: Path,
    bucket: str,
    endpoint: str,
    repository_url: str,
    deploy_latest: bool,
    deploy_versioned: bool,
    dry_run: bool,
    key_prefix: str = "",
    verify: bool = True,
) -> list[dict]:
    manifest = json.loads((out_dir / "manifest.json").read_text())
    plan = object_plan(manifest, deploy_latest, deploy_versioned, key_prefix)
    if not plan:
        print("::warning::deploy_latest and deploy_versioned are both false; nothing to upload")
        return plan
    mode = "DRY RUN - would upload" if dry_run else "Uploading"
    rows = ["| object | size | sha256 (gz) | cache-control |", "|---|---:|---|---|"]
    for item in plan:
        url = f"{repository_url.rstrip('/')}/{item['key']}"
        print(
            f"{mode}: s3://{bucket}/{item['key']}  size={item['gz_size']}  sha256={item['gz_sha256']}  "
            f"(signed extension sha256={item['signed_sha256']}, {item['signed_size']} bytes)  "
            f"cache-control='{item['cache_control']}'  -> {url}"
        )
        rows.append(f"| `{item['key']}` | {item['gz_size']} | `{item['gz_sha256']}` | {item['cache_control']} |")
        if dry_run:
            continue
        result = _aws(
            [
                "s3", "cp", str(out_dir / item["file"]), f"s3://{bucket}/{item['key']}",
                "--content-type", "application/octet-stream",
                "--cache-control", item["cache_control"],
                "--metadata", f"sha256={item['gz_sha256']},extension-sha256={item['signed_sha256']}",
                "--no-progress", "--only-show-errors",
            ],
            endpoint,
        )
        if result.returncode != 0:
            raise DeployError(f"upload of {item['key']} failed (exit {result.returncode}): {result.stderr.strip()[:500]}")
        if verify:
            verify_uploaded(item, bucket, endpoint, repository_url)
    title = "Query.Farm deploy (dry run)" if dry_run else "Query.Farm deploy"
    _summary(f"### {title}: {manifest['extension_name']} for DuckDB {manifest['duckdb_version']}\n\n" + "\n".join(rows))
    return plan


def _bool(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in ("true", "1", "yes"):
        return True
    if lowered in ("false", "0", "no", ""):
        return False
    raise argparse.ArgumentTypeError(f"expected true/false, got {value!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    ctx = sub.add_parser("check-context")
    ctx.add_argument("--event-name", required=True)
    ctx.add_argument("--duckdb-version", required=True)
    ctx.add_argument("--require-release-label", type=_bool, default=True)

    prep = sub.add_parser("prepare")
    prep.add_argument("--artifacts-dir", type=Path, required=True)
    prep.add_argument("--out-dir", type=Path, required=True)
    prep.add_argument("--extension-name", required=True)
    prep.add_argument("--duckdb-version", required=True)
    prep.add_argument("--exclude-archs", default="")
    prep.add_argument("--signing-key", type=Path, required=True)
    prep.add_argument("--repository-url", required=True)
    prep.add_argument("--well-known-file", type=Path, help="use a local well-known JSON (tests only)")
    prep.add_argument("--require-release-label", type=_bool, default=True)

    up = sub.add_parser("upload")
    up.add_argument("--out-dir", type=Path, required=True)
    up.add_argument("--bucket", required=True)
    up.add_argument("--endpoint", required=True)
    up.add_argument("--repository-url", required=True)
    up.add_argument("--deploy-latest", type=_bool, default=True)
    up.add_argument("--deploy-versioned", type=_bool, default=True)
    up.add_argument("--dry-run", type=_bool, default=False)
    up.add_argument("--key-prefix", default="", help="prepend to every object key, e.g. '_pilot/' (testing)")
    up.add_argument("--verify", type=_bool, default=True, help="check each object via head-object and its public URL")

    args = parser.parse_args(argv)
    try:
        if args.command == "check-context":
            proceed = check_context(args.event_name, args.duckdb_version, args.require_release_label)
            _github_output("proceed", "true" if proceed else "false")
        elif args.command == "prepare":
            if shutil.which("openssl") is None:
                raise DeployError("openssl is not on PATH")
            well_known = (
                json.loads(args.well_known_file.read_text())
                if args.well_known_file
                else fetch_well_known(args.repository_url)
            )
            prepare(
                args.artifacts_dir,
                args.out_dir,
                args.extension_name,
                args.duckdb_version,
                split_archs(args.exclude_archs),
                args.signing_key,
                well_known,
                args.require_release_label,
            )
        else:
            upload(
                args.out_dir,
                args.bucket,
                args.endpoint,
                args.repository_url,
                args.deploy_latest,
                args.deploy_versioned,
                args.dry_run,
                args.key_prefix,
                args.verify,
            )
    except DeployError as error:
        message = str(error)
        first, _, rest = message.partition("\n")
        print(f"::error title=Query.Farm deploy::{first}")
        if rest:
            print(rest)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
