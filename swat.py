#!/usr/bin/env python3

import click
import logging
import tabulate
import subprocess
import shlex
import swatbot
import os
import tempfile
import pathlib
import bugzilla
from typing import Any, Optional

logger = logging.getLogger(__name__)

BINDIR = pathlib.Path(__file__).parent.resolve()
DATADIR = BINDIR / "data"

MAILNAME = subprocess.run(["git", "config", "--global", "user.name"],
                          capture_output=True).stdout.decode().strip()


@click.group()
@click.option('-v', '--verbose', count=True, help="Increase verbosity")
def main(verbose: int):
    if verbose >= 1:
        loglevel = logging.DEBUG
    else:
        loglevel = logging.INFO
    logging.basicConfig(level=loglevel)


@main.command()
@click.argument('user')
@click.argument('password')
def login(user: str, password: str):
    swatbot.login(user, password)


failures_list_options = [
    click.option('--limit', '-l', type=click.INT, default=0,
                 help="Only parse the n last failures waiting for triage"),
    click.option('--sort', '-s', multiple=True, default=["Build"],
                 type=click.Choice([str(f) for f in swatbot.Field],
                                   case_sensitive=False),
                 help="Specify sort order"),
    click.option('--refresh', '-r',
                 type=click.Choice([p.name for p in swatbot.RefreshPolicy],
                                   case_sensitive=False),
                 default="auto",
                 help="Fetch data from server instead of using cache"),
    click.option('--test-filter', '-t', multiple=True,
                 help="Only show some tests"),
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
    click.option('--with-notes', '-N', is_flag=True,
                 help="Only show failures with attached note")
]


def add_options(options):
    def _add_options(func):
        for option in reversed(options):
            func = option(func)
        return func
    return _add_options


@main.command()
@add_options(failures_list_options)
@click.option('--open-url-with',
              help="Open the swatbot url with given program")
def show_pending_failures(open_url_with: str, *args, **kwargs):
    infos, userinfos = swatbot.get_failure_infos(*args, **kwargs)

    for info in infos:
        if open_url_with:
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


def edit_text(text: Optional[str]) -> str:
    editor = os.environ.get("EDITOR", "vim")

    with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
        if text:
            f.write(text)
        f.close()
        subprocess.run(shlex.split(f"{editor} {f.name}"))
        with open(f.name, mode='r') as fr:
            newtext = fr.read()
        os.unlink(f.name)

    return newtext


def review_menu(infos: list[dict[swatbot.Field, Any]],
                userinfos: dict[int, dict[swatbot.Field, Any]],
                entry: int) -> Optional[int]:
    print("a ab-int")
    print("b bug opened")
    print("m mail sent")
    if MAILNAME:
        print(f"i mail sent by {MAILNAME}")
    print("o other")
    print("f other: Fixed")
    print("t not for swat")
    print("r reset status")
    print()
    print("n next")
    print("p previous")
    print("e edit notes")
    print("q quit")

    info = infos[entry]
    userinfo = userinfos.setdefault(info[swatbot.Field.BUILD], {})
    failures = info[swatbot.Field.FAILURES]
    first_failure = min(failures)
    newstatus: Optional[dict] = None

    while True:
        line = input('action: ')
        if line.strip() == "n":
            entry += 1
        elif line.strip() == "p":
            if entry >= 1:
                entry -= 1
            else:
                logger.warning("This is the first entry")
                continue
        elif line.strip() == "q":
            return None
        elif line.strip() == "e":
            newnotes = edit_text(userinfo.get(swatbot.Field.USER_NOTES))
            userinfo[swatbot.Field.USER_NOTES] = newnotes
        elif line.strip() in ["a", "b"]:
            abints = bugzilla.get_abints()
            while True:
                bugnum = input('Bug number:').strip()
                if bugnum.isnumeric() and (int(bugnum) in abints
                                           or line.strip() == "b"):
                    print("Please set the comment content")
                    print(failures[first_failure]['urls'])
                    if 'stdio' in failures[first_failure]['urls']:
                        log = failures[first_failure]['urls']['stdio']
                        bcomment = edit_text(log)
                    else:
                        bcomment = edit_text(None)
                    newstatus = {'status': swatbot.TriageStatus.BUG,
                                 'comment': int(bugnum),
                                 'bugzilla-comment': bcomment,
                                 }
                    break
                elif bugnum.strip() == "q":
                    break
                else:
                    logging.warning("Invalid issue: %s", bugnum)
                    if line.strip() == "a":
                        print(tabulate.tabulate(abints.items()))
        elif line.strip() == "m":
            newstatus = {'status': swatbot.TriageStatus.MAIL_SENT,
                         'comment': input('Comment:').strip(),
                         }
        elif line.strip() == "i" and MAILNAME:
            newstatus = {'status': swatbot.TriageStatus.MAIL_SENT,
                         'comment': f"Mail sent by {MAILNAME}",
                         }
        elif line.strip() == "o":
            newstatus = {'status': swatbot.TriageStatus.OTHER,
                         'comment': input('Comment:').strip(),
                         }
        elif line.strip() == "f":
            newstatus = {'status': swatbot.TriageStatus.OTHER,
                         'comment': 'Fixed',
                         }
        elif line.strip() == "n":
            newstatus = {'status': swatbot.TriageStatus.NOT_FOR_SWAT,
                         'comment': input('Comment:').strip(),
                         }
        elif line.strip() == "r":
            userinfo[swatbot.Field.USER_STATUS] = []
        else:
            logger.warning("Invalid command")
            continue
        break

    if newstatus:
        # newstatus['failures'] = {first_failure: failures[first_failure]}
        newstatus['failures'] = failures
        userinfo[swatbot.Field.USER_STATUS] = [newstatus]

        # if len(failures) > 1:
        #     otherstatus = {'status': swatbot.TriageStatus.OTHER,
        #                    'comment': 'Previous step failed',
        #                    }
        #     otherstatus['failures'] = {k: v for k, v in failures.items()
        #                                if k != first_failure}
        #     userinfo[swatbot.Field.USER_STATUS].append(otherstatus)

    if entry >= len(infos):
        return None

    return entry


