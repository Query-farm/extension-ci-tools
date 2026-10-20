To recompute the SHA512 when bumping the commit of a repository (for example avro-c):
```bash
curl -L https://github.com/duckdb/duckdb-avro-c/archive/<commit_hash>.tar.gz | openssl dgst -sha512
```
## thrift (Query.Farm)

`thrift` overlays vcpkg's thrift 0.23.0 port with `-DBUILD_COMPILER=OFF` and without the flex/bison lookup. vcpkg
2026.06.24 ships thrift 0.23.0, whose compiler build passes `--file-prefix-map` to bison, which needs GNU bison >= 3.7;
the manylinux_2_28 images have bison 3.0.4. Arrow only needs the Thrift C++ runtime library, so the compiler is not
built. Extensions pick this up through `./extension-ci-tools/vcpkg_ports` in their vcpkg overlay ports.
