#!/usr/bin/env bash
# Cut a release the safe way: bump, sync dist, test, push, then tag master HEAD.
#
#   scripts/release.sh 0.8.2 "Release notes (markdown)"
#
# The release tag must point at a commit that already contains the current
# .github/workflows/publish-orcacloud.yml. GitHub runs the workflow file from
# the tagged commit, so tagging an older commit silently uses an older — often
# broken — workflow. `gh release create --target master` avoids that.
set -euo pipefail

version="${1:?usage: scripts/release.sh X.Y.Z \"release notes\"}"
notes="${2:-Release v$version}"

if ! printf '%s' "$version" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "version must be X.Y.Z (got: $version)" >&2
  exit 1
fi

cd "$(dirname "$0")/.."

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "working tree is dirty; commit or stash first" >&2
  exit 1
fi

branch="$(git rev-parse --abbrev-ref HEAD)"
if [ "$branch" != "master" ]; then
  echo "releases are cut from master (currently on $branch)" >&2
  exit 1
fi

latest="$(git tag --list 'v*' --sort=-v:refname | head -1)"
if [ -n "$latest" ]; then
  highest="$(printf '%s\n%s\n' "${latest#v}" "$version" | sort -V | tail -1)"
  if [ "$highest" != "$version" ]; then
    echo "version $version is not greater than the latest local tag $latest" >&2
    exit 1
  fi
fi

python3 - "$version" <<'PY'
import re
import sys

version = sys.argv[1]

path = "plugin/plugin.py"
source = open(path, encoding="utf-8").read()
source = re.sub(r'^# version = "[^"]+"$', f'# version = "{version}"',
                source, count=1, flags=re.M)
source = re.sub(r'^PLUGIN_VERSION = "[^"]+"$', f'PLUGIN_VERSION = "{version}"',
                source, count=1, flags=re.M)
open(path, "w", encoding="utf-8").write(source)

readme = open("README.md", encoding="utf-8").read()
readme = re.sub(r'^\*\*Version:\*\* .*$', f'**Version:** {version}',
                readme, count=1, flags=re.M)
open("README.md", "w", encoding="utf-8").write(readme)
PY

cp plugin/plugin.py dist/anycubic_cloud_any.py
cp plugin/plugin.py dist/anycubic_linux_x86_64.py
shasum -a 256 dist/anycubic_cloud_any.py > dist/anycubic_cloud_any.py.sha256
shasum -a 256 dist/anycubic_linux_x86_64.py > dist/anycubic_linux_x86_64.py.sha256

python3 -m py_compile plugin/plugin.py
(cd plugin && PYTHONPATH=tests python3 tests/smoke_test.py >/dev/null)
(cd plugin && ANYCUBIC_PLUGIN_PATH=../dist/anycubic_cloud_any.py \
  PYTHONPATH=tests python3 tests/smoke_test.py >/dev/null)

git add plugin/plugin.py plugin/tests dist README.md .github scripts
git commit -m "release: v$version"
git push origin master

gh release create "v$version" \
  --target master \
  --title "v$version" \
  --notes "$notes" \
  dist/anycubic_cloud_any.py \
  dist/anycubic_cloud_any.py.sha256

echo "Released v$version. Watch the publish:"
echo "  gh run list --repo $(gh repo view --json nameWithOwner --jq .nameWithOwner) --workflow publish-orcacloud.yml --limit 1"
