#!/usr/bin/env python3


"""A tool allowing to import missing buildbot builds into swatbot."""

import datetime
import json
import logging
from typing import Any, Optional, TextIO

from buildbot.process import results  # type: ignore
import click
import requests
from tqdm.contrib.logging import tqdm_logging_redirect

from swattool import buildbotrest
from swattool.main import shared_main
from swattool import swatbotrest
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
    """Handle triage of Yocto autobuilder failures.

    Main entry point for the application. Sets up logging and handles login if
    needed.
    """
    shared_main(maingroup)


def _get_build_collection(rest_url: str, build: dict) -> int:
    buildrequestid = build['buildrequestid']
    buildreq_url = f"{rest_url}/buildrequests/{buildrequestid}"
    buildreq_json = Session().get(buildreq_url, True, -1)
    buildreq_data = json.loads(buildreq_json)
    buildreq = buildreq_data['buildrequests'][0]

    buildsetid = buildreq['buildsetid']
    buildset_url = f"{rest_url}/buildsets/{buildsetid}"
    buildset_json = Session().get(buildset_url, True, -1)
    buildset_data = json.loads(buildset_json)
    buildset = buildset_data['buildsets'][0]

    collection_build_id = buildset['parent_buildid']
    if not collection_build_id:
        collection_build_id = build['buildid']

    return collection_build_id


def _get_build_branch(build: dict) -> Optional[str]:
    repos = ['poky', 'oecore', 'yocto-docs']
    for repo in repos:
        branch = build['properties'].get(f'branch_{repo}')
        if branch:
            return branch[0]
    branches = [p for p in build['properties'] if p.startswith('branch')]
    logging.warning("Failed to get corresponding branch, possible values: %s",
                    branches)
    return None


def _get_or_add_collection_id(rest_url: str, build: dict, dry_run: bool
                              ) -> Optional[int]:
    collection_build_id = _get_build_collection(rest_url, build)

    if collection_build_id != build['buildid']:
        build_url = \
            f"{rest_url}/builds/{collection_build_id}?property=*"
        build_json = Session().get(build_url, True, 0)
        build_data = json.loads(build_json)
        collection_build = build_data['builds'][0]
    else:
        collection_build = build

    sb_collections_url = f"/buildcollection/?buildid={collection_build_id}"
    sb_collections = swatbotrest.get_json(sb_collections_url, 0)['data']
    if len(sb_collections) > 1:
        logger.error("Unexpected number of collections for buildid %s: %s",
                     collection_build_id, len(sb_collections))
        return None
    if len(sb_collections) == 1:
        return sb_collections[0]['id']

    logger.info("Adding build collection %s", collection_build_id)
    col_props = collection_build['properties']
    branch = _get_build_branch(collection_build)
    if not branch:
        logger.warning("Failed to get branch of build %s: "
                       "cannot create build collection", collection_build_id)
        return None

    payload: dict[str, dict[str, Any]] = {
        'data': {
            'type': 'BuildCollection',
            'attributes': {
                "buildid": collection_build_id,
                "targetname": col_props['buildername'][0],
                "branch": branch,
            }
        }
    }

    if 'reason' in col_props and col_props['reason'][0]:
        payload['data']['attributes']['reason'] = col_props['reason'][0]
    if 'owner' in col_props and col_props['owner'][0]:
        payload['data']['attributes']['owner'] = col_props['owner'][0]
    if 'swat_monitor' in col_props and col_props['swat_monitor'][0]:
        payload['data']['attributes']['forswat'] = True
    else:
        payload['data']['attributes']['forswat'] = False

    if dry_run:
        logger.info("Would have sent to %s: %s", "rest/buildcollection/",
                    payload)
        data = {'id': 123456}
    else:
        data = swatbotrest.post_json("/buildcollection/", payload)['data']
    return data['id']


