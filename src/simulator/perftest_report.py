#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import csv
import glob
import shutil

from simulator.perftest_report_dstat import report_dstat, analyze_dstat
from simulator.perftest_report_hdr import report_hdr, prepare_hdr, analyze_latency_history
from simulator.perftest_report_operations import report_operations, prepare_operation, analyze_operations
from simulator.util import mkdir, exit_with_error
from simulator.perftest_report_common import *
from simulator.perftest_report_html import HTMLReport


def prepare(config: ReportConfig):
    report_dir = config.report_dir
    if os.path.exists(report_dir):
        basename = os.path.basename(report_dir)
        # protection against accidental deletion of a directory that isn't a report directory
        if "report" not in basename.lower():
            exit_with_error(f"Simulator will not delete '{report_dir}', because it doesn't contain the word 'report'.")
        shutil.rmtree(config.report_dir)
    mkdir(report_dir)
    prepare_operation(config)
    lookup_periods(config)
    prepare_hdr(config)


def analyze(config: ReportConfig):
    all_runs_data = []
    for run_label, run_dir in config.runs.items():
        run_data = analyze_run(config, run_dir, run_label)
        if run_data is None:
            continue

        if not config.preserve_time:
            period = config.periods.get(run_label)
            if period is None:
                start_time_sec = round(run_data.index[0].timestamp())
                end_time_sec = round(run_data.index[-1].timestamp())
                info(f"No period metadata for run_label [{run_label}]; using loaded data bounds.")
            else:
                start_time_sec = round(period.start_time)
                end_time_sec = round(period.end_time)
            if config.warmup_seconds is not None:
                start_time_sec = start_time_sec + config.warmup_seconds
            if config.cooldown_seconds is not None:
                end_time_sec = end_time_sec - config.cooldown_seconds
            run_data = df_trim_time(run_data, start_time_sec, end_time_sec)
            run_data = df_shift_time(run_data, -start_time_sec)
        
        all_runs_data.append(run_data)

    return concat_dataframe_columns(all_runs_data)


def analyze_run(config: ReportConfig, run_dir, run_label):
    info(f"Analyzing run_path:{run_dir}")

    if run_label is None:
        run_label = os.path.basename(run_dir)

    attributes = {"run_label": run_label}

    result = concat_dataframe_columns([
        analyze_operations(run_dir, attributes),
        analyze_latency_history(config.report_dir, attributes),
        analyze_dstat(run_dir, attributes)
    ])

    if result is None:
        info(f"No reportable data found for run_path:{run_dir}; skipping report generation.")

    info(f"Analyzing run_path:{run_dir}: Done")
    return result


def report(config: ReportConfig, df: pd.DataFrame):
    if df is None:
        return

    path_csv = f"{config.report_dir}/data.csv"
    info(f"Writing combined run data to: {path_csv}")
    df.to_csv(path_csv)

    # for column_name in df.columns:
    #     print(column_name)

    report_operations(config, df)
    report_hdr(config, df)
    report_dstat(config, df)

    html_report = HTMLReport(config)
    html_report.make()


def lookup_periods(config):
    for run_label, run_dir in config.runs.items():
        period = None
        for operations_csv_file in __find_operations_csv_files(run_dir):
            file_period = __read_operations_period(operations_csv_file)
            if file_period is None:
                continue

            # Pick the earliest start and latest end across all operation files.
            # This avoids trimming series because worker reporting intervals differ
            # by milliseconds.
            if period is None:
                period = file_period
            else:
                period = Period(
                    min(period.start_time, file_period.start_time),
                    max(period.end_time, file_period.end_time)
                )

        if period is not None:
            config.periods[run_label] = period
        else:
            info(f"No operations period found for run_label [{run_label}]. Time trimming will use loaded data bounds.")


def __find_operations_csv_files(run_dir):
    result = []
    for pattern in (
            f"{run_dir}/operations*.csv",
            f"{run_dir}/*/operations*.csv",
            f"{run_dir}/performance*.csv",
            f"{run_dir}/*/performance*.csv"):
        result.extend(glob.glob(pattern))
    return sorted(set(result))


def __read_operations_period(operations_csv_file):
    if not os.path.isfile(operations_csv_file):
        return None

    start_time = None
    end_time = None
    with open(operations_csv_file) as csv_file:
        csv_reader = csv.DictReader(csv_file)
        for row in csv_reader:
            epoch = row.get("epoch")
            if epoch is None or epoch == "":
                continue
            epoch = float(epoch)
            if start_time is None:
                start_time = epoch
            end_time = epoch

    if start_time is None or end_time is None:
        return None

    return Period(start_time, end_time)

