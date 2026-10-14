[![Charmhub Badge](https://charmhub.io/temporal-admin-k8s/badge.svg)](https://charmhub.io/temporal-admin-k8s)
[![Release Edge](https://github.com/canonical/temporal-admin-k8s-operator/actions/workflows/test_and_publish_charm.yaml/badge.svg)](https://github.com/canonical/temporal-admin-k8s-operator/actions/workflows/test_and_publish_charm.yaml)

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

### Temporal endpoint resolution

`temporal-host-info` relation is the only source of Temporal server address.

### Frontend TLS

When the Temporal server's frontend serves gRPC over TLS (its `frontend-certificates`
relation is integrated), the server tells this charm over `temporal-host-info`, and the
`cli` action dials the frontend over TLS. To verify the frontend's certificate, the
charm needs the CA that issued it. Integrate `receive-ca-cert` with the same
certificates provider that issues the frontend certificate:

```bash
juju integrate temporal-admin-k8s:receive-ca-cert self-signed-certificates:send-ca-cert
```

Until the CA is available, the `cli` action fails with a message saying so. Without
frontend TLS, the relation isn't needed and the CLI dials the frontend in plaintext.

## Contributing

This charm is still in active development. Please see the
[Juju SDK docs](https://juju.is/docs/sdk) for guidelines on enhancements to this charm
following best practice guidelines, and `CONTRIBUTING.md` for developer guidance.
