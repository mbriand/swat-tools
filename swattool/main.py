#!/usr/bin/env python3


"""A tool helping triage of Yocto autobuilder failures."""

import logging
import re
import textwrap
from typing import Any

import click
import tabulate

from .bugzilla import Bugzilla
from . import review
from . import swatbot
from . import swatbuild
from . import userdata
from . import utils
from .webrequests import RefreshPolicy, Session

logger = logging.getLogger(__name__)


def _add_options(options):
    def _add_options(func):
        for option in reversed(options):
            func = option(func)
        return func
    return _add_options


def parse_filters(kwargs) -> dict[str, Any]:
    """Parse filter arguments.

    Parse filter values givean as program argument and generate a dictionary to
    be used with get_failure_infos().
    """
    statuses = [swatbot.Status[s.upper()] for s in kwargs['status_filter']]
    tests = [re.compile(f"^{f}$") for f in kwargs['test_filter']]
    ignoretests = [re.compile(f"^{f}$") for f in kwargs['ignore_test_filter']]
    owners = [None if str(f).lower() == "none" else f
              for f in kwargs['owner_filter']]

    completed_after = completed_before = None
    if kwargs['completed_after']:
        completed_after = kwargs['completed_after'].astimezone()
    if kwargs['completed_before']:
        completed_before = kwargs['completed_before'].astimezone()

    filters = {'build': kwargs['build_filter'],
               'test': tests,
               'ignore-test': ignoretests,
               'status': statuses,
               'owner': owners,
               'completed-after': completed_after,
               'completed-before': completed_before,
               'with-notes': kwargs['with_notes'],
               'with-new-status': kwargs['with_new_status'],
               }
    return filters


@click.group()
@click.option('-v', '--verbose', count=True, help="Increase verbosity")
def main(verbose: int):
    """Handle triage of Yocto autobuilder failures."""
    utils.setup_logging(verbose)


@main.command()
@click.option('--user', '-u', prompt=True)
@click.option('--password', '-p', prompt=True, hide_input=True)
def login(user: str, password: str):
    """Login to the swatbot Django interface."""
    swatbot.login(user, password)


failures_list_options = [
    click.option('--limit', '-l', type=click.INT, default=None,
                 help="Only parse the n last failures waiting for triage"),
    click.option('--sort', '-s', multiple=True, default=["Build"],
                 type=click.Choice([str(f) for f in swatbuild.Field],
                                   case_sensitive=False),
                 help="Specify sort order"),
    click.option('--refresh', '-r',
                 type=click.Choice([p.name for p in RefreshPolicy],
                                   case_sensitive=False),
                 default="auto",
                 help="Fetch data from server instead of using cache"),
    click.option('--test-filter', '-t', multiple=True,
                 help="Only show some tests"),
    click.option('--build-filter', '-b', type=click.INT, multiple=True,
                 help="Only show some builds"),
    click.option('--owner-filter', '-o', multiple=True,
                 help='Only show some owners ("none" for no owner)'),
    click.option('--ignore-test-filter', '-T', multiple=True,
                 help="Ignore some tests"),
    click.option('--status-filter', '-S', multiple=True,
                 type=click.Choice([str(s) for s in swatbot.Status],
                                   case_sensitive=False),
                 help="Only show some statuses"),
    click.option('--completed-after', '-A',
                 type=click.DateTime(),
                 help="Only show failures after a given date"),
    click.option('--completed-before', '-B',
                 type=click.DateTime(),
                 help="Only show failures before a given date"),
    click.option('--with-notes', '-N', type=click.BOOL, default=None,
                 help="Only show failures with or without attached note"),
    click.option('--with-new-status', type=click.BOOL, default=None,
                 help="Only show failures with or without new (local) status"),
]


def _format_pending_failures(builds: list[swatbuild.Build],
                             userinfos: userdata.UserInfos,
                             shown_fields: list[swatbuild.Field]
                             ) -> tuple[list[list[str]], list[str]]:
    # Generate a list of formatted builds on failures.
    def format_header(field):
        if field == swatbuild.Field.STATUS:
            return "Sts"
        return str(field)

    def format_field(build, userinfo, field):
        if field == swatbuild.Field.STATUS:
            return build.get(swatbuild.Field.STATUS).as_short_colored_str()
        if field == swatbuild.Field.FAILURES:
            return "\n".join([f.stepname for f in build.get(field).values()])
        if field == swatbuild.Field.USER_STATUS:
            statuses = [str(triage) for fail in build.failures.values()
                        if (triage := userinfo.get_failure_triage(fail.id))]
            return "\n".join(statuses)
        if field == swatbuild.Field.USER_NOTES:
            notes = userinfo.get_notes()
            return textwrap.shorten(notes, 80)
        return str(build.get(field))

    headers = [format_header(f) for f in shown_fields]
    table = [[format_field(build, userinfos.get(build.id, {}), field)
              for field in shown_fields] for build in builds]

    return (table, headers)


