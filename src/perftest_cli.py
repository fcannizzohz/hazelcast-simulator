#!/usr/bin/env python3
import os
import sys
import argparse

usage = '''perftest <command> [<args>]

The available commands are:
    create      Creates a new performance test based on a template.
    clone       Clones an existing performance test.
    collect     Collects the performance test data and stores it in result.yaml.
    run         Runs a tests.yaml which is a self contained set of tests
    kill_java   Kills all Java processes   
    report      Generate performance report 
    report_grafana Generate Grafana dashboards from a performance report
    export_observability Export a run and Prometheus snapshot as a local Grafana bundle
'''


# https://stackoverflow.com/questions/27146262/create-variable-key-value-pairs-with-argparse-python


class PerftestCli:

    def __init__(self):
        parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter,
                                         description='Management and execution of performance tests', usage=usage)
        parser.add_argument('command', help='Subcommand to run')

        args = parser.parse_args(sys.argv[1:2])
        if not hasattr(self, args.command):
            print('Unrecognized command', parser.print_help())
            exit(1)

        getattr(self, args.command)()

    def create(self):
        from simulator.perftest import PerftestCreateCli

        PerftestCreateCli(sys.argv[2:])

    def clean(self):
        from simulator.perftest import PerftestCleanCli

        PerftestCleanCli(sys.argv[2:])

    def clone(self):
        from simulator.perftest import PerftestCloneCli

        PerftestCloneCli(sys.argv[2:])

    def run(self):
        from simulator.perftest import PerftestRunCli

        PerftestRunCli(sys.argv[2:])

    def kill_java(self):
        from simulator.perftest import PerftestKillJavaCli

        PerftestKillJavaCli(sys.argv[2:])

    def collect(self):
        from simulator.perftest import PerftestCollectCli

        PerftestCollectCli(sys.argv[2:])

    def report(self):
        from simulator.perftest_report import PerfTestReportCli

        PerfTestReportCli(sys.argv[2:])

    def report_grafana(self):
        from simulator.perftest_report_grafana import PerftestReportGrafanaCli

        PerftestReportGrafanaCli(sys.argv[2:])

    def export_observability(self):
        from simulator.observability_export import ObservabilityExportCli

        ObservabilityExportCli(sys.argv[2:])


if __name__ == '__main__':
    os.path.expanduser('~/your_directory')
    PerftestCli()
