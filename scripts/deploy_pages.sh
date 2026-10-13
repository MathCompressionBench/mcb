#!/usr/bin/env bash
# ABOUTME: Builds the GitHub Pages branch from a folder of site pages: runs the deny-pattern gate, makes one orphan commit
# ABOUTME: on the pages branch (gh-pages, or --branch), and force-updates the remote's branch only with --push REMOTE.
#
# Usage: scripts/deploy_pages.sh --gate GATE.py [--branch BRANCH] [--push REMOTE] [--out DIR] [SITE_DIR]
#
#   SITE_DIR       the folder of pages to publish (default: site/); it must hold index.html
#   --gate GATE    the deny-pattern gate (a Python script that takes paths and exits 0 only when it finds nothing);
#                  or set MCB_PAGES_GATE. Required: without a gate nothing is built.
#   --branch BRANCH  the pages branch (default gh-pages; the organization's <org>.github.io repository serves main)
#   --push REMOTE  force-update the pages branch of REMOTE (a URL or a path, e.g. git@github.com:<owner>/<repo>.git) with
#                  the new commit. Without it the commit is only built, and the script says what it would push.
#   --out DIR      where to build the one-commit repository (default: a new directory under $TMPDIR); it must not
#                  exist yet.
#
# The gate runs on SITE_DIR before anything is built, and again on the built repository (its files and its commit)
# before anything is pushed. Every deploy replaces the branch with a single commit, so it keeps no history.
set -euo pipefail

AUTHOR_NAME=MathCompressionBench
AUTHOR_EMAIL=MathCompressionBench@users.noreply.github.com

gate=${MCB_PAGES_GATE:-}
branch=gh-pages
remote=""
out=""
site=""
while [ $# -gt 0 ]; do
  case $1 in
    --gate) gate=${2:?--gate needs a path}; shift 2 ;;
    --branch) branch=${2:?--branch needs a name}; shift 2 ;;
    --push) remote=${2:?--push needs a remote}; shift 2 ;;
    --out) out=${2:?--out needs a directory}; shift 2 ;;
    -h|--help) sed -n '4,18p' "$0"; exit 0 ;;
    -*) echo "unknown option $1" >&2; exit 2 ;;
    *) site=$1; shift ;;
  esac
done
site=${site:-site}

if [ -z "$gate" ]; then
  echo "no gate: pass --gate GATE.py (or set MCB_PAGES_GATE); nothing is built without it" >&2
  exit 2
fi
[ -f "$gate" ] || { echo "the gate $gate is not a file" >&2; exit 2; }
[ -f "$site/index.html" ] || { echo "$site has no index.html: give the folder of site pages" >&2; exit 2; }

echo "gate on $site:"
python3 "$gate" "$site" || { echo "the gate found material that must not be published in $site: nothing built" >&2; exit 1; }

if [ -z "$out" ]; then
  out=$(mktemp -d "${TMPDIR:-/tmp}/mcb-pages.XXXXXX")
  rmdir "$out"
fi
[ -e "$out" ] && { echo "$out exists; choose a new directory" >&2; exit 2; }
mkdir -p "$out"
cp -R "$site"/. "$out"/
touch "$out/.nojekyll"   # serve the files as they are

export GIT_AUTHOR_NAME=$AUTHOR_NAME GIT_AUTHOR_EMAIL=$AUTHOR_EMAIL GIT_COMMITTER_NAME=$AUTHOR_NAME \
  GIT_COMMITTER_EMAIL=$AUTHOR_EMAIL
g() { git -C "$out" -c commit.gpgsign=false -c core.hooksPath=/dev/null "$@"; }
git init -q "$out"
g symbolic-ref HEAD "refs/heads/$branch"
g add -A
g commit -q -m "MathCompressionBench site"
sha=$(g rev-parse HEAD)

echo "gate on the built branch:"
python3 "$gate" "$out" || { echo "the gate found material in the built branch ($out): nothing pushed" >&2; exit 1; }

echo "built $branch commit $sha in $out ($(g ls-files | wc -l | tr -d ' ') files)"
if [ -z "$remote" ]; then
  echo "not pushed: run again with --push <remote> to force-update its $branch with this one commit"
  exit 0
fi
g push --force "$remote" "refs/heads/$branch:refs/heads/$branch"
echo "pushed $branch $sha to $remote"
