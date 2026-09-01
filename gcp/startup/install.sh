#!/usr/bin/env bash

# Copyright 2025-2026 Nils Knieling. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Install Docker, GitHub CLI and GitHub Actions Runner for Linux with x64 or ARM64 CPU architecture
# https://github.com/actions/runner
# https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/about-self-hosted-runners#linux
# https://docs.docker.com/engine/install/ubuntu/
# https://github.com/cli/cli/blob/trunk/docs/install_linux.md

# Exit on error, undefined variables, and pipe failures
set -euo pipefail

# Set default GitHub Actions Runner installation directory
readonly MY_RUNNER_DIR="/actions-runner"

# Pinned so a rebake reproduces the same toolchain. The image bake is triggered by this
# file's hash (see outputs.tf), so any edit here rebakes on the next apply - with floating
# versions that apply would also move Docker, yarn and the runner under the whole fleet,
# and whoever ran an unrelated apply would own the upgrade.
# Bump deliberately, in a PR of its own:
#   apt-cache madison docker-ce            (or the Packages index for the target release)
#   npm view yarn versions
#   https://github.com/actions/runner/releases
#   curl -s https://cli.github.com/packages/dists/stable/main/binary-amd64/Packages | grep -A1 '^Package: gh$'
readonly MY_DOCKER_VERSION="5:29.7.2-1~ubuntu.24.04~noble"
readonly MY_CONTAINERD_VERSION="2.3.3-1~ubuntu.24.04~noble"
readonly MY_BUILDX_VERSION="0.36.1-1~ubuntu.24.04~noble"
readonly MY_COMPOSE_VERSION="5.4.0-1~ubuntu.24.04~noble"
readonly MY_YARN_VERSION="1.22.22"
readonly MY_RUNNER_VERSION="2.336.0"
readonly MY_GH_VERSION="2.98.0"

# Prevent interactive prompts during package installation
export DEBIAN_FRONTEND=noninteractive

# Function to exit the script with a failure message
exit_with_failure() {
	echo >&2 "FAILURE: $1"
	exit 1
}

# Detect CPU architecture early
case $(uname -m) in
	aarch64|arm64)
		readonly MY_ARCH="arm64"
		;;
	amd64|x86_64)
		readonly MY_ARCH="x64"
		;;
	*)
		exit_with_failure "Cannot determine CPU architecture!"
		;;
esac

# Install dependencies
echo "Installing system dependencies..."
sudo apt-get update -yq
sudo apt-get install -y \
	apt-transport-https \
	apt-utils \
	build-essential \
	ca-certificates \
	curl \
	dnsutils \
	git \
	gpg \
	jq \
	lsb-release \
	nodejs \
	npm \
	openssh-client \
	python3-crcmod \
	python3-openssl \
	python3-pip \
	python3-venv \
	software-properties-common \
	tar \
	unzip \
	zip

# Verify required commands are available
readonly REQUIRED_COMMANDS=(curl gzip jq sed tar)
for cmd in "${REQUIRED_COMMANDS[@]}"; do
	if ! command -v "$cmd" >/dev/null 2>&1; then
		exit_with_failure "Required command '$cmd' not found"
	fi
done

# Pull Docker Hub images through Google's pull-through cache. Hub answers manifest requests
# with 500s and rate limits often enough to fail unrelated CI jobs; mirror.gcr.io serves the
# same digests from inside GCP, and a daemon mirror covers every docker.io pull rather than
# the Dockerfiles someone remembered to edit. Written before the install so the daemon reads
# it on its first start.
#
# Two limits worth knowing before trusting this:
#   - `docker pull` falls back to Hub when the mirror misses or errors, but a BuildKit-resolved
#     `FROM` may not: moby/buildkit#1972 (open) reports a hard fail on a mirror 503 where the
#     classic puller fell back. Image builds trade Hub outages for mirror.gcr.io outages, and
#     mirror.gcr.io carries no SLA - keep build-level retry in the consuming repos.
#   - Only the default `docker` buildx driver honours this. A `docker-container` builder (what
#     docker/setup-buildx-action creates by default) ignores daemon.json and needs its own
#     buildkitd.toml `[registry."docker.io"] mirrors`.
echo "Configuring Docker Hub registry mirror..."
sudo mkdir -p "/etc/docker"
echo '{"registry-mirrors":["https://mirror.gcr.io"]}' | sudo tee "/etc/docker/daemon.json" >/dev/null

# Add Docker repository and install
echo "Installing Docker..."
sudo curl -fsSL "https://download.docker.com/linux/ubuntu/gpg" | sudo gpg --dearmor -o "/usr/share/keyrings/download.docker.com"
echo "deb [signed-by=/usr/share/keyrings/download.docker.com] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee "/etc/apt/sources.list.d/docker.list" >/dev/null
sudo apt-get update -yq
sudo apt-get install -y \
	docker-ce="$MY_DOCKER_VERSION" \
	docker-ce-cli="$MY_DOCKER_VERSION" \
	containerd.io="$MY_CONTAINERD_VERSION" \
	docker-buildx-plugin="$MY_BUILDX_VERSION" \
	docker-compose-plugin="$MY_COMPOSE_VERSION"

# Enable and start Docker service
sudo systemctl enable docker.service
sudo systemctl start docker.service

# Fail the bake rather than ship an image that silently pulls straight from Docker Hub.
if ! sudo docker info --format '{{.RegistryConfig.Mirrors}}' | grep -q "mirror.gcr.io"; then
	exit_with_failure "Docker did not pick up the mirror.gcr.io registry mirror"
fi

