#!/usr/bin/env bash
# Query.Farm: choose the vcpkg binary cache for build.yml jobs.
#
# duckdb/duckdb-ci/build-duckdb points VCPKG_BINARY_SOURCES at a files cache in
# $GITHUB_WORKSPACE/vcpkg-cache that it saves to the GitHub Actions cache under a
# new key on every run. build.yml disables that (cache: false) and instead
# exports CI_VCPKG_BINARY_SOURCES, which scripts/ci_phase.py applies on top of
# whatever build-duckdb configured. Sources, in lookup order:
#
# 1. VCPKG_PUBLIC_CACHE_URL (default DuckDB's public cache), read only - the
#    same source the legacy _extension_distribution.yml path uses.
# 2. VCPKG_REMOTE_URL (optional, empty by default): an http binary cache of our
#    own, read anonymously and written when VCPKG_REMOTE_TOKEN is non-empty.
# 3. Unless (2) is writable: a files cache in vcpkg-cache that build.yml restores
#    from / saves to the GitHub Actions cache, saving only when the build
#    produced new packages.
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
if [ -n "${VCPKG_PUBLIC_CACHE_URL:-}" ]; then
  sources="${sources};http,${VCPKG_PUBLIC_CACHE_URL},read"
  echo "vcpkg binary cache: ${VCPKG_PUBLIC_CACHE_URL} (read only)"
fi
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
