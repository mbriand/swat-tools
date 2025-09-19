#!/usr/bin/env python3

"""A tool allowing to import missing buildbot builds into swatbot."""

import json
import logging
from typing import TextIO

import click
import requests
from tqdm.contrib.logging import tqdm_logging_redirect

from swattool import buildbotrest
from swattool.main import shared_main
from swattool import utils
from . import buildbot_operations
from . import swatbot_operations

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
    """Handle triage of Yocto autobuilder failures.

    Main entry point for the application. Sets up logging and handles login if
    needed.
    """
    shared_main(maingroup)


@maingroup.command()
@click.argument('buildbot_url')
@click.argument('buildid_min', type=click.INT)
@click.argument('buildid_max', type=click.INT)
@click.option('--output', '-o', type=click.File(mode='w'),
              default="missing_builds_list.json",
              help="Job list ouput file")
def find(buildbot_url, buildid_min, buildid_max, output: TextIO):
    """Fetch builds from a buildbot instance and find missing swatbot entries.

    Args:
        buildbot_url: URL of the buildbot instance
        buildid_min: Minimum build ID to check
        buildid_max: Maximum build ID to check
        output: Output file for the results
    """
    # Basic input validation
    if buildid_min < 1 or buildid_max < 1:
        raise click.BadParameter("Build IDs must be positive")
    if buildid_min > buildid_max:
        raise click.BadParameter("Min build ID cannot be greater than max")
    if not buildbot_url.strip():
        raise click.BadParameter("Buildbot URL cannot be empty")

    base_url = buildbotrest.autobuilder_base_url(buildbot_url)
    rest_url = buildbotrest.rest_api_url(base_url)

    create_builds = []
    update_builds = []

    def process_buildid(buildid):
        try:
            status = buildbot_operations.check_build_is_missing(base_url,
                                                                rest_url,
                                                                buildid)
            if status == buildbot_operations.BuildStatus.MISSING:
                create_builds.append(buildid)
            elif status == buildbot_operations.BuildStatus.NEEDS_UPDATE:
                update_builds.append(buildid)
        except requests.exceptions.HTTPError:
            logger.warning("Build %s not found on buildbot server",
                           buildid)
        except (requests.exceptions.RequestException,
                json.JSONDecodeError, KeyError, ValueError) as err:
            logger.exception("Failed to analyze build %s: %s", buildid,
                             err)

    executor = utils.ExecutorWithProgress(4)
    for buildid in range(buildid_min, buildid_max + 1):
        executor.submit(f"Fetching build {buildid}", process_buildid, buildid)
    executor.run()

    data = {
        'buildbot_url': base_url,
        'buildbot_rest_url': rest_url,
        'create_builds': create_builds,
        'update_builds': update_builds,
    }
    json.dump(data, output)


@maingroup.command()
@click.option('--dry-run', '-n', is_flag=True,
              help="Only shows what would be done")
@click.option('--input', '-i', 'input_file', type=click.File(mode='r'),
              default="missing_builds_list.json",
              help="Job list input file")
def fix(dry_run: bool, input_file: TextIO):
    """Publish missing entries on swatbot.

    Args:
        dry_run: Only show what would be done without making changes if True
        input_file: Input file containing the list of builds to process
    """
    input_data = json.load(input_file)
    rest_url = input_data['buildbot_rest_url']
    buildbot_url = input_data['buildbot_url']

    bar_format = "{l_bar}{bar}| [{elapsed}<{remaining}, {postfix}]"
    all_builds = input_data['update_builds'] + input_data['create_builds']
    with tqdm_logging_redirect(all_builds, bar_format=bar_format) as progress:
        for buildid in progress:
            try:
                if buildid in input_data['create_builds']:
                    if not swatbot_operations.add_build(rest_url,
                                                        buildbot_url,
                                                        buildid, dry_run):
                        continue
                swatbot_operations.update_build(rest_url, buildbot_url,
                                                buildid, dry_run)
            except KeyboardInterrupt:
                break
            except (requests.exceptions.RequestException,
                    json.JSONDecodeError, KeyError, ValueError) as err:
                logger.exception("Failed to handle build %s: %s", buildid, err)