def _add_build(rest_url: str, buildbot_url: str, buildid: int, dry_run: bool
               ) -> bool:
    build_url = f"{rest_url}/builds/{buildid}?property=*"
    build_json = Session().get(build_url, True, 0)
    build_data = json.loads(build_json)
    build = build_data['builds'][0]

    sb_builds = swatbotrest.get_json(f"/build/?buildid={buildid}", 0)['data']
    if len(sb_builds) >= 1:
        logger.warning("Build %s found on swatbot, we will not add it",
                       buildid)
        return False

    collection_id = _get_or_add_collection_id(rest_url, build, dry_run)
    if not collection_id:
        logger.warning("Failed to get collection for build %s: skipping",
                       buildid)
        return False

    logger.info("Adding build %s", buildid)
    builderid = build['builderid']
    number = build['number']
    build_url = f"{buildbot_url}/#/builders/{builderid}/builds/{number}"
    started_at = datetime.datetime.fromtimestamp(build['started_at'],
                                                 datetime.timezone.utc)

    if 'buildername' not in build['properties']:
        logging.warning("Builder name not set: was the build cancelled?")
        return False

    payload = {
        'data': {
            'type': 'Build',
            'attributes': {
                "buildid": build['buildid'],
                "url": build_url,
                "targetname": build['properties']['buildername'][0],
                "started": started_at.isoformat(),
                "workername": build['properties']['workername'][0],
                "buildcollection": {
                    "type": "BuildCollection",
                    "id": collection_id
                }
            }
        }
    }

    if dry_run:
        logger.info("Would have sent to %s: %s", "rest/build/", payload)
        return False

    swatbotrest.post_json("/build/", payload)
    return True


def _get_step_urls(rest_url: str, buildbot_url: str, build: dict, step: dict
                   ) -> list[str]:
    buildid = build['buildid']

    logs_url = f"{rest_url}/builds/{buildid}/steps/{step['number']}/logs"
    logs_json = Session().get(logs_url, True, 0)
    logs_data = json.loads(logs_json)
    logs = logs_data['logs']

    builderid = build['builderid']
    number = build['number']
    build_url = f"{buildbot_url}#/builders/{builderid}/builds/{number}"
    prefix = f"{build_url}/steps/{step['number']}/logs"
    return [f"{prefix}/{log['name'].replace(' ', '_')}" for log in logs]


def _add_build_steps(rest_url: str, buildbot_url: str, build: dict,
                     sb_buildid: int, dry_run: bool) -> None:
    buildid = build['buildid']

    steps_url = f"{rest_url}/builds/{buildid}/steps"
    steps_json = Session().get(steps_url, True, 0)
    steps_data = json.loads(steps_json)
    for step in steps_data['steps']:
        # Ignore logs for steps which succeeded/cancelled
        result = step['results']
        if result in (results.SUCCESS, results.RETRY, results.CANCELLED,
                      results.SKIPPED):
            continue

        # Log for FAILURE, EXCEPTION, WARNING
        urls = _get_step_urls(rest_url, buildbot_url, build, step)
        if not urls:
            logger.warning("Skipping step %s of build %s as there is no log",
                           step['number'], buildid)
            continue

        payload = {
            'data': {
                'type': 'StepFailure',
                'attributes': {
                    "urls": " ".join(urls),
                    "status": step['results'],
                    "stepname": step['name'],
                    "stepnumber": step['number'],
                    "build": {
                        "type": "Build",
                        "id": sb_buildid,
                    }
                }
            }
        }

        if dry_run:
            logger.info("Would have sent to %s: %s", "rest/stepfailure/",
                        payload)
        else:
            swatbotrest.post_json("/stepfailure/", payload)


