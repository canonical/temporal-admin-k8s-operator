# temporal-admin-k8s

## Developing

You can use the environments created by `tox` for development:

```shell
tox --notest -e unit
source .tox/unit/bin/activate
```

### Testing

```shell
tox -e fmt           # update your code according to linting rules
tox -e lint          # code style
tox -e unit          # unit tests
tox -e integration   # integration tests
tox                  # runs 'lint' and 'unit' environments
```

### Deploy

Please refer to the
[Temporal server documentation](https://github.com/canonical/temporal-k8s-operator/blob/main/CONTRIBUTING.md)
for instructions about how to deploy the admin tools and relate them to the
server.

### Committing

This repo uses team-owned CI/CD workflows (see `.github/workflows`). Each job runs the same `just` recipes you can run locally (`just lint`, `just unit`, `just integration tests/integration/<module>.py`). The workflows are as follows:
- `tests.yaml`: This runs library checks, linting, unit tests and the integration tests (one job per test module) on every pull request.
- `publish.yaml`: This runs on every push to a `track/**` branch. Once a PR is merged, this workflow runs `tests.yaml`, then builds the charm and publishes the new version to the edge channel on Charmhub.
- `promote.yaml`: This is a manually triggered workflow which publishes the charm currently on the edge channel to the stable channel on Charmhub.

These tests validate extensive linting and formatting rules. Before creating a PR, please run `tox` to ensure proper formatting and linting is performed.
