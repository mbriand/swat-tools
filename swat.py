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
    infos = swatbot.get_failure_infos(*args, **kwargs)

    for info in infos:
        if open_url_with:
            url = info[swatbot.Field.SWAT_URL]
            subprocess.run(shlex.split(f"{open_url_with} {url}"))

    def format(info, field):
        if field == swatbot.Field.USER_STATUS:
            status = info.get(field)
            if status:
                return f"{status['status'].name.title()}: {status['comment']}"
            return None
        return info[field]

    shown_fields = [
        swatbot.Field.BUILD,
        swatbot.Field.STATUS,
        swatbot.Field.TEST,
        swatbot.Field.OWNER,
        swatbot.Field.WORKER,
        swatbot.Field.COMPLETED,
        swatbot.Field.SWAT_URL,
        swatbot.Field.USER_STATUS,
    ]
    headers = [str(f) for f in shown_fields]
    table = [[format(info, field) for field in shown_fields] for info in infos]

    print(tabulate.tabulate(table, headers=headers))

    logging.info("%s entries found (%s warnings and %s errors)", len(infos),
                 len([i for i in infos
                      if i[swatbot.Field.STATUS] == swatbot.Status.ERROR]),
                 len([i for i in infos
                      if i[swatbot.Field.STATUS] == swatbot.Status.WARNING]))


def review_status_menu(info: dict[swatbot.Field, Any]):
    print("a(b-int)")
    print("b(ug-opened)")
    print("m(ail-sent)")
    print(f"(ma)i(l-sent-by-me): {MAILNAME}")
    print("o(ther)")
    print("n(ot-for-swat)")
    print("q(uit): Go back to previous menu")

    newstatus: Optional[dict] = None
    while True:
        line = input('action: ')

        if line.strip() in ["a", "ab-int"]:
            abints = bugzilla.get_abints()
            while True:
                abint = input('Bug number:').strip()
                if abint.isnumeric() and int(abint) in abints:
                    newstatus = {'status': swatbot.TriageStatus.BUG,
                                 'comment': int(abint)
                                 }
                    break
                elif abint.strip() in ["q", "quit"]:
                    break
                else:
                    logging.warning("Unknown AB-INT issue: %s", abint)
                    print(tabulate.tabulate(abints.items()))
        elif line.strip() in ["b", "bug-opened"]:
            newstatus = {'status': swatbot.TriageStatus.BUG,
                         'comment': input('Bug URL:').strip()
                         }
        elif line.strip() in ["m", "mail-sent"]:
            newstatus = {'status': swatbot.TriageStatus.MAIL_SENT,
                         'comment': input('Comment:').strip()
                         }
        elif line.strip() in ["i", "mail-sent-by-me"]:
            newstatus = {'status': swatbot.TriageStatus.MAIL_SENT,
                         'comment': f"Mail sent by {MAILNAME}"
                         }
        elif line.strip() in ["o", "other"]:
            newstatus = {'status': swatbot.TriageStatus.OTHER,
                         'comment': input('Comment:').strip()
                         }
        elif line.strip() in ["n", "not-for-swat"]:
            newstatus = {'status': swatbot.TriageStatus.NOT_FOR_SWAT,
                         'comment': input('Comment:').strip()
                         }
        elif line.strip() in ["q", "quit"]:
            pass
        else:
            logger.warning("Invalid status")
            continue
        break

    if newstatus:
        info[swatbot.Field.USER_STATUS] = newstatus


def review_menu(infos: list[dict[swatbot.Field, Any]],
                entry: int) -> Optional[int]:
    print("n(ext)")
    print("p(revious)")
    print("s(et-status)")
    print("e(dit-notes)")
    print("q(uit)")

    info = infos[entry]

    while True:
        line = input('action: ')
        if line.strip() in ["n", "next"]:
            if entry < len(infos) - 1:
                entry += 1
            else:
                logger.warning("This is the last entry")
                continue
        elif line.strip() in ["p", "prev", "previous"]:
            if entry >= 1:
                entry -= 1
            else:
                logger.warning("This is the first entry")
                continue
        elif line.strip() in ["q", "quit"]:
            return None
        elif line.strip() in ["e", "edit-notes"]:
            editor = os.environ.get("EDITOR", "vim")

            with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
                usernotes = info.get(swatbot.Field.USER_NOTES)
                if usernotes:
                    f.write(usernotes)
                f.close()
                subprocess.run(shlex.split(f"{editor} {f.name}"))
                with open(f.name, mode='r') as fr:
                    info[swatbot.Field.USER_NOTES] = fr.read()
                os.unlink(f.name)
        elif line.strip() in ["s", "set-status"]:
            review_status_menu(info)
        else:
            logger.warning("Invalid command")
            continue
        break

    return entry


@main.command()
@add_options(failures_list_options)
@click.option('--open-url-with',
              help="Open the swatbot url with given program")
def review_pending_failures(open_url_with: str, *args, **kwargs):
    infos = swatbot.get_failure_infos(*args, **kwargs)

    if not infos:
        return

    entry: Optional[int] = 0
    while entry is not None:
        info = infos[entry]

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
        table.append([swatbot.Field.STEPS,
                      "\n".join(info[swatbot.Field.STEPS])])

        usernotes = info.get(swatbot.Field.USER_NOTES)
        if usernotes:
            table.append([swatbot.Field.USER_NOTES, usernotes])

        userstatus = info.get(swatbot.Field.USER_STATUS)
        if userstatus:
            def format(v):
                if isinstance(v, swatbot.TriageStatus):
                    return v.name.title().replace('_', ' ')
                return v
            statusstr = "\n".join([f'{str(k).title()}: {format(v)}'
                                   for k, v in userstatus.items()])
            table.append([swatbot.Field.USER_STATUS, statusstr])

        print()
        print(tabulate.tabulate(table))
        print()

        if open_url_with:
            url = info[swatbot.Field.SWAT_URL]
            subprocess.run(shlex.split(f"{open_url_with} {url}"))

        entry = review_menu(infos, entry)

    swatbot.save_user_infos(infos)


if __name__ == '__main__':
    main()
