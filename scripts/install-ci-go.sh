#!/bin/sh
# The same pinned toolchain is used in the CI image and for an existing
# session's owner-approved repair. Do not overwrite a different installation.
set -eu

version=1.24.9
destination=/opt/mergemarshall/go${version}
case "$(uname -m)" in
    x86_64) arch=amd64; checksum=5b7899591c2dd6e9da1809fde4a2fad842c45d3f6b9deb235ba82216e31e34a6 ;;
    aarch64|arm64) arch=arm64; checksum=9aa1243d51d41e2f93e895c89c0a2daf7166768c4a4c3ac79db81029d295a540 ;;
    *) echo 'Unsupported Go toolchain architecture' >&2; exit 1 ;;
esac
test "$(id -u)" = 0 || { echo 'Run as container root' >&2; exit 1; }
for tool in go gofmt; do
    link=/usr/local/bin/$tool
    if test -e "$link" || test -L "$link"; then
        test "$(readlink "$link")" = "$destination/bin/$tool" || {
            echo "Refusing to replace $link" >&2; exit 1;
        }
    fi
done
if test ! -e "$destination"; then
    mkdir -p /opt/mergemarshall
    staging=$(mktemp -d /opt/mergemarshall/.go-install.XXXXXX)
    trap 'rm -rf "$staging"' EXIT HUP INT TERM
    curl --fail --location --silent --show-error \
        "https://go.dev/dl/go${version}.linux-${arch}.tar.gz" -o "$staging/go.tar.gz"
    printf '%s  %s\n' "$checksum" "$staging/go.tar.gz" | sha256sum --check --strict
    tar -xzf "$staging/go.tar.gz" -C "$staging"
    test "$("$staging/go/bin/go" version)" = "go version go${version} linux/${arch}"
    # -T refuses an unexpected existing directory; never nest/replace it.
    mv -T "$staging/go" "$destination"
fi
test "$("$destination/bin/go" version)" = "go version go${version} linux/${arch}"
for tool in go gofmt; do
    test -L "/usr/local/bin/$tool" || ln -s "$destination/bin/$tool" "/usr/local/bin/$tool"
done
go version