@main.command()
@add_options(failures_list_options)
@click.option('--open-url-with',
              help="Open the swatbot url with given program")
@click.option('--include-already-reviewed', '-a', is_flag=True,
              help="Include already reviewed issues")
def review_pending_failures(open_url_with: str, include_already_reviewed: bool,
                            *args, **kwargs):
    infos, userinfos = swatbot.get_failure_infos(*args, **kwargs)

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
        entry = review_menu(infos, userinfos, entry)

    swatbot.save_user_infos(userinfos)


@main.command()
@click.option('--dry-run', '-n', is_flag=True,
              help="Only shows what would be done")
def publish_new_reviews(dry_run: bool):
    userinfos = swatbot.get_user_infos()

    logger.info("Loading build failures...")
    failures = swatbot.get_stepfailures(refresh=swatbot.RefreshPolicy.FORCE)
    pending_failures = {int(failure['id']): failure for failure in failures
                        if failure['attributes']['triage'] == 0}

    reviews: dict[tuple[swatbot.TriageStatus, Any], list[dict]] = {}
    for buildid, userinfo in userinfos.items():
        if swatbot.Field.USER_STATUS not in userinfo:
            continue

        userstatuses = userinfo.get(swatbot.Field.USER_STATUS, [])
        for userstatus in userstatuses:
            status = userstatus.get('status')
            comment = userstatus.get('comment')
            if not status or not comment:
                continue

            # Make sure failures are still pending
            userstatus['failures'] = {k: v for
                                      k, v in userstatus['failures'].items()
                                      if k in pending_failures}

            reviews.setdefault((status, comment), []).append(userstatus)

    for (status, comment), entries in reviews.items():
        bugurl = None

        if status == swatbot.TriageStatus.BUG:
            logs = [entry['bugzilla-comment'] for entry in entries]
            try:
                bugid = int(comment)
            except ValueError:
                bugid = None

            if bugid:
                comment = bugurl = bugzilla.get_bug_url(bugid)
                logging.debug('Need to update %s with %s', bugurl,
                              ", ".join(logs))
                strlogs = '\n'.join(logs)
                if not dry_run:
                    print(f"\nPlease update {bugurl} ticket id with:\n"
                          f"{'-'*40}\n"
                          f"{strlogs}\n"
                          f"{'-'*40}\n")
            else:
                strlogs = '\n'.join(logs)
                if not dry_run:
                    print(f"\nPlease update {comment} ticket id with:\n"
                          f"{'-'*40}\n"
                          f"{strlogs}\n"
                          f"{'-'*40}\n")

        for entry in entries:
            for failureid, failuredata in entry['failures'].items():
                logging.debug('Need to update failure %s (%s) '
                              'to status %s (%s) with "%s"',
                              failureid, failuredata['stepname'], status,
                              status.name.title(), comment)
                if not dry_run:
                    # TODO: remove and publish result using REST
                    failure = pending_failures[failureid]
                    buildid = failure['relationships']['build']['data']['id']
                    build = swatbot.get_build(buildid)
                    buildcollection = build['relationships']['buildcollection']
                    colid = buildcollection['data']['id']
                    swat_url = f"{swatbot.BASE_URL}/collection/{colid}/"

                    print(f'Please update failure {failureid} '
                          f'("{failuredata["stepname"]}" on {swat_url} ) '
                          f'to status {status.name.title()} '
                          f'with "{comment}"')


if __name__ == '__main__':
    main()