@main.command()
@_add_options(failures_list_options)
@click.option('--open-url', '-u', is_flag=True,
              help="Open the autobuilder url in web browser")
def show_pending_failures(refresh: str, open_url: str,
                          limit: int, sort: list[str],
                          **kwargs):
    """Show all failures waiting for triage."""
    Session().set_refresh_policy(RefreshPolicy[refresh.upper()])

    filters = parse_filters(kwargs)
    builds, userinfos = swatbot.get_failure_infos(limit=limit, sort=sort,
                                                  filters=filters)

    if open_url:
        for build in builds:
            click.launch(build.autobuilder_url)

    has_user_status = any(userinfos[build.id].triages for build in builds)
    has_notes = any(userinfos[build.id].notes for build in builds)

    shown_fields_all = [
        swatbuild.Field.BUILD,
        swatbuild.Field.STATUS if len(kwargs['status_filter']) != 1 else None,
        swatbuild.Field.TEST if len({build.test
                                     for build in builds}) != 1 else None,
        swatbuild.Field.OWNER if len(kwargs['owner_filter']) != 1 else None,
        swatbuild.Field.WORKER,
        swatbuild.Field.COMPLETED,
        swatbuild.Field.SWAT_URL,
        swatbuild.Field.FAILURES,
        swatbuild.Field.USER_STATUS if has_user_status else None,
        swatbuild.Field.USER_NOTES if has_notes else None,
    ]
    shown_fields = [f for f in shown_fields_all if f]

    table, headers = _format_pending_failures(builds, userinfos, shown_fields)
    print(tabulate.tabulate(table, headers=headers))

    logging.info("%s entries found (%s warnings, %s errors and %s cancelled)",
                 len(builds),
                 len([b for b in builds
                      if b.status == swatbot.Status.WARNING]),
                 len([b for b in builds
                      if b.status == swatbot.Status.ERROR]),
                 len([b for b in builds
                      if b.status == swatbot.Status.CANCELLED]))


@main.command()
@_add_options(failures_list_options)
@click.option('--open-autobuilder-url', '-u', is_flag=True,
              help="Open the autobuilder url in web browser")
@click.option('--open-swatbot-url', '-w', is_flag=True,
              help="Open the swatbot url in web browser")
@click.option('--open-stdio-url', '-g', is_flag=True,
              help="Open the first stdio url in web browser")
def review_pending_failures(refresh: str, open_autobuilder_url: bool,
                            open_swatbot_url: bool, open_stdio_url: bool,
                            limit: int, sort: list[str],
                            **kwargs):
    """Review failures waiting for triage."""

    # pylint: disable=too-many-arguments,too-many-positional-arguments

    Session().set_refresh_policy(RefreshPolicy[refresh.upper()])

    filters = parse_filters(kwargs)
    builds, userinfos = swatbot.get_failure_infos(limit=limit, sort=sort,
                                                  filters=filters)

    if not builds:
        return

    logger.info("Downloading logs...")
    with click.progressbar(builds) as builds_progress:
        for build in builds_progress:
            logurl = build.get_first_failure().get_log_raw_url()
            if logurl:
                Session().get(logurl)

    review.review_failures(builds, userinfos,
                           open_autobuilder_url, open_swatbot_url,
                           open_stdio_url)

    userinfos.save()


@main.command()
@click.option('--dry-run', '-n', is_flag=True,
              help="Only shows what would be done")
def publish_new_reviews(dry_run: bool):
    """Publish new local triage status to swatbot Django interface."""
    reviews = review.get_new_reviews()

    logger.info("Publishing new reviews...")
    for (status, comment), triages in reviews.items():
        bugurl = None

        # Bug entry: need to also publish a new comment on bugzilla.
        if status == swatbot.TriageStatus.BUG:
            bugid = int(comment)
            logs = [triage.extra['bugzilla-comment'] for triage in triages
                    if triage.failures]

            if any(logs):
                comment = bugurl = Bugzilla.get_bug_url(bugid)
                logger.info('Need to update %s with %s', bugurl,
                            ", ".join(logs).replace('\n', ' '))
                if not dry_run:
                    Bugzilla.add_bug_comment(bugid, '\n'.join(logs))

        for triage in triages:
            for failureid in triage.failures:
                logger.info('Need to update failure %s '
                            'to status %s (%s) with "%s"',
                            failureid, status, status.name.title(), comment)
                if not dry_run:
                    swatbot.publish_status(failureid, status, comment)

    if not dry_run:
        swatbot.invalidate_stepfailures_cache()
