#!/usr/bin/env python3


"""A tool helping triage of Yocto autobuilder failures."""

import click
import logging
import re
import sys
import tabulate
import textwrap
from typing import Any, Optional

from . import bugzilla
from . import review
from . import swatbot
from . import webrequests

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
    if verbose >= 1:
        loglevel = logging.DEBUG
    else:
        loglevel = logging.INFO
    logging.basicConfig(level=loglevel)


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
                 type=click.Choice([str(f) for f in swatbot.Field],
                                   case_sensitive=False),
                 help="Specify sort order"),
    click.option('--refresh', '-r',
                 type=click.Choice([p.name for p in webrequests.RefreshPolicy],
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


@main.command()
@_add_options(failures_list_options)
@click.option('--open-url', '-u', is_flag=True,
              help="Open the autobuilder url in web browser")
def show_pending_failures(refresh: str, open_url: str,
                          limit: int, sort: list[str],
                          *args, **kwargs):
    """Show all failures waiting for triage."""
    refreshpol = webrequests.RefreshPolicy[refresh.upper()]

    filters = parse_filters(kwargs)
    infos, userinfos = swatbot.get_failure_infos(limit=limit, sort=sort,
                                                 refresh=refreshpol,
                                                 filters=filters)

    if open_url:
        for info in infos:
            click.launch(info[swatbot.Field.AUTOBUILDER_URL])

    # Generate a list of formatted infos on failures.
    def format(info, userinfo, field):
        if field == swatbot.Field.FAILURES:
            return "\n".join([f['stepname'] for f in info[field].values()])
        if field == swatbot.Field.USER_STATUS:
            status_strs = []
            statuses = userinfo.get(field, [])
            for failure in info[swatbot.Field.FAILURES]:
                status_str = ""
                for status in statuses:
                    if failure in status['failures']:
                        status_str = f"{status['status'].name.title()}: " \
                            f"{status['comment']}"
                        break
                status_strs.append(status_str)
            return "\n".join(status_strs)
        if field == swatbot.Field.USER_NOTES:
            notes = userinfo.get(field, "").replace("\n", " ")
            return textwrap.shorten(notes, 80)
        return str(info[field])

    shown_fields = [
        swatbot.Field.BUILD,
        swatbot.Field.STATUS if len(kwargs['status_filter']) != 1 else None,
        swatbot.Field.TEST if len({info[swatbot.Field.TEST]
                                   for info in infos}) != 1 else None,
        swatbot.Field.OWNER if len(kwargs['owner_filter']) != 1 else None,
        swatbot.Field.WORKER,
        swatbot.Field.COMPLETED,
        swatbot.Field.SWAT_URL,
        swatbot.Field.FAILURES,
        swatbot.Field.USER_STATUS,
        swatbot.Field.USER_NOTES,
    ]
    shown_fields = [f for f in shown_fields if f]
    headers = [str(f) for f in shown_fields]
    table = [[format(info, userinfos.get(info[swatbot.Field.BUILD], {}), field)
              for field in shown_fields] for info in infos]

    print(tabulate.tabulate(table, headers=headers))

    logging.info("%s entries found (%s warnings, %s errors and %s cancelled)",
                 len(infos),
                 len([i for i in infos
                      if i[swatbot.Field.STATUS] == swatbot.Status.WARNING]),
                 len([i for i in infos
                      if i[swatbot.Field.STATUS] == swatbot.Status.ERROR]),
                 len([i for i in infos
                      if i[swatbot.Field.STATUS] == swatbot.Status.CANCELLED]))


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
                            *args, **kwargs):
    """Review failures waiting for triage."""
    refreshpol = webrequests.RefreshPolicy[refresh.upper()]

    filters = parse_filters(kwargs)
    infos, userinfos = swatbot.get_failure_infos(limit=limit, sort=sort,
                                                 refresh=refreshpol,
                                                 filters=filters)

    if not infos:
        return

    entry: Optional[int] = 0
    prev_entry = None
    kbinter = False
    while entry is not None:
        try:
            info = infos[entry]
            userinfo = userinfos.get(info[swatbot.Field.BUILD], {})

            if prev_entry != entry:
                if open_autobuilder_url:
                    click.launch(info[swatbot.Field.AUTOBUILDER_URL])
                if open_swatbot_url:
                    click.launch(info[swatbot.Field.SWAT_URL])
                if open_stdio_url:
                    failures = info[swatbot.Field.FAILURES]
                    first_failure = min(failures)
                    if 'stdio' in failures[first_failure]['urls']:
                        click.launch(failures[first_failure]['urls']['stdio'])
                    else:
                        logger.warning("Failed to find stdio log")

            if not kbinter:
                click.clear()
                print(swatbot.get_failure_description(info, userinfo))
                print()

            prev_entry = entry
            statusbar = f"Progress: {entry+1}/{len(infos)}"
            entry = review.review_menu(infos, userinfos, entry, statusbar)
        except KeyboardInterrupt:
            if kbinter:
                sys.exit(1)
            else:
                print("^C pressed. Press again to quit without saving")
                print()
                kbinter = True
                continue
        except Exception as e:
            filename = swatbot.save_user_infos(userinfos, suffix="-crash")
            logging.error("Got exception, saving userinfos in a crash file: "
                          "You may want to retrieve data from there (%s)",
                          filename)
            raise e
        kbinter = False

    swatbot.save_user_infos(userinfos)


@main.command()
@click.option('--dry-run', '-n', is_flag=True,
              help="Only shows what would be done")
def publish_new_reviews(dry_run: bool):
    """Publish new triage status modified locally."""
    reviews = review.get_new_reviews()

    logger.info("Publishing new reviews...")
    for (status, comment), entries in reviews.items():
        bugurl = None

        # Bug entry: need to also publish a new comment on bugzilla.
        if status == swatbot.TriageStatus.BUG:
            bugid = int(comment)
            logs = [entry['bugzilla-comment'] for entry in entries
                    if entry['failures']]

            if any(logs):
                comment = bugurl = bugzilla.get_bug_url(bugid)
                logging.info('Need to update %s with %s', bugurl,
                             ", ".join(logs).replace('\n', ' '))
                if not dry_run:
                    bugzilla.add_bug_comment(bugid, '\n'.join(logs))

        for entry in entries:
            for failureid, failuredata in entry['failures'].items():
                logging.info('Need to update failure %s (%s) '
                             'to status %s (%s) with "%s"',
                             failureid, failuredata['stepname'], status,
                             status.name.title(), comment)
                if not dry_run:
                    swatbot.publish_status(failureid, status, comment)

    if not dry_run:
        swatbot.invalidate_stepfailures_cache()


if __name__ == '__main__':
    main()
