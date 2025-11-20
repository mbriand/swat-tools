#!/usr/bin/env python3

"""A tool to analyze selftest performances."""

from datetime import datetime
import json
import logging
import re
import urllib

import click
import csv

from swattool import buildbotrest
from swattool.main import shared_main
from swattool import utils
from swattool.webrequests import Session

logger = logging.getLogger(__name__)


@click.group()
@click.option('-v', '--verbose', count=True, help="Increase verbosity")
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
    timings = {}
    for line in log_data.splitlines():
        fields = line.split(' - ')
        if len(fields) < 4:
            continue
        if fields[1] != 'oe-selftest' or fields[3] != 'RESULTS':
            continue

        subfields = fields[4].split()
        if subfields[1] != 'PASSED':
            continue

        testname = subfields[0].strip(':')
        testtime = subfields[2].strip('()s')

        timings[testname] = float(testtime)

    return timings

def get_builds_data(rest_url: str, builder_id: int, start_date: datetime,
                    branch_name: str
                    ) -> dict[int, tuple[str, dict[str, float]]]:
    cache_max_age = 60 * 60

    branches = ['oecore', 'poky']
    params = {'property': [f'branch_{branch}' for branch in branches]}
    fparams = urllib.parse.urlencode(params, doseq=True)
    all_builds_url = f"{rest_url}/builders/{builder_id}/builds?{fparams}"
    builds_json = Session().get(all_builds_url, True, cache_max_age)
    builds_data = json.loads(builds_json)

    def use_build(build):
        if not build.get('complete_at'):
            return False
        if datetime.fromtimestamp(build['complete_at']) < start_date:
            return False

        return all(build['properties'][f'branch_{branch}'][0] == branch_name
                   for branch in branches)

    builds = [build for build in builds_data['builds'] if use_build(build)]

    builds_data = {}
    for build in builds:
        steps_url = f"{rest_url}/builds/{build['buildid']}/steps"
        steps_json = Session().get(steps_url, True, -1)
        steps_data = json.loads(steps_json)
        selftest_steps = [step for step in steps_data["steps"]
                          if step["name"] == "OE Selftest: Run cmds"]
        if len(selftest_steps) != 1:
            continue
        stepnumber = selftest_steps[0]["number"]
        logmetadata = buildbotrest.get_log_data(rest_url, build['buildid'],
                                                stepnumber, 'stdio')

        if not logmetadata:
            continue

        log_url = f"{rest_url}/logs/{logmetadata['logid']}/raw"
        log_data = Session().get(log_url, True, -1)

        date = datetime.fromtimestamp(build['complete_at']).date()
        timings = extract_times_from_log(log_data)
        builds_data[build['buildid']] = (date, timings)

    return builds_data


@maingroup.command()
@click.argument('buildbot_url')
@click.argument('builder_id', type=click.INT)
@click.argument('start_date', type=click.DateTime())
@click.option('--branch-name', '-b', default="master",
              help="branch to analyze")
@click.option('--export-all-stats', '-a', is_flag=True,
              help="Export all stats")
@click.option('--export-stats', '-s', multiple=True,
              help="Export stats for a given test")
# pylint: disable=too-many-arguments,too-many-positional-arguments
def stats(buildbot_url: str, builder_id: int, start_date: datetime,
          branch_name: str, export_all_stats: bool, export_stats: str):
    base_url = buildbotrest.autobuilder_base_url(buildbot_url)
    rest_url = buildbotrest.rest_api_url(base_url)

    builds_data = get_builds_data(rest_url, builder_id, start_date,
                                  branch_name)

    tests: set[str] = set()
    for _, build_timings in builds_data.values():
        tests.update(build_timings.keys())

    long_tests = []
    longer_tests = []
    for test in tests:
        timings = [timing for _, build_timings in builds_data.values()
                   if (timing := build_timings.get(test)) is not None]
        print(test, min(timings), max(timings), sum(timings) / len(timings))

        if max(timings) > 3600:
            long_tests.append(test)

        old_mean = sum(timings[:5]) / 5
        new_mean = sum(timings[-5:]) / 5
        if new_mean > 5 and new_mean > 3 * old_mean:
            longer_tests.append(test)

    print('---')
    for test in long_tests:
        timings = [timing for _, build_timings in builds_data.values()
                   if (timing := build_timings.get(test)) is not None]
        print(test, min(timings), max(timings), sum(timings) / len(timings))
    print('---')
    for test in longer_tests:
        timings = [timing for _, build_timings in builds_data.values()
                   if (timing := build_timings.get(test)) is not None]
        print(test, min(timings), max(timings), sum(timings) / len(timings))

    exported_tests_set = set()
    if export_all_stats:
        exported_tests_set.update(tests)
    for stat in export_stats:
        exported_tests_set.update([test for test in tests
                                   if re.match(stat, test)])

    if not exported_tests_set:
        exported_tests_set.update(longer_tests)
    exported_tests = list(exported_tests_set)
    csvname = f'selftest_timings_{builder_id}_{branch_name}.csv'
    with open(csvname, 'w', newline='', encoding='utf-8') as csvfile:
        writer = csv.writer(csvfile, delimiter=' ', quotechar='|',
                            quoting=csv.QUOTE_MINIMAL)
        writer.writerow(["build_date", *exported_tests])
        for build_date, build_timings in builds_data.values():
            writer.writerow([build_date, *[build_timings.get(test)
                                           for test in exported_tests]])
