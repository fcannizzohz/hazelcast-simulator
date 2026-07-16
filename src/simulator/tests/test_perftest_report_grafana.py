import csv
import tempfile
import unittest
from pathlib import Path

from simulator.perftest_report_grafana import (
    GrafanaConflict,
    GrafanaDashboardInstaller,
    ReportDashboardGenerator,
    ReportData,
    ReportGrafanaCommand,
)


class TestPerftestReportGrafana(unittest.TestCase):

    def test_report_timestamp_uses_parent_run_directory(self):
        with synthetic_report() as report_dir:
            report = ReportData(report_dir)

        self.assertEqual("16-07-2026_08-23-37", report.timestamp)

    def test_generates_summary_latency_operations_and_system_dashboards(self):
        with synthetic_report() as report_dir:
            report = ReportData(report_dir)
            dashboards = ReportDashboardGenerator(report, "Simulator Report 16-07-2026_08-23-37").generate()

        titles = [dashboard["title"] for dashboard in dashboards]
        self.assertIn("Simulator Report 16-07-2026_08-23-37 - Summary", titles)
        self.assertIn("Simulator Report 16-07-2026_08-23-37 - Latency", titles)
        self.assertIn("Simulator Report 16-07-2026_08-23-37 - Operations", titles)
        self.assertIn("Simulator Report 16-07-2026_08-23-37 - System", titles)
        self.assertEqual(len({dashboard["uid"] for dashboard in dashboards}), len(dashboards))

    def test_timeseries_targets_use_numeric_value_field(self):
        with synthetic_report() as report_dir:
            report = ReportData(report_dir)
            dashboards = ReportDashboardGenerator(report, "Simulator Report 16-07-2026_08-23-37").generate()

        targets = []
        for dashboard in dashboards:
            for panel in dashboard["panels"]:
                targets.extend(panel.get("targets", []))

        self.assertGreater(len(targets), 0)
        for target in targets:
            self.assertEqual("csv_content", target["scenarioId"])
            self.assertEqual("csv_content", target["queryType"])
            rows = list(csv.reader(target["csvContent"].splitlines()))
            self.assertEqual(["time", "metric", "value"], rows[0])
            self.assertGreater(len(rows), 1)
            for row in rows[1:]:
                self.assertEqual(3, len(row))
                self.assertTrue(row[0].endswith("Z"))
                float(row[2])

    def test_latency_dashboard_starts_with_total_and_interval_aggregate_panels(self):
        with synthetic_report() as report_dir:
            report = ReportData(report_dir)
            dashboards = ReportDashboardGenerator(report, "Simulator Report 16-07-2026_08-23-37").generate()

        latency = next(dashboard for dashboard in dashboards if dashboard["title"].endswith(" - Latency"))
        self.assertEqual("All Total Latency Metrics", latency["panels"][1]["title"])
        self.assertEqual("All Interval Latency Metrics", latency["panels"][2]["title"])
        self.assertIn("Total p99 one backup.get", latency["panels"][1]["targets"][0]["csvContent"])
        self.assertIn("Int Mean one backup.get", latency["panels"][2]["targets"][0]["csvContent"])
        self.assertNotIn("Total Count one backup.get", latency["panels"][1]["targets"][0]["csvContent"])
        self.assertNotIn("Total Throughput one backup.get", latency["panels"][1]["targets"][0]["csvContent"])
        self.assertNotIn("Int Count one backup.get", latency["panels"][2]["targets"][0]["csvContent"])
        self.assertNotIn("Int Throughput one backup.get", latency["panels"][2]["targets"][0]["csvContent"])

    def test_incomplete_run_directory_generates_available_dashboards_and_errors(self):
        with synthetic_incomplete_run() as run_dir:
            report = ReportData(run_dir)
            dashboards = ReportDashboardGenerator(report, "Simulator Report 15-07-2026_11-43-48").generate()

        titles = [dashboard["title"] for dashboard in dashboards]
        self.assertIn("Simulator Report 15-07-2026_11-43-48 - Summary", titles)
        self.assertIn("Simulator Report 15-07-2026_11-43-48 - System", titles)
        self.assertIn("Simulator Report 15-07-2026_11-43-48 - Errors", titles)
        self.assertNotIn("Simulator Report 15-07-2026_11-43-48 - Latency", titles)
        self.assertNotIn("Simulator Report 15-07-2026_11-43-48 - Operations", titles)

        summary = next(dashboard for dashboard in dashboards if dashboard["title"].endswith(" - Summary"))
        errors = next(dashboard for dashboard in dashboards if dashboard["title"].endswith(" - Errors"))
        self.assertIn("report.csv", summary["panels"][1]["options"]["content"])
        self.assertIn("MissingLicenseException", errors["panels"][1]["options"]["content"])

    def test_no_install_writes_dashboard_json(self):
        with synthetic_report() as report_dir:
            output_dir = Path(report_dir) / "out"
            command = ReportGrafanaCommand(
                report_path=report_dir,
                grafana_url="http://grafana.example",
                output_dir=str(output_dir),
                install=False,
            )
            command.run()

            generated = sorted(output_dir.glob("*.json"))

        self.assertGreaterEqual(len(generated), 3)

    def test_installer_creates_datasource_folder_and_dashboards(self):
        client = FakeGrafanaClient()
        installer = GrafanaDashboardInstaller(client, "Simulator Report test", overwrite=True)

        urls = installer.install([
            {"uid": "dashboard-one", "title": "Dashboard One", "panels": []},
            {"uid": "dashboard-two", "title": "Dashboard Two", "panels": []},
        ])

        self.assertEqual(2, len(urls))
        self.assertEqual("POST", client.calls[0][0])
        self.assertEqual("/api/datasources", client.calls[0][1])
        self.assertEqual("GET", client.calls[1][0])
        self.assertEqual("POST", client.calls[2][0])
        self.assertEqual("/api/folders", client.calls[2][1])
        self.assertEqual("/api/dashboards/db", client.calls[3][1])
        self.assertEqual("/api/dashboards/db", client.calls[4][1])


