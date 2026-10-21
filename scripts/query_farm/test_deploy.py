import gzip
import hashlib
import json
from contextlib import redirect_stdout
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from query_farm import deploy


def make_footer(platform, duckdb_version, extension_version, abi="CPP", magic="4"):
    # DuckDB reads 8 x 32-byte fields and reverses them: field 0 (last on disk) is the magic.
    fields = [b"", b"", b"", abi.encode(), extension_version.encode(),
              duckdb_version.encode(), platform.encode(), magic.encode()]
    metadata = b"".join(f.ljust(32, b"\0") for f in fields)
    return metadata + b"\0" * 256


def make_extension(platform="linux_amd64", duckdb_version="v2.0.0", extension_version="abc123",
                   body_size=3 * 1024 * 1024 + 17, abi="CPP"):
    body = bytes((i * 7) % 251 for i in range(body_size))
    return body + make_footer(platform, duckdb_version, extension_version, abi)


HAVE_OPENSSL = shutil.which("openssl") is not None


def genkey(directory: Path, name: str):
    private = directory / f"{name}_private.pem"
    public = directory / f"{name}_public.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                    "-out", str(private)], check=True, capture_output=True)
    subprocess.run(["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(public)],
                   check=True, capture_output=True)
    return private, public


class FooterTest(unittest.TestCase):
    def test_parse(self):
        footer = deploy.parse_footer(make_extension("osx_arm64", "v2.0.0", "175c649dd8"))
        self.assertEqual(footer, deploy.Footer("4", "osx_arm64", "v2.0.0", "175c649dd8", "CPP"))

    def test_empty_abi_means_cpp(self):
        self.assertEqual(deploy.parse_footer(make_extension(abi="")).abi_type, "CPP")

    def test_bad_magic(self):
        data = b"x" * 1000 + make_footer("linux_amd64", "v2.0.0", "a", magic="")
        with self.assertRaisesRegex(deploy.DeployError, "magic"):
            deploy.parse_footer(data)

    def test_too_small(self):
        with self.assertRaisesRegex(deploy.DeployError, "smaller"):
            deploy.parse_footer(b"abc")

    def test_hash_matches_chunked_algorithm(self):
        payload = bytes(range(256)) * 9000  # > 2 MiB, not chunk aligned
        chunks = [payload[i:i + 1048576] for i in range(0, len(payload), 1048576)]
        expected = hashlib.sha256(b"".join(hashlib.sha256(c).digest() for c in chunks)).digest()
        self.assertEqual(deploy.extension_hash(payload), expected)


class ContextTest(unittest.TestCase):
    def run_quiet(self, *args):
        with redirect_stdout(io.StringIO()) as out:
            result = deploy.check_context(*args)
        return result, out.getvalue()

    def test_release_label(self):
        self.assertTrue(deploy.is_release_label("v2.0.0"))
        for label in ("v2.0-cyanoptera", "v2.0.0-dev12", "2.0.0", "v2.0", "main", "v2.0.0\n"):
            self.assertFalse(deploy.is_release_label(label), label)

    def test_pull_request_refused(self):
        for event in ("pull_request", "pull_request_target"):
            with self.assertRaises(deploy.DeployError):
                deploy.check_context(event, "v2.0.0", True)

    def test_prerelease_skips_with_notice(self):
        proceed, out = self.run_quiet("push", "v2.0-cyanoptera", True)
        self.assertFalse(proceed)
        self.assertIn("::notice", out)

    def test_prerelease_allowed_when_not_required(self):
        proceed, out = self.run_quiet("workflow_dispatch", "v2.0-cyanoptera", False)
        self.assertTrue(proceed)
        self.assertIn("::warning", out)

    def test_release_proceeds(self):
        self.assertTrue(self.run_quiet("push", "v2.0.0", True)[0])


@unittest.skipUnless(HAVE_OPENSSL, "openssl not available")
class PrepareTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keydir = Path(tempfile.mkdtemp())
        cls.private, cls.public = genkey(cls.keydir, "repo")
        cls.other_private, cls.other_public = genkey(cls.keydir, "other")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.keydir)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.artifacts = self.tmp / "artifacts"
        self.out = self.tmp / "out"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def add_artifact(self, arch, data, name="quack", version="v2.0.0"):
        directory = self.artifacts / f"{name}-{version}-extension-{arch}"
        directory.mkdir(parents=True)
        suffix = ".duckdb_extension.wasm" if arch.startswith("wasm") else ".duckdb_extension"
        (directory / f"{name}{suffix}").write_bytes(data)

    def well_known(self, *publics):
        return {"signature_keys": [p.read_text() for p in publics]}

    def prepare(self, version="v2.0.0", key=None, publics=None, exclude="wasm_mvp;wasm_eh;wasm_threads",
                require=True):
        with redirect_stdout(io.StringIO()):
            return deploy.prepare(self.artifacts, self.out, "quack", version, deploy.split_archs(exclude),
                                  key or self.private, self.well_known(*(publics or [self.other_public, self.public])),
                                  require)

    def test_sign_verify_gzip_and_plan(self):
        for arch in ("linux_amd64", "osx_arm64"):
            self.add_artifact(arch, make_extension(arch))
        self.add_artifact("wasm_eh", make_extension("wasm_eh"))
        results = self.prepare()
        self.assertEqual([r.arch for r in results], ["linux_amd64", "osx_arm64"])
        with tempfile.TemporaryDirectory() as work:
            for result in results:
                signed = gzip.decompress((self.out / result.file).read_bytes())
                self.assertEqual(hashlib.sha256(signed).hexdigest(), result.signed_sha256)
                self.assertTrue(deploy.verify_extension(signed, self.public, Path(work)))
                self.assertFalse(deploy.verify_extension(signed, self.other_public, Path(work)))
                original = make_extension(result.arch)
                self.assertEqual(signed[:-256], original[:-256])
        manifest = json.loads((self.out / "manifest.json").read_text())
        plan = deploy.object_plan(manifest, True, True)
        self.assertEqual([p["key"] for p in plan], [
            "quack/abc123/v2.0.0/linux_amd64/quack.duckdb_extension.gz",
            "quack/abc123/v2.0.0/osx_arm64/quack.duckdb_extension.gz",
            "v2.0.0/linux_amd64/quack.duckdb_extension.gz",
            "v2.0.0/osx_arm64/quack.duckdb_extension.gz",
        ])
        self.assertEqual(plan[0]["cache_control"], deploy.VERSIONED_CACHE_CONTROL)
        self.assertEqual(plan[-1]["cache_control"], deploy.LATEST_CACHE_CONTROL)
        self.assertEqual(len(deploy.object_plan(manifest, True, False)), 2)
        with redirect_stdout(io.StringIO()) as out:
            deploy.upload(self.out, "bucket", "https://example.invalid", "https://repo.example", True, False, True)
        self.assertIn("DRY RUN", out.getvalue())

    def test_platform_mismatch_refused(self):
        self.add_artifact("linux_arm64", make_extension("linux_amd64"))
        with self.assertRaisesRegex(deploy.DeployError, "footer platform"):
            self.prepare()

    def test_duckdb_version_mismatch_refused(self):
        self.add_artifact("linux_amd64", make_extension("linux_amd64", duckdb_version="v2.0.1"))
        with self.assertRaisesRegex(deploy.DeployError, "footer duckdb_version"):
            self.prepare()

    def test_c_struct_skips_duckdb_version_check(self):
        self.add_artifact("linux_amd64", make_extension("linux_amd64", duckdb_version="v1.2.0", abi="C_STRUCT"))
        self.assertEqual(len(self.prepare()), 1)

    def test_extension_version_disagreement_refused(self):
        self.add_artifact("linux_amd64", make_extension("linux_amd64", extension_version="a"))
        self.add_artifact("osx_arm64", make_extension("osx_arm64", extension_version="b"))
        with self.assertRaisesRegex(deploy.DeployError, "disagree"):
            self.prepare()

    def test_prerelease_label_refused_when_required(self):
        self.add_artifact("linux_amd64", make_extension("linux_amd64", duckdb_version="v2.0-cyanoptera"),
                          version="v2.0-cyanoptera")
        with self.assertRaisesRegex(deploy.DeployError, "release label"):
            self.prepare(version="v2.0-cyanoptera")
        self.assertEqual(len(self.prepare(version="v2.0-cyanoptera", require=False)), 1)

    def test_unpublished_signing_key_refused(self):
        self.add_artifact("linux_amd64", make_extension("linux_amd64"))
        with self.assertRaisesRegex(deploy.DeployError, "not listed"):
            self.prepare(key=self.other_private, publics=[self.public])

    def test_nothing_found(self):
        self.artifacts.mkdir()
        with self.assertRaisesRegex(deploy.DeployError, "no artifacts"):
            self.prepare()

    def test_all_excluded(self):
        self.add_artifact("wasm_mvp", make_extension("wasm_mvp"))
        with self.assertRaisesRegex(deploy.DeployError, "excluded"):
            self.prepare()


if __name__ == "__main__":
    unittest.main()
