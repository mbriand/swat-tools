#!/usr/bin/env python3


"""A tool helping triage of Yocto autobuilder failures."""

import concurrent.futures
import logging
import re
import sys
import textwrap
from typing import Any, Collection

import click
import tabulate

from .bugzilla import Bugzilla
from . import review
from . import swatbot
from . import swatbotrest
from . import swatbuild
from . import triagehistory
from . import userdata
from . import utils
from .webrequests import Session

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
    statuses = [swatbuild.Status[s.upper()] for s in kwargs['status_filter']]
    tests = [re.compile(f"^{f}$") for f in kwargs['test_filter']]
    ignoretests = [re.compile(f"^{f}$") for f in kwargs['ignore_test_filter']]
    owners = [None if str(f).lower() == "none" else f
              for f in kwargs['owner_filter']]
    triages = [swatbotrest.TriageStatus.from_str(s)
               for s in kwargs.get('triage_filter', [])]

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
               'triage': triages,
               }
    return filters


def parse_urlopens(kwargs) -> set[str]:
    """Parse url open arguments."""
    opens = set()
    for urltype in ['autobuilder', 'swatbot', 'stdio']:
        if kwargs.get(f'open_{urltype}_url'):
            opens.add(urltype)

    return opens


@click.group()
@click.option('-v', '--verbose', count=True, help="Increase verbosity")
def maingroup(verbose: int):
    """Handle triage of Yocto autobuilder failures."""
    utils.setup_logging(verbose)


def main():
    """Handle triage of Yocto autobuilder failures."""
    try:
        maingroup()  # pylint: disable=no-value-for-parameter
    except utils.LoginRequiredException as e:
        if e.service == "swatbot":
            logger.warning("Login required to swatbot server")
            user = click.prompt('swatbot user')
            password = click.prompt('swatbot password', hide_input=True)
            success = swatbotrest.login(user, password)
            if success:
                maingroup()  # pylint: disable=no-value-for-parameter
        else:
            raise


@maingroup.command()
@click.option('--user', '-u', prompt=True)
@click.option('--password', '-p', prompt=True, hide_input=True)
def login(user: str, password: str):
    """Login to the swatbot Django interface."""
    swatbotrest.login(user, password)