def _update_build(rest_url: str, buildbot_url: str, buildid: int, dry_run: bool
                  ) -> None:
    build_url = f"{rest_url}/builds/{buildid}?property=swat_monitor"
    build_json = Session().get(build_url, True, 0)
    build_data = json.loads(build_json)
    build = build_data['builds'][0]

    sb_builds = swatbotrest.get_json(f"/build/?buildid={buildid}", 0)['data']
    if len(sb_builds) != 1:
        logger.warning("Unexpected number of entries found on swatbot "
                       "for build %s: %s", buildid, len(sb_builds))
        return

    logger.info("Updating build %s", buildid)
    sb_buildid = sb_builds[0]['id']

    payload = {
        'data': {
            'id': sb_buildid,
            'type': 'Build',
            'attributes': sb_builds[0]['attributes'],
        }
    }

    payload['data']['attributes']['status'] = build['results']
    complete_at = datetime.datetime.fromtimestamp(build['complete_at'],
                                                  datetime.timezone.utc)
    payload['data']['attributes']['completed'] = complete_at.isoformat()
    if "yp_build_revision" in build['properties']:
        revision = build['properties']['yp_build_revision'][0]
        payload['data']['attributes']['revision'] = revision

    if dry_run:
        logger.info("Would have sent to %s: %s", "rest/build/", payload)
    else:
        swatbotrest.put_json(f"/build/{sb_buildid}/", payload)

    _add_build_steps(rest_url, buildbot_url, build, sb_buildid, dry_run)


def _check_build_is_missing(rest_url: str, buildid: int) -> tuple[bool, bool]:
    cache_max_age = 60 * 60

    build_url = f"{rest_url}/builds/{buildid}?property=swat_monitor"
    build_json = Session().get(build_url, True, cache_max_age)
    build_data = json.loads(build_json)
    build = build_data['builds'][0]
    if not build['complete_at']:
        logger.warning("Ignoring build %s as no end time was set", buildid)
        return False, False
    build_time = datetime.datetime.fromtimestamp(build['complete_at'],
                                                 datetime.timezone.utc)

    sb_builds = swatbotrest.get_json(f"/build/?buildid={buildid}",
                                     cache_max_age)['data']
    if len(sb_builds) >= 1:
        if len(sb_builds) != 1:
            logger.warning("Unexpected number of entries found on swatbot "
                           "for build %s: %s", buildid, len(sb_builds))
            return False, False

        sb_complete = sb_builds[0]['attributes']['completed']
        sb_build_time = None
        if sb_complete:
            sb_build_time = datetime.datetime.fromisoformat(sb_complete)

        if sb_build_time == build_time:
            logger.debug("Build %s found on swatbot", buildid)
            return False, False
        logger.info("Build %s found on swatbot but with %s complete time "
                    "instead of %s", buildid, sb_build_time, build_time)
        return False, True

    logger.info("Build %s has to be sent to swatbot", buildid)
    return True, False


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
        dry_run: Only show what would be done without making changes if True
    """
    base_url = buildbotrest.autobuilder_base_url(buildbot_url)
    rest_url = buildbotrest.rest_api_url(base_url)

    create_builds = []
    update_builds = []

    # This operation is slow, about 10000 build check per hour when I was
    # testing it. We could probably parallelize server requests to speed-up
    # everything, but:
    # - I don't want to flood buildbot and swatbot servers with my requests.
    # - Having to scan the whole buildid range should be rare, so there is
    #   probably no need to complexify the code for this rare use case.
    bar_format = "{l_bar}{bar}| [{elapsed}<{remaining}, {postfix}]"
    with tqdm_logging_redirect(range(buildid_min, buildid_max + 1),
                               bar_format=bar_format) as progress:
        for buildid in progress:
            progress.set_postfix_str(str(buildid))
            try:
                missing, need_update = _check_build_is_missing(rest_url,
                                                               buildid)
                if missing:
                    create_builds.append(buildid)
                elif need_update:
                    update_builds.append(buildid)
            except KeyboardInterrupt:
                break
            except requests.exceptions.HTTPError:
                logger.warning("Build %s not found on buildbot server",
                               buildid)
            except Exception:  # pylint: disable=broad-exception-caught
                logger.exception("Faild to analyze build %s", buildid)

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
                    if not _add_build(rest_url, buildbot_url, buildid,
                                      dry_run):
                        continue
                _update_build(rest_url, buildbot_url, buildid, dry_run)
            except KeyboardInterrupt:
                break
            except Exception:  # pylint: disable=broad-exception-caught
                logger.exception("Faild to handle build %s", buildid)
