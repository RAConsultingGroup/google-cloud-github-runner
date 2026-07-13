#!/usr/bin/env bash

# Helper script for Google Cloud Build
# Terraform managed!

#shellcheck disable=SC2154

set -e

# Run from the script's own directory so the relative paths below work even
# when Terraform invokes this via local-exec from a remote root module
# (local-exec's cwd is the root module dir, not this module's gcp/ dir).
cd "$(dirname "$0")"

# Check if required files exist
if [ ! -f "cloudbuild-container.yaml" ] || [ ! -f "../Dockerfile" ]; then
	echo "Error: This command must be executed in the gcp directory." >&2
	echo "Required files not found:"
	[ ! -f "cloudbuild-container.yaml" ] && echo "  - cloudbuild-container.yaml (in current directory)"
	[ ! -f "../Dockerfile" ] && echo "  - Dockerfile (in parent directory)"
	exit 1
fi

# Build the container image
echo "Building container image via Cloud Build..."
cd ..
gcloud builds submit --config "gcp/cloudbuild-container.yaml" --region="${region}" --gcs-source-staging-dir="gs://${bucket}/source" --project="${project_id}" --quiet
cd ~-

echo "✓ Container build completed successfully."
