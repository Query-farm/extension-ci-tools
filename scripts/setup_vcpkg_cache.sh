#!/usr/bin/env bash
# Query.Farm: choose the vcpkg binary cache for build.yml jobs.
#
# duckdb/duckdb-ci/build-duckdb points VCPKG_BINARY_SOURCES at a files cache in
# $GITHUB_WORKSPACE/vcpkg-cache that it saves to the GitHub Actions cache under a
# new key on every run. build.yml disables that (cache: false) and instead
# exports CI_VCPKG_BINARY_SOURCES, which scripts/ci_phase.py applies on top of
# whatever build-duckdb configured:
#
# - remote (VCPKG_REMOTE_URL set): vcpkg's http provider against the Query.Farm
#   R2 cache, shared by every repository. Reads are anonymous; writes only when
#   VCPKG_REMOTE_TOKEN is non-empty (trusted runs).
# - gha: unless the remote is writable, a files cache in vcpkg-cache that
#   build.yml restores from / saves to the GitHub Actions cache, saving only when
#   the build produced new packages.
#
# Outputs: path (files cache directory), gha (true when the GitHub cache is used).
set -euo pipefail

if [ "${RUNNER_OS:-}" = "Windows" ]; then
  cache_path="$(cygpath -w "${GITHUB_WORKSPACE}")\\vcpkg-cache"
  mkdir -p "$(cygpath -u "${cache_path}")"
else
  cache_path="${GITHUB_WORKSPACE}/vcpkg-cache"
  mkdir -p "${cache_path}"
fi

token="$(printf '%s' "${VCPKG_REMOTE_TOKEN:-}" | tr -d '\r\n')"
sources="clear"
use_gha=true
if [ -n "${VCPKG_REMOTE_URL:-}" ]; then
  if [ -n "${token}" ]; then
    sources="${sources};http,${VCPKG_REMOTE_URL},readwrite,Authorization: Bearer ${token}"
    use_gha=false
    echo "vcpkg binary cache: ${VCPKG_REMOTE_URL} (read/write)"
  else
    sources="${sources};http,${VCPKG_REMOTE_URL},read"
    echo "vcpkg binary cache: ${VCPKG_REMOTE_URL} (read only, no token)"
  fi
fi
if [ "${use_gha}" = "true" ]; then
  sources="${sources};files,${cache_path},readwrite"
  echo "vcpkg binary cache: GitHub Actions cache via ${cache_path}"
fi

echo "CI_VCPKG_BINARY_SOURCES=${sources}" >> "${GITHUB_ENV}"
{
  echo "path=${cache_path}"
  echo "gha=${use_gha}"
} >> "${GITHUB_OUTPUT}"
