#!/usr/bin/env python3

import click
import logging
import tabulate
import subprocess
import shlex
import swatbot
import pathlib
import bugzilla
import webrequests
import review
from typing import Any, Optional
import re

logger = logging.getLogger(__name__)

BINDIR = pathlib.Path(__file__).parent.resolve()
DATADIR = BINDIR / "data"


def add_options(options):
    def _add_options(func):
        for option in reversed(options):
            func = option(func)
        return func
    return _add_options


def parse_filters(kwargs) -> dict[str, Any]:
    statuses = [swatbot.Status[s.upper()] for s in kwargs['status_filter']]
    tests = [re.compile(f"^{f}$") for f in kwargs['test_filter']]
    ignoretests = [re.compile(f"^{f}$") for f in kwargs['ignore_test_filter']]
    owners = [None if str(f).lower() == "none" else f
              for f in kwargs['owner_filter']]

    completed_after = None
    if kwargs['completed_after']:
        completed_after = kwargs['completed_after'].astimezone()

    filters = {'build': kwargs['build_filter'],
               'test': tests,
               'ignore-test': ignoretests,
               'status': statuses,
               'owner': owners,
               'completed-after': completed_after,
               'with-notes': kwargs['with_notes']
               }
    return filters


@click.group()
@click.option('-v', '--verbose', count=True, help="Increase verbosity")
def main(verbose: int):
    if verbose >= 1:
        loglevel = logging.DEBUG
    else:
        loglevel = logging.INFO
    logging.basicConfig(level=loglevel)


@main.command()
@click.option('--user', '-u', prompt=True)
@click.option('--password', '-p', prompt=True, hide_input=True)
def login(user: str, password: str):
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
    click.option('--with-notes', '-N', type=click.BOOL, default=None,
                 help="Only show failures with or without attached note")
]


@main.command()
@add_options(failures_list_options)
@click.option('--open-url-with',
              help="Open the swatbot url with given program")
def show_pending_failures(refresh: str, open_url_with: str,
                          limit: int, sort: list[str],
                          *args, **kwargs):
    refreshpol = webrequests.RefreshPolicy[refresh.upper()]

    filters = parse_filters(kwargs)
    infos, userinfos = swatbot.get_failure_infos(limit=limit, sort=sort,
                                                 refresh=refreshpol,
                                                 filters=filters)

    if open_url_with:
        for info in infos:
            url = info[swatbot.Field.SWAT_URL]
            subprocess.run(shlex.split(f"{open_url_with} {url}"))

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
            maxlen = 40
            if len(notes) > maxlen:
                notes = f"{notes[:maxlen-3]}..."
            return notes
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

    logging.info("%s entries found (%s warnings and %s errors)", len(infos),
                 len([i for i in infos
                      if i[swatbot.Field.STATUS] == swatbot.Status.WARNING]),
                 len([i for i in infos
                      if i[swatbot.Field.STATUS] == swatbot.Status.ERROR]))


@main.command()
@add_options(failures_list_options)
@click.option('--open-url-with',
              help="Open the swatbot url with given program")
@click.option('--include-already-reviewed', '-a', is_flag=True,
              help="Include already reviewed issues")
def review_pending_failures(refresh: str, open_url_with: str,
                            include_already_reviewed: bool,
                            limit: int, sort: list[str],
                            *args, **kwargs):
    refreshpol = webrequests.RefreshPolicy[refresh.upper()]

    filters = parse_filters(kwargs)
    infos, userinfos = swatbot.get_failure_infos(limit=limit, sort=sort,
                                                 refresh=refreshpol,
                                                 filters=filters)

    if not include_already_reviewed:
        infos = [info for info in infos
                 if not userinfos.get(info[swatbot.Field.BUILD],
                                      {}).get(swatbot.Field.USER_STATUS, [])]

    if not infos:
        return

    entry: Optional[int] = 0
    prev_entry = None
    while entry is not None:
        info = infos[entry]
        userinfo = userinfos.get(info[swatbot.Field.BUILD], {})

        simple_fields = [
            swatbot.Field.BUILD,
            swatbot.Field.STATUS,
            swatbot.Field.TEST,
            swatbot.Field.OWNER,
            swatbot.Field.WORKER,
            swatbot.Field.COMPLETED,
            swatbot.Field.SWAT_URL,
            swatbot.Field.AUTOBUILDER_URL,
        ]
        table = [[k, info[k]] for k in simple_fields]

        status_strs = []
        statuses = userinfo.get(swatbot.Field.USER_STATUS, [])
        failures = info[swatbot.Field.FAILURES]
        for failure in failures:
            status_str = ""
            for status in statuses:
                if failure in status['failures']:
                    status_str = f"{status['status'].name.title()}: " \
                        f"{status['comment']}"
                    break
            status_strs.append(status_str)

        table.append([swatbot.Field.FAILURES,
                      "\n".join([f['stepname'] for f in failures.values()]),
                      "\n".join(status_strs)])

        usernotes = userinfo.get(swatbot.Field.USER_NOTES)
        if usernotes:
            table.append([swatbot.Field.USER_NOTES, usernotes])

        print()
        print(f"Progress: {entry+1}/{len(infos)}")
        print(tabulate.tabulate(table))
        print()

        if open_url_with and prev_entry != entry:
            url = info[swatbot.Field.AUTOBUILDER_URL]
            subprocess.run(shlex.split(f"{open_url_with} {url}"))

        prev_entry = entry
        entry = review.review_menu(infos, userinfos, entry)

    swatbot.save_user_infos(userinfos)


@main.command()
@click.option('--dry-run', '-n', is_flag=True,
              help="Only shows what would be done")
def publish_new_reviews(dry_run: bool):
    reviews = review.get_new_reviews()

    logger.info("Publishing new reviews...")
    for (status, comment), entries in reviews.items():
        bugurl = None

        if status == swatbot.TriageStatus.BUG:
            bugid = int(comment)
            logs = [entry['bugzilla-comment'] for entry in entries
                    if entry['failures']]

            if logs:
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
                    swatbot.publish_status(failureid, failuredata, status,
                                           comment)


if __name__ == '__main__':
    main()
