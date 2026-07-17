#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import csv
import hashlib
import io
import json
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from simulator.log import info, log_header


TESTDATA_UID = "simulator-report-testdata"
TESTDATA_NAME = "Simulator Report TestData"
TESTDATA_TYPE = "testdata"


def exit_with_error(text):
    print(f"ERROR: {text}", file=sys.stderr)
    raise SystemExit(1)


def mkdir(path):
    Path(path).mkdir(parents=True, exist_ok=True)


class PerftestReportGrafanaCli:

    def __init__(self, argv):
        parser = argparse.ArgumentParser(
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
            description="Generates and installs Grafana dashboards from a Simulator report directory.")
        parser.add_argument("report_path", help="Path to a generated report directory containing report.csv.")
        parser.add_argument("--grafana-url", help="Grafana base URL. Defaults to the observability host from inventory.yaml.")
        parser.add_argument("--folder", help="Grafana folder title. Defaults to the report timestamp.")
        parser.add_argument("--output-dir", help="Where generated dashboard JSON files are written.")
        parser.add_argument("--no-install", action="store_true", help="Only generate dashboard JSON files.")
        parser.add_argument("--overwrite", action="store_true", help="Overwrite existing dashboards with matching UIDs.")
        args = parser.parse_args(argv)

        command = ReportGrafanaCommand(
            report_path=args.report_path,
            grafana_url=args.grafana_url,
            folder=args.folder,
            output_dir=args.output_dir,
            install=not args.no_install,
            overwrite=args.overwrite,
        )
        command.run()


class ReportGrafanaCommand:

    def __init__(self, report_path, grafana_url=None, folder=None, output_dir=None, install=True, overwrite=False):
        self.report = ReportData(report_path)
        self.grafana_url = grafana_url
        self.folder_title = folder or f"Simulator Report {self.report.timestamp}"
        self.output_dir = Path(output_dir) if output_dir else self.report.path / "grafana-dashboards"
        self.install = install
        self.overwrite = overwrite

    def run(self):
        log_header("Generating Grafana dashboards from report")
        generator = ReportDashboardGenerator(self.report, self.folder_title)
        dashboards = generator.generate()
        mkdir(str(self.output_dir))

        written = []
        for dashboard in dashboards:
            path = self.output_dir / f"{dashboard['uid']}.json"
            with open(path, "w") as file:
                json.dump(dashboard, file, indent=2)
                file.write("\n")
            written.append(path)

        info(f"Report: {self.report.path}")
        info(f"Folder: {self.folder_title}")
        for path in written:
            info(f"Generated dashboard: {path}")

        if not self.install:
            info("Installation skipped because --no-install was set.")
            info("Import the JSON files in Grafana, or rerun without --no-install to install through the Grafana API.")
            return

        grafana_url = self.grafana_url or infer_grafana_url()
        client = GrafanaClient(grafana_url)
        installer = GrafanaDashboardInstaller(client, self.folder_title, self.overwrite)
        urls = installer.install(dashboards)

        log_header("Grafana report dashboards installed")
        info("No Grafana restart is required because dashboards were imported through the HTTP API.")
        for title, url in urls:
            info(f"{title}: {url}")


class ReportData:

    def __init__(self, report_path):
        self.path = Path(report_path).expanduser().resolve()
        if not self.path.is_dir():
            exit_with_error(f"Report path [{self.path}] does not exist or is not a directory.")
        if not (self.path / "report.csv").exists():
            exit_with_error(f"Report path [{self.path}] does not contain report.csv.")
        self.timestamp = self._timestamp()

    def _timestamp(self):
        parent = self.path.parent.name
        if re.match(r"^\d{2}-\d{2}-\d{4}_\d{2}-\d{2}-\d{2}$", parent):
            return parent
        with open(self.path / "report.csv", newline="") as file:
            rows = list(csv.DictReader(file))
        if rows and rows[0].get("run_label"):
            return sanitize_title(rows[0]["run_label"])
        return sanitize_title(parent or self.path.name)

    def report_rows(self):
        with open(self.path / "report.csv", newline="") as file:
            return list(csv.DictReader(file))

    def csv_series_files(self, relative_dir):
        directory = self.path / relative_dir
        if not directory.is_dir():
            return []
        return sorted(path for path in directory.glob("*.csv") if path.is_file())

    def data_csv_columns(self):
        data_csv = self.path / "data.csv"
        if not data_csv.exists():
            return []
        with open(data_csv, newline="") as file:
            reader = csv.reader(file)
            try:
                return next(reader)
            except StopIteration:
                return []


