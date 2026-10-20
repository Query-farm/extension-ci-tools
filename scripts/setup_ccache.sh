#!/usr/bin/env bash
# Query.Farm: install a pinned ccache plus the HTTPS remote storage helper on the
# GitHub Actions host, and optionally enable remote storage.
#
# - Installs ccache ${CCACHE_VERSION} and ccache-storage-http-go ${HELPER_VERSION}
#   (verified by sha256) into $RUNNER_TEMP/ccache-bin and prepends it to PATH, so
#   hendrikmuhs/ccache-action (install: detect) uses it instead of installing an
#   older ccache, and so it is the `ccache` used as the compiler launcher.
# - The helper is installed as both ccache-storage-http and ccache-storage-https
#   next to ccache, where ccache looks for storage helpers.
# - Only when CCACHE_REMOTE_TOKEN is non-empty: exports CCACHE_REMOTE_STORAGE
#   (CCACHE_REMOTE_URL + bearer token) and path normalisation settings so cache
#   entries are shared across repositories. Without a token, ccache behaves like
#   upstream (local cache restored/saved through the GitHub Actions cache).
#
# On Linux the compiler runs inside Docker; the image ships its own ccache and
# ci_phase.py forwards CCACHE_REMOTE_STORAGE into docker_env.txt. The host copy
# is still installed so ccache-action's stats/eviction use a matching version.
set -euo pipefail

CCACHE_VERSION=4.13.6
HELPER_VERSION=0.10

case "${RUNNER_OS:-}/${RUNNER_ARCH:-}" in
  Linux/X64)
    ccache_asset="ccache-${CCACHE_VERSION}-linux-x86_64-musl-static.tar.gz"
    ccache_sha=09e0547a0c3b250a76675c33130366f1399f3580842fb360c052520d56214ead
    helper_asset="ccache-storage-http-go-${HELPER_VERSION}-linux-amd64.tar.gz"
    helper_sha=88963ef2cc21ca588145d46bcb02b8295275975ed75696eedf2cdb0e9543edd0
    ;;
  Linux/ARM64)
    ccache_asset="ccache-${CCACHE_VERSION}-linux-aarch64-musl-static.tar.gz"
    ccache_sha=bff0e0c19165db8627c85c36b0885b3b180659eda67a028298d26565aad52f56
    helper_asset="ccache-storage-http-go-${HELPER_VERSION}-linux-arm64.tar.gz"
    helper_sha=f0ee3302b52f87628b69fa53d2cc52a626c4c5c7e43b779ac9e3db481418db40
    ;;
  macOS/X64)
    ccache_asset="ccache-${CCACHE_VERSION}-darwin.tar.gz" # universal binary
    ccache_sha=0274210ec9c9936ed5711d59b0de3167a51216a588ddde35f6bc828f366fe6d9
    helper_asset="ccache-storage-http-go-${HELPER_VERSION}-darwin-amd64.tar.gz"
    helper_sha=e835dd964885f9f1be62888f8a76c65051031b8cdc6378b3c692a8993b199bbb
    ;;
  macOS/ARM64)
    ccache_asset="ccache-${CCACHE_VERSION}-darwin.tar.gz" # universal binary
    ccache_sha=0274210ec9c9936ed5711d59b0de3167a51216a588ddde35f6bc828f366fe6d9
    helper_asset="ccache-storage-http-go-${HELPER_VERSION}-darwin-arm64.tar.gz"
    helper_sha=e84eb529e2ee49bb4263b671f8431376995f8cf071facb20a05aa9aa235bf095
    ;;
  Windows/X64)
    ccache_asset="ccache-${CCACHE_VERSION}-windows-x86_64.zip"
    ccache_sha=3d7cebb05850ad704e197b3f1d3f0f924ab6c9fdfc561578e146184fe9d89380
    helper_asset="ccache-storage-http-go-${HELPER_VERSION}-windows-amd64.zip"
    helper_sha=0bdb58975880dad2e53fc8e99f35e942ca4770a3cc7a39aa3453d6ffe13315b2
    ;;
  Windows/ARM64)
    ccache_asset="ccache-${CCACHE_VERSION}-windows-aarch64.zip"
    ccache_sha=bec01846b06d6d87bf35eda50544d7c8bf9b9a4859f218417a7081aa45d7fd47
    helper_asset="ccache-storage-http-go-${HELPER_VERSION}-windows-arm64.zip"
    helper_sha=e80897f036a04acc7a78b43662abcd032e71db1ac7a23918860b0d7ef6a03e9a
    ;;
  *)
    echo "::warning::setup_ccache.sh: unsupported runner ${RUNNER_OS:-?}/${RUNNER_ARCH:-?}; leaving ccache installation to ccache-action"
    exit 0
    ;;
