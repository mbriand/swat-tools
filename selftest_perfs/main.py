#!/usr/bin/env python3

"""A tool to analyze selftest performances."""

import csv
from datetime import date, datetime
import json
import logging
import re
from typing import Optional
import urllib

import click

from swattool import buildbotrest
from swattool.main import shared_main
from swattool import utils
from swattool.webrequests import Session

logger = logging.getLogger(__name__)


@click.group()
@click.option("-v", "--verbose", count=True, help="Increase verbosity")
def maingroup(verbose: int):
    """Handle triage of Yocto autobuilder failures.

    Args:
        verbose: Verbosity level for logging
    """
    utils.setup_logging(verbose)
    utils.setup_readline()


def main():
    """Analyze selftest performances.

    Main entry point for the application. Sets up logging and handles login if
    needed.
    """
    shared_main(maingroup)


def extract_times_from_log(log_data: str) -> dict[str, float]:
    """Extract test timing information from selftest log data.

    Parses the log data from a selftest run to extract the execution time
    for each individual test.

    Args:
        log_data: Raw log content from a selftest run

    Returns:
        Dictionary mapping test names to their execution times in seconds
    """
    timings = {}
    for line in log_data.splitlines():
        fields = line.split(" - ")
        if len(fields) < 4:
            continue
        if fields[1] != "oe-selftest" or fields[3] != "RESULTS":
            continue

        subfields = fields[4].split()
        if subfields[1] != "PASSED":
            continue

        testname = subfields[0].strip(":")
        testtime = subfields[2].strip("()s")

        timings[testname] = float(testtime)

    return timings


def get_build_log(rest_url: str, build_id: int) -> Optional[str]:
    """Retrieve the selftest log for a specific build.

    Args:
        rest_url: The REST API URL prefix for buildbot
        build_id: The ID of the build to retrieve logs from

    Returns:
        The raw log content as a string, or None if the log cannot be found
    """
    steps_url = f"{rest_url}/builds/{build_id}/steps"
    steps_json = Session().get(steps_url, True, -1)
    steps_data = json.loads(steps_json)
    selftest_steps = [
        step
        for step in steps_data["steps"]
        if step["name"] == "OE Selftest: Run cmds"
    ]
    if len(selftest_steps) != 1:
        return None
    stepnumber = selftest_steps[0]["number"]
    logmetadata = buildbotrest.get_log_data(
        rest_url, build_id, stepnumber, "stdio"
    )

    if not logmetadata:
        return None

    log_url = f"{rest_url}/logs/{logmetadata['logid']}/raw"
    return Session().get(log_url, True, -1)


def _get_builds(
    rest_url: str, builder_id: int, start_date: datetime, branch_name: str
):
    cache_max_age = 60 * 60

    branches = ["oecore", "poky"]
    params = {"property": [f"branch_{branch}" for branch in branches]}
    fparams = urllib.parse.urlencode(params, doseq=True)
    all_builds_url = f"{rest_url}/builders/{builder_id}/builds?{fparams}"
    builds_json = Session().get(all_builds_url, True, cache_max_age)
    builds_data = json.loads(builds_json)

    def use_build(build):
        if not build.get("complete_at"):
            return False
        if datetime.fromtimestamp(build["complete_at"]) < start_date:
            return False

        return all(
            build["properties"][f"branch_{branch}"][0] == branch_name
            for branch in branches
        )

    builds = [build for build in builds_data["builds"] if use_build(build)]

    return builds


def get_builds_data(
    rest_url: str, builder_id: int, start_date: datetime, branch_name: str
) -> dict[int, tuple[date, dict[str, float]]]:
    """Retrieve and analyze selftest timing data from buildbot builds.

    Fetches build data from a specific builder, filters by date and branch,
    then extracts timing information from selftest logs.

    Args:
        rest_url: The REST API URL prefix for buildbot
        builder_id: The ID of the buildbot builder to analyze
        start_date: Only analyze builds completed after this date
        branch_name: Only analyze builds from this branch

    Returns:
        Dictionary mapping build IDs to tuples of (completion_date,
        timings_dict) where timings_dict maps test names to execution times
    """
    builds = _get_builds(rest_url, builder_id, start_date, branch_name)

    builds_data: dict[int, tuple[date, dict[str, float]]] = {}
    for build in builds:
        log_data = get_build_log(rest_url, build["buildid"])
        if not log_data:
            continue

        complete_date = datetime.fromtimestamp(build["complete_at"]).date()
        timings = extract_times_from_log(log_data)
        builds_data[build["buildid"]] = (complete_date, timings)

    return builds_data


