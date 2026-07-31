import os
import yaml
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from simulator.observability_export import ObservabilityExportCommand


class TestObservabilityExport(unittest.TestCase):
    def test_writes_timestamped_portable_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "runs" / "sample" / "16-07-2026_08-23-37"
            report = run / "report"
            report.mkdir(parents=True)
            (report / "report.csv").write_text(
                "run_label,benchmark,75%(us),95%(us),99%(us),max(us),operations,duration(ms),throughput\n"
                "16-07-2026_08-23-37,get,1,2,3,4,5,6,7\n"
            )
            (report / "data.csv").write_text("time\n")
            other_report = root / "runs" / "other-test" / "16-07-2026_08-24-37" / "report"
            other_report.mkdir(parents=True)
            (other_report / "report.csv").write_text(
                "run_label,benchmark,75%(us),95%(us),99%(us),max(us),operations,duration(ms),throughput\n"
                "16-07-2026_08-24-37,get,1,2,3,4,5,6,7\n"
            )
            (other_report / "data.csv").write_text("time\n")
            (root / "inventory.yaml").write_text("all:\n  hosts: {}\n")
            (root / "inventory_plan.yaml").write_text("observability:\n  enabled: true\n")
            old_cwd = os.getcwd()
            os.chdir(root)
            try:
                with patch.object(ObservabilityExportCommand, "_export_prometheus_snapshot", return_value={"name": "snapshot"}):
                    command = ObservabilityExportCommand(run)
                    command.run()
                    custom_bundle = run / "custom" / "bundle"
                    ObservabilityExportCommand(run, output_dir=custom_bundle).run()
            finally:
                os.chdir(old_cwd)

            bundle = next(run.glob("observability-export-*"))
            self.assertTrue((bundle / "docker-compose.yml").is_file())
            self.assertTrue((bundle / "manifest.yaml").is_file())
            self.assertTrue((bundle / "results" / run.name / "report" / "report.csv").is_file())
            self.assertTrue(list((bundle / "grafana/dashboards/simulator-run").glob("*.json")))
            manifest = yaml.safe_load((bundle / "manifest.yaml").read_text())
            self.assertEqual(2, len(manifest["runs"]))
            self.assertIn("simulator-report-testdata", (bundle / "grafana/provisioning/datasources/datasources.yml").read_text())
            self.assertFalse((custom_bundle / "results" / run.name / "custom").exists())
