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
