## Prepare helm

```sh
helm repo add apache https://pulsar.apache.org/charts
```

```sh
helm repo update
```

## Deploy chart

1. Create pulsar namespace

```sh
kubectl create namespace pulsar
```

2. Deploy chart

```sh
helm install pulsar apache/pulsar \
     --namespace pulsar \
     -f charts/pulsar/values.yaml \
     --set initialize=true
```

3. Upgrade chart

```sh
helm upgrade pulsar apache/pulsar \
     --namespace pulsar \
     -f charts/pulsar/values.yaml
```

### Notes

- `functions` component was disabled because it caused an error at start of `broker`
- `pulsar-manager` component was disabled for now (error related to postgres?)
- pods `resources` where adjusted to satisfy gke autopilot requirements to enable affinity