failures_list_options = [
    click.option('--limit', '-l', type=click.INT, default=None,
                 help="Only parse the n last failures"),
    click.option('--sort', '-s', multiple=True, default=["Build"],
                 type=click.Choice([str(f) for f in swatbuild.Field],
                                   case_sensitive=False),
                 help="Specify sort order"),
    click.option('--refresh', '-r',
                 type=click.Choice([p.name for p in swatbotrest.RefreshPolicy],
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
                 type=click.Choice([str(s) for s in swatbuild.Status],
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

url_open_options = [
    click.option('--open-autobuilder-url', '-u', is_flag=True,
                 help="Open the autobuilder url in web browser"),
    click.option('--open-swatbot-url', '-w', is_flag=True,
                 help="Open the swatbot url in web browser"),
    click.option('--open-stdio-url', '-g', is_flag=True,
                 help="Open the first stdio url in web browser"),
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
        if field == swatbuild.Field.TRIAGE:
            return "\n".join([str(f.get_triage_with_notes())
                              for f in build.failures.values()])
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


def _show_failures(refresh: str, urlopens: set[str], limit: int,
                   sort: Collection[str], filters: dict[str, Any]):
    """Show all failures waiting for triage."""
    swatbotrest.RefreshManager().set_policy_by_name(refresh)

    builds, userinfos = swatbot.get_failure_infos(limit=limit, sort=sort,
                                                  filters=filters)

    for build in builds:
        build.open_urls(urlopens)

    has_user_status = any(userinfos[build.id].triages for build in builds)
    has_notes = any(userinfos[build.id].notes for build in builds)

    shown_fields_all = [
        swatbuild.Field.BUILD,
        swatbuild.Field.STATUS if len(filters['status']) != 1 else None,
        swatbuild.Field.TEST if len({build.test
                                     for build in builds}) != 1 else None,
        swatbuild.Field.OWNER if len(filters['owner']) != 1 else None,
        swatbuild.Field.WORKER,
        swatbuild.Field.COMPLETED,
        swatbuild.Field.SWAT_URL,
        swatbuild.Field.FAILURES,
        swatbuild.Field.TRIAGE if len(filters['triage']) != 1 else None,
        swatbuild.Field.USER_STATUS if has_user_status else None,
        swatbuild.Field.USER_NOTES if has_notes else None,
    ]
    shown_fields = [f for f in shown_fields_all if f]

    table, headers = _format_pending_failures(builds, userinfos, shown_fields)
    print(tabulate.tabulate(table, headers=headers))

    logging.info("%s entries found (%s warnings, %s errors and %s cancelled)",
                 len(builds),
                 len([b for b in builds
                      if b.status == swatbuild.Status.WARNING]),
                 len([b for b in builds
                      if b.status == swatbuild.Status.ERROR]),
                 len([b for b in builds
                      if b.status == swatbuild.Status.CANCELLED]))


@maingroup.command()
@_add_options(failures_list_options)
@_add_options(url_open_options)
@click.option('--triage-filter', multiple=True,
              type=click.Choice([str(s) for s in swatbotrest.TriageStatus],
                                case_sensitive=False),
              help="Only show some triage statuses")
def show_failures(refresh: str, limit: int, sort: list[str],
                  **kwargs):
    """Show all failures, including the old ones."""
    urlopens = parse_urlopens(kwargs)
    filters = parse_filters(kwargs)
    _show_failures(refresh, urlopens, limit, sort, filters)


@maingroup.command()
@_add_options(failures_list_options)
@_add_options(url_open_options)
def show_pending_failures(refresh: str, limit: int, sort: list[str],
                          **kwargs):
    """Show all failures waiting for triage."""
    urlopens = parse_urlopens(kwargs)
    filters = parse_filters(kwargs)
    filters['triage'] = [swatbotrest.TriageStatus.PENDING]
    _show_failures(refresh, urlopens, limit, sort, filters)


def _show_history_info(ctx: click.Context,
                       history: triagehistory.TriageHistory):
    logging.info("Found %s entries in triage history", len(history))
    if len(history) < 100:
        cmd = ctx.parent.command_path if ctx.parent else sys.argv[0]
        histcmd = f'{cmd} generate-triage-history ' \
            '-A $(date -I -d "last month")'
        logging.warning("Very few entries found in triage history\n"
                        'You can create a triage history file with "%s"',
                        histcmd)


@maingroup.command()
@_add_options(failures_list_options)
@_add_options(url_open_options)
@click.option('--triage-filter', multiple=True,
              type=click.Choice([str(s) for s in swatbotrest.TriageStatus],
                                case_sensitive=False),
              help="Only show some triage statuses")
@click.pass_context
def review_pending_failures(ctx: click.Context, refresh: str,
                            limit: int, sort: list[str],
                            **kwargs):
    """Review failures waiting for triage."""
    # pylint: disable=too-many-arguments,too-many-positional-arguments

    swatbotrest.RefreshManager().set_policy_by_name(refresh)

    urlopens = parse_urlopens(kwargs)
    filters = parse_filters(kwargs)
    # filters['triage'] = [swatbotrest.TriageStatus.PENDING]
    builds, userinfos = swatbot.get_failure_infos(limit=limit, sort=sort,
                                                  filters=filters)

    if not builds:
        return

    history = triagehistory.TriageHistory()
    history.load()
    _show_history_info(ctx, history)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        logger.info("Starting log download and "
                    "triage suggestions computation...")
        firstbuild_fut = None
        for build in builds:
            fut = executor.submit(history.compute_similar_triages, build,
                                  timeout_s=10)
            if not firstbuild_fut:
                firstbuild_fut = fut

        # Make sure abints are up-to-date.
        Bugzilla.get_abints()

        # Wait until the triage suggestion for first build is computed, as it
        # will be shown when starting review
        logger.info("Waiting until first entry is ready for review...")
        if firstbuild_fut:
            concurrent.futures.wait([firstbuild_fut])

        review.review_failures(builds, userinfos, history, urlopens)
        executor.shutdown(cancel_futures=True)

    userinfos.save()


@maingroup.command()
@click.option('--dry-run', '-n', is_flag=True,
              help="Only shows what would be done")
def publish_new_reviews(dry_run: bool):
    """Publish new local triage status to swatbot Django interface."""
    reviews = review.get_new_reviews()

    logger.info("Publishing new reviews...")
    for (status, comment), triages in reviews.items():
        bugurl = None

        # Bug entry: need to also publish a new comment on bugzilla.
        if status == swatbotrest.TriageStatus.BUG:
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
                    swatbotrest.publish_status(failureid, status, comment)

    if not dry_run:
        swatbotrest.invalidate_stepfailures_cache()


@maingroup.command()
@_add_options(failures_list_options)
@click.option('--triage-filter', multiple=True,
              type=click.Choice([str(s) for s in swatbotrest.TriageStatus],
                                case_sensitive=False),
              help="Only show some triage statuses")
def generate_triage_history(refresh: str, limit: int, sort: list[str],
                            **kwargs):
    """Generate triage history from old triages."""
    filters = parse_filters(kwargs)
    filters['triage'] = [s for s in swatbotrest.TriageStatus
                         if s != swatbotrest.TriageStatus.PENDING]

    swatbotrest.RefreshManager().set_policy_by_name(refresh)

    builds, _ = swatbot.get_failure_infos(limit=limit, sort=sort,
                                          filters=filters)

    logger.info("Downloading logs...")
    with click.progressbar(builds) as builds_progress:
        for build in builds_progress:
            logurl = build.get_first_failure().get_log_raw_url()
            if logurl:
                Session().get(logurl)

    logger.info("Generating triage history...")
    history = triagehistory.TriageHistory()
    with click.progressbar(builds) as builds_progress:
        for build in builds_progress:
            history.add_build(build)

    history.save()