def collect_runs(benchmarks, config: ReportConfig):
    benchmark_dirs = []
    run_names = {}
    last_benchmark = None

    # collect all benchmark directories and the names for the benchmarks
    for benchmark_arg in benchmarks:
        if benchmark_arg.startswith("[") and benchmark_arg.endswith("]"):
            if not last_benchmark:
                info("Benchmark name " + benchmark_arg + " must be preceded with a benchmark directory.")
                exit()
            run_names[last_benchmark] = benchmark_arg[1:len(benchmark_arg) - 1]
            last_benchmark = None
        elif config.compare_last:
            if config.long_label:
                exit_with_error("cannot use --last with --longLabel")
            benchmark_root = benchmark_arg
            if not os.path.exists(benchmark_root):
                exit_with_error("Directory '" + benchmark_root + "' does not exist!")
            subdirectories = sorted(filter(os.path.isdir, glob.glob(benchmark_root + "/*")))

            run_dir = subdirectories[-1]
            if not os.path.exists(run_dir):
                exit_with_error("benchmark directory '" + run_dir + "' does not exist!")

            last_benchmark = benchmark_arg
            benchmark_dirs.append(run_dir)
            run_names[run_dir] = os.path.basename(os.path.normpath(benchmark_root))
        else:
            run_dir = benchmark_arg
            if not os.path.exists(run_dir):
                exit_with_error("benchmark directory '" + run_dir + "' does not exist!")

            last_benchmark = benchmark_arg
            benchmark_dirs.append(run_dir)
            if config.long_label:
                label_prefix = os.path.basename(os.path.dirname(os.path.normpath(run_dir))) + "@"
            else:
                label_prefix = ""
            run_names[run_dir] = label_prefix + os.path.basename(os.path.normpath(run_dir))

    if len(run_names) == 0:
        exit_with_error("No runs were found")
    elif len(run_names) == 1:
        info("Using the following run:")
    else:
        info("Using the following set of runs:")

    for run_dir in benchmark_dirs:
        run_label = run_names[run_dir]
        info(f"       {run_label} {run_dir}")
        config.runs[run_label] = run_dir


class PerfTestReportCli:

    def __init__(self, argv):
        parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter,
                                         description='Creating a benchmark report from one or more benchmarks.')
        parser.add_argument('benchmarks',
                            metavar='B',
                            nargs='+',
                            help='a benchmark to be used in the comparison')
        parser.add_argument('-t', '--time',
                            help='Preserve the real time',
                            action="store_true")
        parser.add_argument('-z', '--zero',
                            help='Let the y-axis start from zero',
                            action="store_true")
        parser.add_argument('--svg',
                            help='Also create svg images',
                            action="store_true")
        parser.add_argument('-o', '--output',
                            nargs=1,
                            default=["report"],
                            help='The output directory for the report. '
                                 "By default a 'report' directory in the working directory is created.")
        parser.add_argument('-w', '--warmup',
                            nargs=1, default=[0],
                            type=int,
                            help='The warmup period in seconds. The warmup removes datapoints from the start.')
        parser.add_argument('-c', '--cooldown',
                            nargs=1, default=[0],
                            type=int,
                            help='The cooldown period in seconds. The cooldown removes datapoints from the end.')
        parser.add_argument('-f', '--full',
                            help='Enable individual worker level diagrams.',
                            action="store_true")
        parser.add_argument('-l', '--last',
                            help='Compare last results from each benchmark',
                            action='store_true')
        parser.add_argument('-ll', '--longLabel',
                            help='Include benchmark name in run label',
                            action='store_true')
        parser.add_argument('--width',
                            nargs=1,
                            default=[1600],
                            type=int,
                            help='The width, in pixels, of the generated images.')
        parser.add_argument('--height',
                            nargs=1,
                            default=[1200],
                            type=int,
                            help='The height, in pixels, of the generated images.')
        args = parser.parse_args(argv)

        gc_logs_found = False

        os.environ['LC_CTYPE'] = "en_US.UTF-8"

        report_dir = os.path.abspath(args.output[0])
        info("Report directory '" + report_dir + "'")

        config = ReportConfig(report_dir)
        config.warmup_seconds = int(args.warmup[0])
        config.cooldown_seconds = int(args.cooldown[0])
        config.image_width_px = int(args.width[0])
        config.image_height_px = int(args.height[0])
        config.worker_reporting = args.full
        config.compare_last = args.last
        config.long_label = args.longLabel
        config.preserve_time = args.time
        config.y_start_from_zero = args.zero
        config.svg = args.svg

        collect_runs(args.benchmarks, config)
        prepare(config)
        df = analyze(config)
        report(config, df)

        if not args.full and gc_logs_found:
            info("gc.log files have been found. Run with -f option to get these plotted.")
