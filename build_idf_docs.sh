#!/usr/bin/env bash
#
# Build ESP-IDF documentation to docutils XML, one directory per chip target.
#
# The XML builder's output is what ingest_sphinx_xml.py consumes: it is written
# after Sphinx resolves the doctree, so `only::` branches are already pruned for
# the target and every {IDF_TARGET_*} constant is substituted with the real value
# read from soc_caps headers and Kconfig.
#
# Requires a full ESP-IDF install for this checkout, including the docs feature:
#     cd $IDF_PATH && ./install.sh <target> --enable-docs
# The docs build runs `idf.py set-target` against a dummy project to extract
# component info, so the toolchain must be present -- it is not optional.
#
# Usage:
#     ./build_idf_docs.sh esp32p4
#     ./build_idf_docs.sh esp32 esp32s2 esp32c3      # 3 at a time in parallel
#
# Output: $IDF_PATH/docs/_build/en/<target>/xml/
#
set -uo pipefail

IDF_PATH="${IDF_PATH:-$HOME/git/esp-idf}"
PARALLEL="${PARALLEL:-3}"

if [ $# -eq 0 ]; then
    echo "usage: $0 <target> [target ...]" >&2
    exit 2
fi

if [ ! -d "$IDF_PATH" ]; then
    echo "IDF_PATH does not exist: $IDF_PATH" >&2
    exit 1
fi
export IDF_PATH

# cairosvg -> cairocffi -> xcffib resolves libxcb through ctypes, which does not
# search Homebrew's prefix on its own. Without this the build dies at extension
# load with "cannot load library 'libxcb.dylib'".
if [ -d /opt/homebrew/lib ]; then
    export DYLD_FALLBACK_LIBRARY_PATH="/opt/homebrew/lib${DYLD_FALLBACK_LIBRARY_PATH:+:$DYLD_FALLBACK_LIBRARY_PATH}"
fi

# A project virtualenv on PATH silently wins over the IDF python environment and
# drags in an incompatible docutils, which fails much later and much less
# obviously (esp-docs needs docutils <0.21 for docutils.utils.error_reporting).
unset VIRTUAL_ENV

cd "$IDF_PATH" || exit 1
# shellcheck disable=SC1091
source ./export.sh > /dev/null 2>&1 || {
    echo "export.sh failed -- is ESP-IDF installed for this checkout?" >&2
    exit 1
}

cd "$IDF_PATH/docs" || exit 1
build-docs -t "$@" -l en -p "$PARALLEL" --builders xml build
status=$?

# esp-docs exits non-zero when the build produces warnings that aren't in
# sphinx-known-warnings.txt (doxygen @ingroup warnings do this routinely). The
# pages are still written, so report rather than fail outright.
if [ $status -ne 0 ]; then
    echo ""
    echo "build-docs exited $status -- this is usually the warnings check, not missing"
    echo "content. Verify before assuming failure:"
    for target in "$@"; do
        dir="$IDF_PATH/docs/_build/en/$target/xml"
        if [ -d "$dir" ]; then
            echo "  $target: $(grep -rl '^<document' "$dir" 2>/dev/null | wc -l | tr -d ' ') Sphinx pages in $dir"
        else
            echo "  $target: NO OUTPUT at $dir"
        fi
    done
fi

exit $status