class ReportDashboardGenerator:

    def __init__(self, report, folder_title):
        self.report = report
        self.folder_title = folder_title
        self.uid_prefix = f"sim-report-{hashlib.sha1(str(report.path).encode('utf-8')).hexdigest()[:12]}"
        bind_data_csv_content(report.path)

    def generate(self):
        dashboards = [self.summary_dashboard()]
        latency = self.latency_dashboard()
        if latency is not None:
            dashboards.append(latency)
        operations = self.operations_dashboard()
        if operations is not None:
            dashboards.append(operations)
        system = self.system_dashboard()
        if system is not None:
            dashboards.append(system)
        return dashboards

    def summary_dashboard(self):
        rows = self.report.report_rows()
        panels = [
            text_panel(
                1,
                "",
                0,
                0,
                24,
                7,
                f"## Simulator Report Summary\n\n"
                f"Generated from `{self.report.path}`.\n\n"
                "Use this dashboard to identify the tested benchmarks, compare throughput, "
                "and spot latency-tail outliers. High p99/max with stable average latency usually means "
                "short disruption windows or intermittent stalls rather than constant slowness."
            ),
            text_panel(2, "Run Summary", 0, 7, 24, 9, report_rows_to_markdown(rows)),
        ]
        return dashboard(
            uid=f"{self.uid_prefix}-summary",
            title=f"{self.folder_title} - Summary",
            panels=panels,
        )

    def latency_dashboard(self):
        files = self.report.csv_series_files("latency")
        if not files:
            return None

        selected = select_metric_files(files, [
            "Total_Mean",
            "Total_p75",
            "Total_p90",
            "Total_p99",
            "Total_p99.9",
            "Total_Max",
            "Total_Throughput",
            "Total_Count",
            "Int_Mean",
            "Int_p75",
            "Int_p99",
            "Int_Max",
        ])
        if not selected:
            selected = files[:12]

        panels = [
            text_panel(
                1,
                "",
                0,
                0,
                24,
                6,
                "## Latency\n\n"
                "These panels are generated from the report latency CSV files, which are derived from Simulator HDR data. "
                "Use `Total_*` panels for run-level latency and `Int_*` panels for interval behavior. "
                "A p99 or max spike with unchanged throughput usually marks a short stall; a sustained rise in mean, p75, and p99 "
                "indicates broad latency degradation."
            )
        ]
        panels.extend(csv_panels(selected, first_id=2, start_y=6, unit="µs"))
        return dashboard(
            uid=f"{self.uid_prefix}-latency",
            title=f"{self.folder_title} - Latency",
            panels=panels,
        )

    def operations_dashboard(self):
        files = self.report.csv_series_files("operations")
        if not files:
            return None
        panels = [
            text_panel(
                1,
                "",
                0,
                0,
                24,
                6,
                "## Operations\n\n"
                "These charts show operation throughput from the generated report. "
                "During a failover, throughput may dip briefly; recovery is indicated by throughput returning to its pre-event band "
                "without a long tail of degraded operation rate."
            )
        ]
        panels.extend(csv_panels(files, first_id=2, start_y=6, unit="ops"))
        return dashboard(
            uid=f"{self.uid_prefix}-operations",
            title=f"{self.folder_title} - Operations",
            panels=panels,
        )

    def system_dashboard(self):
        columns = self.report.data_csv_columns()
        dstat_columns = [column for column in columns if column.startswith("dstat::")]
        if not dstat_columns:
            return None
        groups = {
            "CPU Usage": ["total usage:usr", "total usage:sys", "total usage:wai"],
            "Memory": ["used", "free", "cach", "buf"],
            "Disk IO": ["dsk/total:read", "dsk/total:writ"],
            "Network IO": ["net/total:recv", "net/total:send"],
            "Load Average": ["1m", "5m", "15m"],
            "Interrupts and Context Switches": ["int", "csw"],
        }
        panels = [
            text_panel(
                1,
                "",
                0,
                0,
                24,
                6,
                "## System Resources\n\n"
                "These panels come from dstat columns in report `data.csv`. "
                "Use them to correlate latency and throughput changes with CPU saturation, memory pressure, disk/network IO, "
                "and load spikes on Simulator agents."
            )
        ]
        panel_id = 2
        y = 6
        for title, metrics in groups.items():
            selected = [column for column in dstat_columns if any(f"::{metric}::" in column for metric in metrics)]
            if not selected:
                continue
            panels.append(data_csv_panel(panel_id, title, selected[:8], 0 if panel_id % 2 == 0 else 12, y, unit=unit_for_system_panel(title)))
            panel_id += 1
            if panel_id % 2 == 0:
                y += 8
        return dashboard(
            uid=f"{self.uid_prefix}-system",
            title=f"{self.folder_title} - System",
            panels=panels,
        )