class FakeGrafanaClient:

    base_url = "http://grafana.example"

    def __init__(self):
        self.calls = []

    def post(self, path, payload):
        self.calls.append(("POST", path, payload))
        if path == "/api/datasources":
            raise GrafanaConflict()
        if path == "/api/dashboards/db":
            uid = payload["dashboard"]["uid"]
            return {"url": f"/d/{uid}"}
        return {}

    def get(self, path):
        self.calls.append(("GET", path, None))
        from simulator.perftest_report_grafana import GrafanaNotFound
        raise GrafanaNotFound()

    def ensure_folder(self, uid, title):
        try:
            self.get(f"/api/folders/{uid}")
        except Exception:
            self.post("/api/folders", {"uid": uid, "title": title})

    def import_dashboard(self, dashboard_json, folder_uid, overwrite):
        return self.post("/api/dashboards/db", {
            "dashboard": dashboard_json,
            "folderUid": folder_uid,
            "overwrite": overwrite,
        })


class synthetic_report:

    def __enter__(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        base = Path(self.tmpdir.name) / "runs" / "sample" / "16-07-2026_08-23-37" / "report"
        base.mkdir(parents=True)
        (base / "latency").mkdir()
        (base / "operations").mkdir()
        write_csv(base / "report.csv", [
            ["run_label", "benchmark", "75%(us)", "95%(us)", "99%(us)", "max(us)", "operations", "duration(ms)", "throughput"],
            ["16-07-2026_08-23-37", "one_backup.get", "706.047", "857.599", "999.935", "1121976.319", "12414241", "297021", "41795.83"],
        ])
        write_csv(base / "latency" / "Total_p99_one_backup.get.csv", [
            ["time", "16-07-2026_08-23-37"],
            ["1970-01-01 00:00:00", "1000"],
            ["1970-01-01 00:00:01", "1010"],
        ])
        write_csv(base / "latency" / "Total_Count_one_backup.get.csv", [
            ["time", "16-07-2026_08-23-37"],
            ["1970-01-01 00:00:00", "10"],
            ["1970-01-01 00:00:01", "11"],
        ])
        write_csv(base / "latency" / "Total_Throughput_one_backup.get.csv", [
            ["time", "16-07-2026_08-23-37"],
            ["1970-01-01 00:00:00", "42000"],
            ["1970-01-01 00:00:01", "42100"],
        ])
        write_csv(base / "latency" / "Int_Mean_one_backup.get.csv", [
            ["time", "16-07-2026_08-23-37"],
            ["1970-01-01 00:00:00", "800"],
            ["1970-01-01 00:00:01", "810"],
        ])
        write_csv(base / "latency" / "Int_Count_one_backup.get.csv", [
            ["time", "16-07-2026_08-23-37"],
            ["1970-01-01 00:00:00", "10"],
            ["1970-01-01 00:00:01", "11"],
        ])
        write_csv(base / "latency" / "Int_Throughput_one_backup.get.csv", [
            ["time", "16-07-2026_08-23-37"],
            ["1970-01-01 00:00:00", "42000"],
            ["1970-01-01 00:00:01", "42100"],
        ])
        write_csv(base / "operations" / "throughput.csv", [
            ["time", "16-07-2026_08-23-37"],
            ["1970-01-01 00:00:00", "42000"],
            ["1970-01-01 00:00:01", "42100"],
        ])
        write_csv(base / "data.csv", [
            [
                "time",
                "dstat::used::run_label==16-07-2026_08-23-37::agent_id==A1",
                "dstat::free::run_label==16-07-2026_08-23-37::agent_id==A1",
                "dstat::total usage:usr::run_label==16-07-2026_08-23-37::agent_id==A1",
            ],
            ["1970-01-01 00:00:00", "100", "200", "10"],
            ["1970-01-01 00:00:01", "110", "190", "20"],
        ])
        self.report_dir = base
        return str(base)

    def __exit__(self, exc_type, exc_value, traceback):
        self.tmpdir.cleanup()


class synthetic_incomplete_run:

    def __enter__(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        base = Path(self.tmpdir.name) / "runs" / "sample" / "15-07-2026_11-43-48"
        report = base / "report"
        worker = base / "A1_W1-127.0.0.1-member"
        report.mkdir(parents=True)
        worker.mkdir()
        write_csv(report / "data.csv", [
            [
                "time",
                "dstat::used::run_label==15-07-2026_11-43-48::agent_id==A1",
                "dstat::free::run_label==15-07-2026_11-43-48::agent_id==A1",
            ],
            ["1970-01-01 00:00:00", "100", "200"],
        ])
        (worker / "worker.log").write_text(
            "11:44:06.260 [main] WARN  com.hazelcast.simulator.utils.ExceptionReporter - Exception #1 detected\n"
            "com.hazelcast.license.exception.MissingLicenseException: The Hazelcast Enterprise license key is not set.\n"
            "11:44:06.268 [main] FATAL com.hazelcast.simulator.worker.Worker - Failed to start Hazelcast Simulator Worker!\n"
        )
        (base / "failures.txt").write_text("Failure[\n   message='Failed to start worker [A1_W1]'\n]\n")
        self.run_dir = base
        return str(base)

    def __exit__(self, exc_type, exc_value, traceback):
        self.tmpdir.cleanup()


def write_csv(path, rows):
    with open(path, "w", newline="") as file:
        writer = csv.writer(file)
        writer.writerows(rows)