def _get_timings(
    builds_data: dict[int, tuple[date, dict[str, float]]], test: str
):
    return [
        timing
        for _, build_timings in builds_data.values()
        if (timing := build_timings.get(test)) is not None
    ]


def _print_data(
    builds_data: dict[int, tuple[date, dict[str, float]]],
    name: str,
    tests: list[str],
):
    print(f"--- {name} ---")
    for test in tests:
        timings = _get_timings(builds_data, test)
        print(test, min(timings), max(timings), sum(timings) / len(timings))


def _find_long_tests(
    builds_data: dict[int, tuple[date, dict[str, float]]], tests: list[str]
) -> tuple[list[str], list[str]]:
    long_tests = []
    longer_tests = []
    for test in tests:
        timings = _get_timings(builds_data, test)

        if max(timings) > 3600:
            long_tests.append(test)

        old_mean = sum(timings[:5]) / 5
        new_mean = sum(timings[-5:]) / 5
        if new_mean > 5 and new_mean > 3 * old_mean:
            longer_tests.append(test)

    return (long_tests, longer_tests)


def print_export_data(
    builds_data: dict[int, tuple[date, dict[str, float]]],
    csvname: str,
    export_all_stats: bool,
    export_stats: str,
):
    """Print statistics and export results.

    Args:
        builds_data: build performances data
        csvname: CSV output file name
        export_all_stats: If True, export timing data for all tests
        export_stats: Regular expressions for specific tests to export
    """
    tests: set[str] = set()
    for _, build_timings in builds_data.values():
        tests.update(build_timings.keys())

    long_tests, longer_tests = _find_long_tests(builds_data, list(tests))

    _print_data(builds_data, "all tests", list(tests))
    _print_data(builds_data, "long tests", long_tests)
    _print_data(builds_data, "longer tests", longer_tests)

    exported_tests_set = set()
    if export_all_stats:
        exported_tests_set.update(tests)
    for stat in export_stats:
        exported_tests_set.update(
            [test for test in tests if re.match(stat, test)]
        )

    if not exported_tests_set:
        exported_tests_set.update(longer_tests)
    exported_tests = list(exported_tests_set)
    with open(csvname, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.writer(
            csvfile, delimiter=" ", quotechar="|", quoting=csv.QUOTE_MINIMAL
        )
        writer.writerow(["build_date", *exported_tests])
        for build_date, build_timings in builds_data.values():
            writer.writerow(
                [
                    build_date,
                    *[build_timings.get(test) for test in exported_tests],
                ]
            )


@maingroup.command()
@click.argument("buildbot_url")
@click.argument("builder_id", type=click.INT)
@click.argument("start_date", type=click.DateTime())
@click.option(
    "--branch-name", "-b", default="master", help="branch to analyze"
)
@click.option(
    "--export-all-stats", "-a", is_flag=True, help="Export all stats"
)
@click.option(
    "--export-stats", "-s", multiple=True, help="Export stats for a given test"
)
# pylint: disable=too-many-arguments,too-many-positional-arguments
def stats(
    buildbot_url: str,
    builder_id: int,
    start_date: datetime,
    branch_name: str,
    export_all_stats: bool,
    export_stats: str,
):
    """Analyze selftest performance statistics and export results.

    Analyzes selftest timing data from a buildbot builder, identifies tests
    with performance issues, and exports timing data to CSV format.

    Args:
        buildbot_url: The buildbot instance URL to analyze
        builder_id: The ID of the builder to analyze
        start_date: Only analyze builds completed after this date
        branch_name: Only analyze builds from this branch
        export_all_stats: If True, export timing data for all tests
        export_stats: Regular expressions for specific tests to export
    """
    base_url = buildbotrest.autobuilder_base_url(buildbot_url)
    rest_url = buildbotrest.rest_api_url(base_url)

    builds_data = get_builds_data(
        rest_url, builder_id, start_date, branch_name
    )

    filename = f"selftest_timings_{builder_id}_{branch_name}.csv"
    print_export_data(builds_data, filename, export_all_stats, export_stats)
