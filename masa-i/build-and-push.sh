#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# build-and-push.sh
# Builds and pushes all MASA 1.0.1 images as multi-arch manifests
# (linux/amd64 + linux/arm64) so they work on both Apple Silicon dev
# machines and x86_64 OpenShift cluster nodes.
#
# Prerequisites:
#   - docker buildx with a builder that supports multi-arch (see setup below)
#   - docker login to Docker Hub as jpksn
#
# Usage:
#   ./build-and-push.sh                  # build everything
#   ./build-and-push.sh baseimage        # build only the baseimage
#   ./build-and-push.sh agents           # build all agents (baseimage must already exist)
#   ./build-and-push.sh operator         # build only the operator
#   ./build-and-push.sh perception       # build only the perception agent
#   ./build-and-push.sh reflection       # build only the reflection agent
#   ./build-and-push.sh planning         # build only the planning agent
#   ./build-and-push.sh planning-training # build only the planning training image
#   ./build-and-push.sh action           # build only the action agent
#   ./build-and-push.sh understanding    # build only the understanding agent
#   ./build-and-push.sh dashboard        # build only the dashboard
# ---------------------------------------------------------------------------

set -euo pipefail

REGISTRY="jpksn"
VERSION="1.0.3"
PLATFORMS="linux/amd64,linux/arm64"
BUILDER="masa-builder"

ROOT="$(cd "$(dirname "$0")" && pwd)"

# ---------------------------------------------------------------------------
# Ensure a multi-arch buildx builder exists
# ---------------------------------------------------------------------------
setup_builder() {
  if ! docker buildx inspect "$BUILDER" &>/dev/null; then
    echo "→ Creating buildx builder: $BUILDER"
    docker buildx create --name "$BUILDER" --use
  else
    echo "→ Using existing buildx builder: $BUILDER"
    docker buildx use "$BUILDER"
  fi
}

# ---------------------------------------------------------------------------
# Helper: build + push one image
#   build_image <image-name> <build-context-path>
# ---------------------------------------------------------------------------
build_image() {
  local name="$1"
  local context="$2"
  local tag="${REGISTRY}/${name}:${VERSION}"
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  Building: ${tag}"
  echo "  Context:  ${context}"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  docker buildx build \
    --platform "${PLATFORMS}" \
    --tag "${tag}" \
    --push \
    "${context}"
  echo "  ✓ Pushed: ${tag}"
}

# ---------------------------------------------------------------------------
# Build targets
# ---------------------------------------------------------------------------
build_baseimage() {
  echo ""
  echo "=== Step 1: Baseimage (all agents depend on this — must be first) ==="
  build_image "masa-baseimage"        "$ROOT/masa-baseimage/docker"
}

build_agents() {
  echo ""
  echo "=== Step 2: Agent images ==="
  build_image "masa-agent-understanding"       "$ROOT/masa-agent-understanding/docker"
  build_image "masa-agent-perception"          "$ROOT/masa-agent-perception/docker"
  build_image "masa-agent-action"              "$ROOT/masa-agent-action/docker"
  build_image "masa-agent-planning"            "$ROOT/masa-agent-planning/docker"
  build_image "masa-agent-planning-training"   "$ROOT/masa-agent-planning/docker-training"
  build_image "masa-agent-reflection"          "$ROOT/masa-agent-reflection/docker"
  build_image "masa-dashboard"                 "$ROOT/masa-dashboard/docker"
}

build_operator() {
  echo ""
  echo "=== Step 3: Operator ==="
  build_image "masa-operator"   "$ROOT/masa-autoscaler-operator"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
TARGET="${1:-all}"

setup_builder

case "$TARGET" in
  baseimage)
    build_baseimage
    ;;
  agents)
    build_agents
    ;;
  operator)
    build_operator
    ;;
  all)
    build_baseimage
    build_agents
    build_operator
    ;;
  perception)
    build_image "masa-agent-perception"        "$ROOT/masa-agent-perception/docker"
    ;;
  reflection)
    build_image "masa-agent-reflection"        "$ROOT/masa-agent-reflection/docker"
    ;;
  planning)
    build_image "masa-agent-planning"          "$ROOT/masa-agent-planning/docker"
    ;;
  planning-training)
    build_image "masa-agent-planning-training" "$ROOT/masa-agent-planning/docker-training"
    ;;
  action)
    build_image "masa-agent-action"            "$ROOT/masa-agent-action/docker"
    ;;
  understanding)
    build_image "masa-agent-understanding"     "$ROOT/masa-agent-understanding/docker"
    ;;
  dashboard)
    build_image "masa-dashboard"               "$ROOT/masa-dashboard/docker"
    ;;
  *)
    echo "Unknown target: $TARGET"
    echo "Usage: $0 [all|baseimage|agents|operator|perception|reflection|planning|planning-training|action|understanding|dashboard]"
    exit 1
    ;;
esac

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  All done. Verify a manifest with:"
echo "  docker buildx imagetools inspect ${REGISTRY}/masa-operator:${VERSION}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
