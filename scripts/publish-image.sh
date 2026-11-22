#!/usr/bin/env bash
# Build and publish a release image from a git tag on Google Cloud Build.
#
# The image lands in Artifact Registry at
#   <region>-docker.pkg.dev/<project>/<repository>/unified-agent:<tag>
# Builds run on Google's default Cloud Build pool, so no GitHub Actions minutes
# are used. The source is `git archive <tag>`, so the image matches the tag
# exactly, whatever the working tree holds.
#
# Usage:
#   scripts/publish-image.sh v0.3.2
#   CLOUDSDK_ACTIVE_CONFIG_NAME=publish scripts/publish-image.sh v0.3.2
#
# Use a gcloud configuration signed in as a project owner (or an account with
# Cloud Build Editor + Artifact Registry Writer). On a deployment hub, do not switch
# the `default` configuration — the hub's backup service account lives there.
set -euo pipefail

tag="${1:?usage: scripts/publish-image.sh <tag>}"
[[ "$tag" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || {
  echo "publish-image.sh: tag must be a valid image tag" >&2; exit 1;
}
# No implicit project, build region or publication destination.
: "${UA_GCP_PROJECT:?set UA_GCP_PROJECT}"
: "${UA_BUILD_REGION:?set UA_BUILD_REGION}"
: "${UA_IMAGE_REPOSITORY:?set UA_IMAGE_REPOSITORY}"
# Substitutions use comma-separated values: reject delimiters before any IO.
for value in "$tag" "$UA_GCP_PROJECT" "$UA_BUILD_REGION" "$UA_IMAGE_REPOSITORY" "${UA_REPO_URL:-}"; do
  [[ "$value" != *','* && "$value" != *'$'* && "$value" != *$'\n'* && "$value" != *$'\r'* ]] || {
    echo "publish-image.sh: invalid configuration value" >&2; exit 1;
  }
done
[[ "$UA_IMAGE_REPOSITORY" == "$UA_BUILD_REGION-docker.pkg.dev/$UA_GCP_PROJECT/"* && "$UA_IMAGE_REPOSITORY" =~ ^[a-z0-9-]+-docker\.pkg\.dev/[a-z0-9-]+/[a-z0-9._-]+/[a-z0-9._/-]+$ ]] || {
  echo "publish-image.sh: image repository must match the configured project and region" >&2; exit 1;
}
# Docker repository components cannot be empty, dot segments, or begin/end
# with separators. Validate the whole path before git fetch/archive or upload.
image_path="${UA_IMAGE_REPOSITORY#*/}"
[[ "$image_path" =~ ^[a-z0-9]+(([._]|__|-+)[a-z0-9]+)*(/[a-z0-9]+(([._]|__|-+)[a-z0-9]+)*)+$ ]] || {
  echo "publish-image.sh: invalid image repository path" >&2; exit 1;
}
if [[ -n "${UA_REPO_URL:-}" ]]; then
  [[ "$UA_REPO_URL" =~ ^https://[^/@[:space:]?#]+(/[^[:space:]?#]*)?$ && "$UA_REPO_URL" != *'@'* ]] || {
    echo "publish-image.sh: UA_REPO_URL must be HTTPS without embedded credentials, query or fragment" >&2; exit 1;
  }
fi
repo_root="$(git rev-parse --show-toplevel)"
config="${repo_root}/deploy/docker/cloudbuild.yaml"

git -C "${repo_root}" fetch -q --tags origin
vcs_ref="$(git -C "${repo_root}" rev-list -n1 "${tag}")"

src="$(mktemp -d)"
trap 'rm -rf "${src}"' EXIT
git -C "${repo_root}" archive "${tag}" | tar -x -C "${src}"

gcloud builds submit "${src}" \
  --project="${UA_GCP_PROJECT}" \
  --region="${UA_BUILD_REGION}" \
  --config="${config}" \
  --substitutions="_IMAGE=${UA_IMAGE_REPOSITORY},_SOURCE_URL=${UA_REPO_URL:-},_TAG=${tag},_VERSION=${tag#v},_VCS_REF=${vcs_ref}"