class GrafanaDashboardInstaller:

    def __init__(self, client, folder_title, overwrite):
        self.client = client
        self.folder_title = folder_title
        self.overwrite = overwrite

    def install(self, dashboards):
        self.ensure_testdata_datasource()
        folder_uid = f"sim-report-{hashlib.sha1(self.folder_title.encode('utf-8')).hexdigest()[:16]}"
        self.client.ensure_folder(folder_uid, self.folder_title)
        urls = []
        for item in dashboards:
            response = self.client.import_dashboard(item, folder_uid, self.overwrite)
            dashboard_url = response.get("url") or f"/d/{item['uid']}"
            urls.append((item["title"], self.client.base_url + dashboard_url))
        return urls

    def ensure_testdata_datasource(self):
        payload = {
            "name": TESTDATA_NAME,
            "uid": TESTDATA_UID,
            "type": TESTDATA_TYPE,
            "access": "proxy",
            "isDefault": False,
        }
        try:
            self.client.post("/api/datasources", payload)
        except GrafanaConflict:
            return


class GrafanaClient:

    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")

    def ensure_folder(self, uid, title):
        try:
            self.get(f"/api/folders/{uid}")
            return
        except GrafanaNotFound:
            self.post("/api/folders", {"uid": uid, "title": title})

    def import_dashboard(self, dashboard_json, folder_uid, overwrite):
        return self.post("/api/dashboards/db", {
            "dashboard": dashboard_json,
            "folderUid": folder_uid,
            "overwrite": overwrite,
        })

    def get(self, path):
        return self._request("GET", path)

    def post(self, path, payload):
        return self._request("POST", path, payload)

    def _request(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {}
        except HTTPError as e:
            if e.code == 404:
                raise GrafanaNotFound()
            if e.code == 409:
                raise GrafanaConflict()
            body = e.read().decode("utf-8", errors="replace")
            exit_with_error(f"Grafana API failed [{method} {path}] status={e.code}: {body}")
        except URLError as e:
            exit_with_error(f"Could not connect to Grafana at [{self.base_url}]: {e}")


class GrafanaNotFound(Exception):
    pass


class GrafanaConflict(Exception):
    pass


def dashboard(uid, title, panels):
    return {
        "uid": uid,
        "title": title,
        "schemaVersion": 38,
        "version": 1,
        "refresh": "",
        "time": {
            "from": "1970-01-01T00:00:00Z",
            "to": "1970-01-01T00:15:00Z",
        },
        "tags": ["simulator", "report"],
        "panels": panels,
    }


def text_panel(panel_id, title, x, y, w, h, content):
    return {
        "id": panel_id,
        "type": "text",
        "title": title,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "options": {"mode": "markdown", "content": content},
        "transparent": True,
    }


def csv_panels(files, first_id, start_y, unit):
    panels = []
    y = start_y
    panel_id = first_id
    for index, path in enumerate(files):
        x = 0 if index % 2 == 0 else 12
        panels.append(csv_file_panel(panel_id, path, x, y, unit))
        panel_id += 1
        if index % 2 == 1:
            y += 8
    return panels


def csv_file_panel(panel_id, path, x, y, unit):
    title = Path(path).stem.replace("_", " ")
    return {
        "id": panel_id,
        "type": "timeseries",
        "title": title,
        "description": chart_description(title),
        "datasource": testdata_datasource(),
        "gridPos": {"x": x, "y": y, "w": 12, "h": 8},
        "targets": [csv_target("A", time_metric_value_csv_content(path))],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "decimals": 2,
                "thresholds": {"mode": "absolute", "steps": [{"color": "blue", "value": 0}]},
            },
            "overrides": [],
        },
        "options": legend_options(),
        "transformations": csv_content_transformations(),
    }


def data_csv_panel(panel_id, title, columns, x, y, unit):
    return {
        "id": panel_id,
        "type": "timeseries",
        "title": title,
        "description": chart_description(title),
        "datasource": testdata_datasource(),
        "gridPos": {"x": x, "y": y, "w": 12, "h": 8},
        "targets": [csv_target("A", data_csv_content(columns))],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "decimals": 2,
                "thresholds": {"mode": "absolute", "steps": [{"color": "blue", "value": 0}]},
            },
            "overrides": [],
        },
        "options": legend_options(),
        "transformations": csv_content_transformations(),
    }


def csv_target(ref_id, csv_content):
    return {
        "refId": ref_id,
        "datasource": testdata_datasource(),
        "scenarioId": "csv_content",
        "queryType": "csv_content",
        "csvContent": csv_content,
    }


def testdata_datasource():
    return {"type": TESTDATA_TYPE, "uid": TESTDATA_UID}


def legend_options():
    return {
        "legend": {"displayMode": "table", "placement": "bottom", "calcs": ["lastNotNull"]},
        "tooltip": {"mode": "multi", "sort": "none"},
    }


