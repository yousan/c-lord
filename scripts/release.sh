#!/bin/bash
# release.sh — Cut a c-lord release by creating and pushing a vX.Y.Z git tag.
#
# The tag is the single source of truth: hatch-vcs derives the package version
# from it, and the tag-triggered .github/workflows/release.yml turns it into a
# GitHub Release with notes extracted from CHANGELOG.md.
#
# The next version is computed from the latest existing tag, bumped by a level
# inferred from the latest commit message ([major] / [minor] / [release] /
# default patch) — or forced with --level. Bootstraps to v1.4.0 when no tag
# exists yet.
#
# Usage:
#   ./scripts/release.sh                 # dry-run: print the tag that would be cut
#   ./scripts/release.sh --apply         # create + push the tag
#   ./scripts/release.sh --level minor   # force a bump level
#   ./scripts/release.sh --version 1.5.0 # set the exact version
#
# Environment:
#   SEED_VERSION  Version used when no tag exists yet (default: 1.4.0)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

APPLY=0
LEVEL=""
FORCE_VERSION=""
SEED_VERSION="${SEED_VERSION:-1.4.0}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --level) LEVEL="$2"; shift 2 ;;
    --version) FORCE_VERSION="$2"; shift 2 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Latest semver tag (highest version, not most recent commit). Empty if none.
LATEST_TAG="$(git tag --list 'v*' --sort=-v:refname | head -1 || true)"

if [[ -n "$FORCE_VERSION" ]]; then
  NEXT="${FORCE_VERSION#v}"
elif [[ -z "$LATEST_TAG" ]]; then
  # No tags yet — bootstrap.
  NEXT="${SEED_VERSION#v}"
  echo "No existing tag; bootstrapping at v${NEXT}" >&2
else
  CURRENT="${LATEST_TAG#v}"
  if [[ -z "$LEVEL" ]]; then
    MSG="$(git log -1 --format=%s)"
    LEVEL="$(python3 -c "import sys; from c_lord.version import detect_bump_level; print(detect_bump_level(sys.argv[1]))" "$MSG")"
  fi
  NEXT="$(python3 -c "import sys; from c_lord.version import bump_version; print(bump_version(sys.argv[1], sys.argv[2]))" "$CURRENT" "$LEVEL")"
fi

TAG="v${NEXT}"
echo "Latest tag : ${LATEST_TAG:-<none>}"
echo "Bump level : ${LEVEL:-<seed/forced>}"
echo "Next tag   : ${TAG}"

if git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "ERROR: tag ${TAG} already exists." >&2
  exit 1
fi

if [[ "$APPLY" -ne 1 ]]; then
  echo "(dry-run — re-run with --apply to create and push ${TAG})"
  exit 0
fi

git tag -a "$TAG" -m "Release ${TAG}"
git push origin "$TAG"
echo "Pushed ${TAG} — release.yml will publish the GitHub Release."
