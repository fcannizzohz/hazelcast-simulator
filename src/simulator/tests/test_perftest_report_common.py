import unittest

import pandas as pd

from simulator.perftest_report_common import concat_dataframe_columns


class ConcatDataframeColumnsTest(unittest.TestCase):

    def test_returns_none_when_all_dataframes_are_missing(self):
        self.assertIsNone(concat_dataframe_columns([None, None]))

    def test_ignores_missing_dataframes(self):
        dataframe = pd.DataFrame({"operations": [1]})

        result = concat_dataframe_columns([None, dataframe, None])

        pd.testing.assert_frame_equal(dataframe, result)
