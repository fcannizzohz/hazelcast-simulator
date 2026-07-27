import argparse
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

import yaml

from simulator.log import info, log_header
from simulator.perftest_report_grafana import ReportDashboardGenerator, ReportData
from simulator.util import exit_with_error, simulator_home


class ObservabilityExportCli:
    def __init__(self, argv):
        parser = argparse.ArgumentParser(
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
            description="Exports a Simulator run and Prometheus snapshot as a local Grafana Compose bundle.",
        )
        parser.add_argument("run_path", help="Run timestamp directory or its report directory.")
        parser.add_argument("--output-dir", help="Destination; defaults to a timestamped directory below the run.")
        parser.add_argument("--overwrite", action="store_true", help="Replace an existing output directory.")
        args = parser.parse_args(argv)
        ObservabilityExportCommand(args.run_path, args.output_dir, args.overwrite).run()


class ObservabilityExportCommand:
    def __init__(self, run_path, output_dir=None, overwrite=False):
        self.report = ReportData(run_path)
        self.run_path = self.report.run_path
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.output_dir = Path(output_dir).expanduser() if output_dir else self.run_path / f"observability-export-{timestamp}"
        self.overwrite = overwrite

    def run(self):
        if self.output_dir.exists():
            if not self.overwrite:
                exit_with_error(f"Export directory already exists: {self.output_dir}. Use --overwrite or --output-dir.")
            shutil.rmtree(self.output_dir)
        if not Path("inventory.yaml").is_file() or not Path("inventory_plan.yaml").is_file():
            exit_with_error("observability export requires inventory.yaml and inventory_plan.yaml in the project directory")

        plan = yaml.safe_load(Path("inventory_plan.yaml").read_text()) or {}
        if not (plan.get("observability") or {}).get("enabled", False):
            exit_with_error("observability export requires observability.enabled: true")

        log_header("Exporting observability bundle")
        self._create_layout()
        self._copy_results()
        self._write_report_dashboards()
        self._copy_operational_assets()
        snapshot = self._export_prometheus_snapshot(plan)
        self._write_bundle_files(snapshot)
        info(f"Observability export: {self.output_dir.resolve()}")
        info("Start it with: docker compose up -d")

    def _create_layout(self):
        for relative in ("results", "prometheus/data", "prometheus/rules", "grafana/dashboards/hazelcast",
                         "grafana/dashboards/simulator-run", "grafana/provisioning/dashboards",
                         "grafana/provisioning/datasources"):
            (self.output_dir / relative).mkdir(parents=True, exist_ok=True)

    def _copy_results(self):
        output_root = self.output_dir.resolve()

        def ignore_exports(directory, names):
            ignored = [name for name in names if name.startswith("observability-export-")]
            current = Path(directory).resolve()
            try:
                relative_output = output_root.relative_to(current)
            except ValueError:
                return ignored
            if relative_output.parts:
                output_child = relative_output.parts[0]
                if output_child in names and output_child not in ignored:
                    ignored.append(output_child)
            return ignored

        shutil.copytree(
            self.run_path, self.output_dir / "results" / self.run_path.name,
            dirs_exist_ok=True, ignore=ignore_exports,
        )

    def _write_report_dashboards(self):
        dashboards = ReportDashboardGenerator(
            self.report, f"Simulator Run {self.report.timestamp}"
        ).generate()
        target = self.output_dir / "grafana/dashboards/simulator-run"
        for dashboard in dashboards:
            (target / f"{dashboard['uid']}.json").write_text(json.dumps(dashboard, indent=2) + "\n")

    def _copy_operational_assets(self):
        root = Path(simulator_home) / "observability"
        if not root.is_dir():
            exit_with_error(f"Could not find bundled observability assets at {root}")
        shutil.copy2(root / "prometheus/prometheus.yml", self.output_dir / "prometheus/prometheus.yml")
        shutil.copytree(root / "prometheus/rules", self.output_dir / "prometheus/rules", dirs_exist_ok=True)
        shutil.copytree(root / "grafana/dashboards", self.output_dir / "grafana/dashboards/hazelcast", dirs_exist_ok=True)

    def _export_prometheus_snapshot(self, plan):
        if plan.get("provisioner") == "kubernetes":
            return self._export_kubernetes_snapshot(plan)
        return self._export_aws_snapshot()

    def _snapshot_api(self, base_url):
        request = Request(f"{base_url.rstrip('/')}/api/v1/admin/tsdb/snapshot", data=b"", method="POST")
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as error:
            exit_with_error(f"Could not create Prometheus TSDB snapshot: {error}")
        name = ((payload.get("data") or {}).get("name"))
        if not name or "/" in name or name in (".", ".."):
            exit_with_error(f"Prometheus returned an invalid snapshot name: {name!r}")
        return name

    def _export_kubernetes_snapshot(self, plan):
        from simulator.inventory_kubernetes import _start_port_forward, kubectl_base, namespace

        snapshot = self._snapshot_api(_start_port_forward(plan, "prometheus", 9090))
        ns = namespace(plan)
        pod_result = subprocess.run(
            kubectl_base(plan) + ["-n", ns, "get", "pods", "-l", "app=prometheus", "-o", "jsonpath={.items[0].metadata.name}"],
            text=True, capture_output=True,
        )
        pod = pod_result.stdout.strip()
        if pod_result.returncode or not pod:
            exit_with_error("Could not find the Prometheus pod for snapshot retrieval")
        destination = self.output_dir / "prometheus/data"
        source = f"{ns}/{pod}:/prometheus/snapshots/{snapshot}/."
        result = subprocess.run(kubectl_base(plan) + ["cp", source, str(destination)], text=True)
        if result.returncode:
            exit_with_error("Could not copy Prometheus snapshot from Kubernetes")
        subprocess.run(kubectl_base(plan) + ["-n", ns, "exec", pod, "--", "rm", "-rf", f"/prometheus/snapshots/{snapshot}"], check=False)
        return {"provider": "kubernetes", "name": snapshot}

    def _export_aws_snapshot(self):
        from inventory import load_hosts
        from simulator.remote import copy_from_remote, remote_exec

        hosts = load_hosts(host_pattern="observability")
        if not hosts:
            exit_with_error("Could not find an observability host in inventory.yaml")
        host = hosts[0]
        command = "curl -fsS -XPOST http://localhost:9090/api/v1/admin/tsdb/snapshot"
        code, output = remote_exec(host, command)
        if code:
            exit_with_error("Could not create Prometheus TSDB snapshot on the observability host")
        try:
            snapshot = ((json.loads(output).get("data") or {}).get("name"))
        except ValueError:
            snapshot = None
        if not snapshot or "/" in snapshot:
            exit_with_error(f"Prometheus returned an invalid snapshot response: {output!r}")
        remote_dir = f"/tmp/simulator-prometheus-export-{snapshot}"
        code, _ = remote_exec(host, f"rm -rf {remote_dir} && sudo docker cp simulator-prometheus:/prometheus/snapshots/{snapshot} {remote_dir}")
        if code:
            exit_with_error("Could not retrieve Prometheus snapshot from the observability container")
        try:
            result = copy_from_remote(host, f"{remote_dir}/", str(self.output_dir / "prometheus/data"))
            if result not in (0, None):
                exit_with_error("Could not copy Prometheus snapshot to the local export directory")
        finally:
            remote_exec(host, f"rm -rf {remote_dir} && sudo docker exec simulator-prometheus rm -rf /prometheus/snapshots/{snapshot}", check=False)
        return {"provider": "aws", "name": snapshot}

    def _write_bundle_files(self, snapshot):
        timestamp = datetime.now(timezone.utc).isoformat()
        manifest = {
            "exported_at": timestamp,
            "source_run": str(self.run_path),
            "report_timestamp": self.report.timestamp,
            "prometheus_snapshot": snapshot,
            "images": {"prometheus": "prom/prometheus:latest", "grafana": "grafana/grafana:latest"},
        }
        (self.output_dir / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
        (self.output_dir / "docker-compose.yml").write_text(_COMPOSE)
        (self.output_dir / "grafana/provisioning/datasources/datasources.yml").write_text(_DATASOURCES)
        (self.output_dir / "grafana/provisioning/dashboards/dashboards.yml").write_text(_DASHBOARD_PROVIDERS)
        (self.output_dir / "README.md").write_text(_README)


_COMPOSE = """services:
  prometheus:
    image: prom/prometheus:latest
    ports: [\"9090:9090\"]
    command: [\"--config.file=/etc/prometheus/prometheus.yml\", \"--storage.tsdb.path=/prometheus\"]
    volumes:
      - ./prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - ./prometheus/rules:/etc/prometheus/rules:ro
      - ./prometheus/data:/prometheus
  grafana:
    image: grafana/grafana:latest
    ports: [\"3000:3000\"]
    environment:
      GF_AUTH_ANONYMOUS_ENABLED: \"true\"
      GF_AUTH_ANONYMOUS_ORG_ROLE: Admin
      GF_AUTH_DISABLE_LOGIN_FORM: \"true\"
    volumes:
      - ./grafana/provisioning:/etc/grafana/provisioning:ro
      - ./grafana/dashboards:/var/lib/grafana/dashboards:ro
    depends_on: [prometheus]
"""

_DATASOURCES = """apiVersion: 1
datasources:
  - name: Prometheus
    uid: prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true
  - name: Simulator Report TestData
    uid: simulator-report-testdata
    type: testdata
    access: proxy
"""

_DASHBOARD_PROVIDERS = """apiVersion: 1
providers:
  - name: Hazelcast
    folder: Hazelcast
    type: file
    options: {path: /var/lib/grafana/dashboards/hazelcast}
  - name: Simulator Run
    folder: Simulator Run
    type: file
    options: {path: /var/lib/grafana/dashboards/simulator-run}
"""

_README = """# Simulator observability export

Start the local stack with `docker compose up -d`. Open Grafana at
http://localhost:3000 and Prometheus at http://localhost:9090. The **Hazelcast**
folder uses the exported Prometheus snapshot; **Simulator Run** dashboards embed
the exported report data. Stop the stack with `docker compose down`.
"""
