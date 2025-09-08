#!/usr/bin/env bash
set -xeuo pipefail

# Wait until all core addons are actually up
sudo k8s status --wait-ready --timeout 10m || true

# Make sure there's a default StorageClass for Juju's PVC
kubectl annotate storageclass local-storage \
  storageclass.kubernetes.io/is-default-class="true" --overwrite

# Debug: show storage classes and nodes
kubectl get sc -o wide
kubectl get nodes -o wide
