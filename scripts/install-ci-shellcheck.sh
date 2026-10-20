#!/bin/sh
# Preserve existing installations; use the same verified tool in images and
# an existing session's prerequisite repair.
set -eu

version=0.11.0
destination=/opt/mergemarshall/shellcheck${version}
case "$(uname -m)" in
    x86_64) arch=x86_64; checksum=b7af85e41cc99489dcc21d66c6d5f3685138f06d34651e6d34b42ec6d54fe6f6 ;;
    aarch64|arm64) arch=aarch64; checksum=68a8133197a50beb8803f8d42f9908d1af1c5540d4bb05fdfca8c1fa47decefc ;;
    *) echo 'Unsupported ShellCheck architecture' >&2; exit 1 ;;
esac
test "$(id -u)" = 0 || { echo 'Run as container root' >&2; exit 1; }
link=/usr/local/bin/shellcheck
if test -e "$link" || test -L "$link"; then
    test "$(readlink "$link")" = "$destination/shellcheck" || {
        echo "Refusing to replace $link" >&2; exit 1;
    }
fi
if test ! -e "$destination"; then
    mkdir -p /opt/mergemarshall
    staging=$(mktemp -d /opt/mergemarshall/.shellcheck-install.XXXXXX)
    trap 'rm -rf "$staging"' EXIT HUP INT TERM
    curl --fail --location --silent --show-error \
        "https://github.com/koalaman/shellcheck/releases/download/v${version}/shellcheck-v${version}.linux.${arch}.tar.gz" \
        -o "$staging/shellcheck.tar.gz"
    printf '%s  %s\n' "$checksum" "$staging/shellcheck.tar.gz" | sha256sum --check --strict
    tar -xzf "$staging/shellcheck.tar.gz" -C "$staging"
    test "$("$staging/shellcheck-v${version}/shellcheck" --version | sed -n 's/^version: //p')" = "$version"
    mv -T "$staging/shellcheck-v${version}" "$destination"
fi
test "$("$destination/shellcheck" --version | sed -n 's/^version: //p')" = "$version"
test -L "$link" || ln -s "$destination/shellcheck" "$link"
shellcheck --version