# Install GitHub CLI. GitHub-hosted runner images preinstall it and workflows use it
# directly (gh api/gh pr/gh release) instead of raw curl+token calls; the actions/checkout
# and friends do not.
echo "Installing GitHub CLI..."
sudo mkdir -p -m 755 "/etc/apt/keyrings"
sudo curl -fsSL "https://cli.github.com/packages/githubcli-archive-keyring.gpg" -o "/etc/apt/keyrings/githubcli-archive-keyring.gpg"
sudo chmod go+r "/etc/apt/keyrings/githubcli-archive-keyring.gpg"
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee "/etc/apt/sources.list.d/github-cli.list" >/dev/null
sudo apt-get update -yq
sudo apt-get install -y gh="$MY_GH_VERSION"

if ! command -v gh >/dev/null 2>&1; then
	exit_with_failure "GitHub CLI installation failed"
fi

# Pre-pull what CI needs on nearly every job: runners are one-job-and-destroyed, so no
# image cache survives between jobs. Non-fatal by design - a missed image is fetched at
# runtime, and a registry hiccup must not block a fleet-wide image update.
for image in $(curl -sf --connect-timeout 2 --max-time 10 -H "Metadata-Flavor: Google" \
	"http://metadata.google.internal/computeMetadata/v1/instance/attributes/prewarm-images" || true); do
	sudo docker pull "$image" || echo "WARNING: could not pre-pull $image"
done

# Install Yarn (classic) globally. GitHub-hosted runner images preinstall it
# and workflows invoke it directly; actions/setup-node does not install yarn.
echo "Installing Yarn..."
sudo npm install -g "yarn@$MY_YARN_VERSION"

# Bubblewrap (used by openai/codex-action's read-only/workspace-write sandboxes, and by
# Codex's own default sandbox) needs unprivileged user namespaces to set up a network
# namespace - without this it fails with `bwrap: loopback: Failed RTM_NEWADDR: Operation
# not permitted`. openai/codex-action ships a setup step for exactly this
# (kernel.unprivileged_userns_clone, and on Ubuntu 24.04+ the AppArmor-added
# kernel.apparmor_restrict_unprivileged_userns), but that step is gated to
# `runner.environment == 'github-hosted'` and explicitly skips self-hosted runners - so
# self-hosted images have to bake it in themselves. Root-only, at bake time - the
# `runner` user needs no sudo grant of its own for this, unlike CI's own sudo needs.
echo "Enabling unprivileged user namespaces for bubblewrap..."
if sysctl -n kernel.unprivileged_userns_clone >/dev/null 2>&1; then
	sudo sysctl -w kernel.unprivileged_userns_clone=1
	echo "kernel.unprivileged_userns_clone=1" | sudo tee /etc/sysctl.d/99-bubblewrap-userns.conf >/dev/null
fi
if sysctl -n kernel.apparmor_restrict_unprivileged_userns >/dev/null 2>&1; then
	sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
	echo "kernel.apparmor_restrict_unprivileged_userns=0" | sudo tee -a /etc/sysctl.d/99-bubblewrap-userns.conf >/dev/null
fi

# Create runner user, in the docker group only - not `google-sudoers`.
echo "Creating runner user..."
if ! id -u runner >/dev/null 2>&1; then
	sudo useradd -m runner
fi
sudo usermod -aG docker runner

# Full passwordless sudo, same as `google-sudoers` granted, but as a NAMED sudoers.d
# entry instead of a group grant. Runner VMs live in their own dedicated project
# (scalera-ci) with nothing else on it, so narrowing this isn't a priority - what
# matters is that openai/codex-action's `safety-strategy: drop-sudo` can actually find
# and revoke it. Its cleanup (dropSudoWithPrivileges) strips sudoers.d lines whose
# FIRST WHITESPACE-DELIMITED TOKEN matches the username - `%google-sudoers ALL=...`
# starts with `%google-sudoers`, never matches `runner`, so drop-sudo silently declared
# success while sudo stayed fully usable. A `runner ...` line matches and gets stripped.
cat <<'EOF' | sudo tee /etc/sudoers.d/runner-ci >/dev/null
runner ALL=(ALL) NOPASSWD:ALL
EOF
sudo chmod 0440 /etc/sudoers.d/runner-ci
sudo visudo -c -f /etc/sudoers.d/runner-ci

# Install GitHub Actions Runner
echo "Installing GitHub Actions Runner..."
echo "Installing GitHub Actions Runner version: v${MY_RUNNER_VERSION}"

# Download and extract runner
sudo mkdir -p "$MY_RUNNER_DIR"
cd "$MY_RUNNER_DIR"
sudo curl -fsSL -O "https://github.com/actions/runner/releases/download/v${MY_RUNNER_VERSION}/actions-runner-linux-${MY_ARCH}-${MY_RUNNER_VERSION}.tar.gz"
sudo tar xzf "actions-runner-linux-${MY_ARCH}-${MY_RUNNER_VERSION}.tar.gz"

# Run the installation script
sudo ./bin/installdependencies.sh
echo "GitHub Actions Runner installed successfully"

# Cleanup: Clear package cache and temporary files
echo "Cleaning up..."
sudo apt-get clean
sudo rm -rf /tmp/* /root/.cache

# Cleanup: Rotate and vacuum journal logs
sudo journalctl --rotate
sudo journalctl --vacuum-time=1s

# Cleanup: Remove compressed and rotated log files, then truncate remaining logs
sudo find /var/log -type f \( -name "*.gz" -o -regex ".*\.[0-9]$" \) -delete
sudo find /var/log -type f -exec truncate -s 0 {} +

echo "Setup completed successfully"

# Shutdown VM
sudo shutdown -h now
