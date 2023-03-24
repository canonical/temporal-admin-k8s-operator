[![Charmhub Badge](https://charmhub.io/temporal-admin-k8s/badge.svg)](https://charmhub.io/temporal-admin-k8s)
[![Release Edge](https://github.com/canonical/temporal-admin-k8s-operator/actions/workflows/test_and_publish_charm.yaml)](https://github.com/canonical/temporal-admin-k8s-operator/actions/workflows/test_and_publish_charm.yaml)

# Temporal Admin K8s Operator

This is the Kubernetes Python Operator for the
[Temporal admin tools](https://temporal.io/).

## Description

Temporal is a developer-first, open source platform that ensures the successful
execution of services and applications (using workflows).

Use Workflow as Code (TM) to build and operate resilient applications. Leverage
developer friendly primitives and avoid fighting your infrastructure

This operator provides the Temporal admin tools, and consists of Python scripts which
wraps the versions distributed by
[temporalio](https://hub.docker.com/r/temporalio/admin-tools).

## Usage

Please check the
[Temporal server operator](https://charmhub.io/temporal-k8s)
for usage instructions.

## Contributing

This charm is still in active development. Please see the
[Juju SDK docs](https://juju.is/docs/sdk) for guidelines on enhancements to this charm
following best practice guidelines, and `CONTRIBUTING.md` for developer guidance.
