import tempfile
import unittest
from pathlib import Path

from simulator.perftest_report_dstat import analyze_dstat


class DstatReportTest(unittest.TestCase):

    def test_loads_legacy_kubernetes_dstat_filename_and_buff_column(self):
        csv = """preamble\npreamble\npreamble\npreamble\npreamble\nepoch,used,free,buff,cach\n1785309934,1,2,3,4\n"""
        with tempfile.TemporaryDirectory() as run_dir:
            Path(run_dir, "dstat.csv").write_text(csv)

            result = analyze_dstat(run_dir, {"run_label": "smoke"})

        self.assertIsNotNone(result)
        self.assertEqual(4, len(result.columns))
        self.assertTrue(any("dstat::buff::" in column for column in result.columns))


if __name__ == "__main__":
    unittest.main()
