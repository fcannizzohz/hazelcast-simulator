# Hazelcast Simulator Scenarios (CP)

See the `test-*.yaml` files for test specifics. Note that you will need to modify the
`inventory_plan.yaml` to allocate the appropriate number of VMs per-test; same for `hazelcast.yaml`
w.r.t. the CP member count. Everything else runs as documented
[here](https://github.com/hazelcast/hazelcast-simulator/blob/master/README.md). A quick example:

```bash
perftest run test-3member-iatomicreference-128kb-set-alter-cas-casopt.yaml
```

## Observability

This template can provision a separate Prometheus/Grafana host. Set both `mc.count: 1`
and `observability.count: 1` in `inventory_plan.yaml`; Management Center is required
because Prometheus scrapes its `/metrics` endpoint.

```bash
inventory apply
inventory install observability
```

Open Grafana on `http://<observability-public-ip>:3000` and Prometheus on
`http://<observability-public-ip>:9090`.
