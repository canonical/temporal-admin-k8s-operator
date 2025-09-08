#!/usr/bin/env bash
set -xeuo pipefail

echo ">>> [k8s-pre.sh] Starting Canonical K8s pre-run script..."
date

# 1. Ensure core addons are enabled
echo ">>> Enabling Canonical K8s addons (network, dns, local-storage)..."
sudo k8s enable network dns local-storage || true

# 2. Wait until they are actually ready
echo ">>> Waiting for addons to be ready..."
sudo k8s status --wait-ready --timeout 10m || true

# 3. Make sure there's a DEFAULT StorageClass for Juju's PVC
echo ">>> Annotating local-storage as default StorageClass..."
kubectl annotate storageclass local-storage \
  storageclass.kubernetes.io/is-default-class="true" --overwrite || true

# 4. Debug: show what we have
echo ">>> Current StorageClasses:"
kubectl get sc -o wide || true

echo ">>> Current Nodes:"
kubectl get nodes -o wide || true
