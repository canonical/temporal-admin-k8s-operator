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

This repo uses CI/CD workflows as outlined by [operator-workflows](https://github.com/canonical/operator-workflows). The four workflows are as follows:
- `test.yaml`: This is a series of tests including linting, unit tests and library checks which run on every pull request.
- `integration_test.yaml`: This runs the suite of integration tests included with the charm and runs on every pull request.
- `test_and_publish_charm.yaml`: This runs either by manual dispatch or on every push to the main branch or a special track/** branch. Once a PR is merged with one of these branches, this workflow runs to ensure the tests have passed before building the charm and publishing the new version to the edge channel on Charmhub.
- `promote_charm.yaml`: This is a manually triggered workflow which publishes the charm currently on the edge channel to the stable channel on Charmhub.

These tests validate extensive linting and formatting rules. Before creating a PR, please run `tox` to ensure proper formatting and linting is performed.