esac

exe=""
if [ "${RUNNER_OS}" = "Windows" ]; then
  exe=".exe"
  temp_root="$(cygpath -u "${RUNNER_TEMP}")"
else
  temp_root="${RUNNER_TEMP}"
fi
bin_dir="${temp_root}/ccache-bin"
work_dir="${temp_root}/ccache-download"
rm -rf "${work_dir}"
mkdir -p "${bin_dir}" "${work_dir}"

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

fetch() { # url expected_sha destination
  curl -fsSL --retry 3 --retry-delay 2 -o "$3" "$1"
  local actual
  actual="$(sha256_of "$3")"
  if [ "${actual}" != "$2" ]; then
    echo "::error::checksum mismatch for $1: expected $2, got ${actual}"
    exit 1
  fi
}

extract() { # archive destination
  case "$1" in
    *.zip) unzip -q -o "$1" -d "$2" ;;
    *) tar -xzf "$1" -C "$2" ;;
  esac
}

fetch "https://github.com/ccache/ccache/releases/download/v${CCACHE_VERSION}/${ccache_asset}" "${ccache_sha}" "${work_dir}/${ccache_asset}"
fetch "https://github.com/ccache/ccache-storage-http-go/releases/download/v${HELPER_VERSION}/${helper_asset}" "${helper_sha}" "${work_dir}/${helper_asset}"
extract "${work_dir}/${ccache_asset}" "${work_dir}"
extract "${work_dir}/${helper_asset}" "${work_dir}"

archive_dir() { # each archive contains a single directory named like the asset
  local name="${1%.zip}"
  echo "${work_dir}/${name%.tar.gz}"
}
cp "$(archive_dir "${ccache_asset}")/ccache${exe}" "${bin_dir}/ccache${exe}"
cp "$(archive_dir "${helper_asset}")/ccache-storage-http${exe}" "${bin_dir}/ccache-storage-http${exe}"
# Copy rather than symlink: symlinks are unreliable on Windows runners.
cp "${bin_dir}/ccache-storage-http${exe}" "${bin_dir}/ccache-storage-https${exe}"
chmod +x "${bin_dir}"/*
rm -rf "${work_dir}"

if [ "${RUNNER_OS}" = "Windows" ]; then
  cygpath -w "${bin_dir}" >> "${GITHUB_PATH}"
else
  echo "${bin_dir}" >> "${GITHUB_PATH}"
fi

echo "Installed into ${bin_dir}:"
"${bin_dir}/ccache${exe}" --version | head -n 1
{ "${bin_dir}/ccache-storage-https${exe}" --version 2>&1 || true; } | grep '^Version' | sed 's/^/ccache-storage-https /'

token="$(printf '%s' "${CCACHE_REMOTE_TOKEN:-}" | tr -d '\r\n')"
if [ -z "${token}" ]; then
  echo "No ccache_remote_token supplied: using the local ccache (GitHub Actions cache) only."
  exit 0
fi
if [ -z "${CCACHE_REMOTE_URL:-}" ]; then
  echo "::warning::ccache_remote_token supplied but ccache_remote_url is empty; remote ccache disabled."
  exit 0
fi

# Values in remote_storage must percent-encode '%', '|' and whitespace.
encoded="$(printf '%s' "${token}" | sed -e 's/%/%25/g' -e 's/|/%7C/g' -e 's/ /%20/g')"
if [ "${encoded}" != "${token}" ]; then
  echo "::add-mask::${encoded}"
fi

{
  echo "CCACHE_REMOTE_STORAGE=${CCACHE_REMOTE_URL} @bearer-token=${encoded} data-timeout=15s request-timeout=60s"
  echo "CCACHE_REMOTE_ENABLED=true"
  # Make cache keys independent of the repository checkout path so objects
  # (notably DuckDB core) are shared between extension repositories. The Linux
  # Docker build always uses /duckdb_build_dir, so this only matters on hosts.
  echo "CCACHE_BASEDIR=${GITHUB_WORKSPACE}"
  echo "CCACHE_NOHASHDIR=true"
} >> "${GITHUB_ENV}"
echo "Remote ccache enabled: ${CCACHE_REMOTE_URL} (https storage helper, bearer auth)"