def csv_content_transformations():
    return [
        {
            "id": "convertFieldType",
            "options": {
                "conversions": [
                    {"targetField": "time", "destinationType": "time"},
                    {"targetField": "value", "destinationType": "number"},
                ],
            },
        },
        {
            "id": "partitionByValues",
            "options": {"fields": ["metric"]},
        },
    ]


def time_metric_value_csv_content(path):
    with open(path, newline="") as file:
        rows = list(csv.reader(file))
    if not rows:
        return empty_metric_csv()
    header = rows[0]
    if len(header) < 2:
        return empty_metric_csv()

    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["time", "metric", "value"])
    for row in rows[1:]:
        if not row:
            continue
        timestamp = normalize_time(row[0])
        for index, metric in enumerate(header[1:], start=1):
            if index >= len(row) or not is_number(row[index]):
                continue
            writer.writerow([timestamp, metric, row[index]])
    return output.getvalue()


def data_csv_content(columns):
    raise RuntimeError("data_csv_content requires bind_data_csv_content before use")


def bind_data_csv_content(report_path):
    data_csv_path = Path(report_path) / "data.csv"

    def content(columns):
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(["time", "metric", "value"])
        with open(data_csv_path, newline="") as file:
            reader = csv.DictReader(file)
            for row in reader:
                timestamp = normalize_time(row.get("time", ""))
                for column in columns:
                    value = row.get(column, "")
                    if not is_number(value):
                        continue
                    writer.writerow([timestamp, short_column_name(column), value])
        return output.getvalue()

    globals()["data_csv_content"] = content


def empty_metric_csv():
    return "time,metric,value\n"


def normalize_time(value):
    value = str(value or "").strip()
    if not value:
        return value
    if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", value):
        return value.replace(" ", "T") + "Z"
    return value


def is_number(value):
    try:
        float(str(value).strip())
        return True
    except ValueError:
        return False


def report_rows_to_markdown(rows):
    if not rows:
        return "No rows found in `report.csv`."
    headers = rows[0].keys()
    result = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        result.append("| " + " | ".join(str(row.get(header, "")) for header in headers) + " |")
    result.append("")
    result.append("Interpretation: compare throughput with tail latency. A healthy run has stable throughput and bounded p95/p99/max latency. Large max values with modest p95/p99 usually indicate isolated stalls.")
    return "\n".join(result)


def select_metric_files(files, preferred_prefixes):
    selected = []
    for prefix in preferred_prefixes:
        selected.extend(path for path in files if path.name.startswith(prefix + "_") or path.stem == prefix)
    # preserve order while removing duplicates
    seen = set()
    result = []
    for item in selected:
        if item in seen:
            continue
        result.append(item)
        seen.add(item)
    return result[:16]


def chart_description(title):
    lower = title.lower()
    if "throughput" in lower:
        return "Operation rate over time. Look for drops during failover and verify recovery to the previous band."
    if "p99" in lower or "max" in lower:
        return "Tail latency over time. Spikes indicate stalls, pauses, retries, or failover windows."
    if "mean" in lower or "p75" in lower or "p90" in lower:
        return "Central and upper latency trend. Sustained increases indicate broad performance degradation."
    if "cpu" in lower:
        return "CPU utilization. High user/system values that coincide with latency increases suggest CPU pressure."
    if "memory" in lower:
        return "Memory usage. Declining free memory or rising used memory can explain latency and GC pressure."
    if "network" in lower:
        return "Network throughput. Spikes or plateaus can indicate replication, migration, or bottlenecks."
    return "Generated from Simulator report data. Correlate changes with throughput, latency, and failover timing."


def unit_for_system_panel(title):
    if title == "CPU Usage":
        return "percent"
    if title in ("Memory", "Disk IO", "Network IO"):
        return "bytes"
    return "short"


def short_column_name(column):
    parts = column.split("::")
    metric = parts[1] if len(parts) > 1 else column
    agent = next((part.replace("agent_id==", "") for part in parts if part.startswith("agent_id==")), None)
    return f"{metric} {agent}" if agent else metric


def safe_csv_header(value):
    return str(value).replace(",", " ")


def stable_uid(value):
    slug = re.sub(r"[^a-zA-Z0-9-]+", "-", value).strip("-").lower()
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
    if not slug:
        slug = "report"
    return f"{slug[:30]}-{digest}"


def sanitize_title(value):
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-")


def infer_grafana_url():
    try:
        from inventory import load_hosts
        hosts = load_hosts(host_pattern="observability")
    except Exception as e:
        exit_with_error(f"Could not infer Grafana URL from inventory.yaml: {e}. Use --grafana-url.")
    if not hosts:
        exit_with_error("No observability host found in inventory.yaml. Use --grafana-url.")
    host = hosts[0]
    address = host.get("public_ip") or host.get("private_ip")
    if not address:
        exit_with_error("Observability host has no public_ip or private_ip. Use --grafana-url.")
    return f"http://{address}:3000"
