#!/usr/bin/env python3

import click
import logging
import tabulate
import subprocess
import shlex
import swatbot
import os
import tempfile
import yaml
import pathlib

logger = logging.getLogger(__name__)

BINDIR = pathlib.Path(__file__).parent.resolve()
DATADIR = BINDIR / "data"


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

    shown_fields = [
        swatbot.Field.BUILD,
        swatbot.Field.STATUS,
        swatbot.Field.TEST,
        swatbot.Field.OWNER,
        swatbot.Field.WORKER,
        swatbot.Field.COMPLETED,
        swatbot.Field.SWAT_URL,
        # swatbot.Field.AUTOBUILDER_URL,
        # swatbot.Field.STEPS,
    ]
    headers = [str(f) for f in shown_fields]
    table = [[info[field] for field in shown_fields] for info in infos]

    print(tabulate.tabulate(table, headers=headers))

    logging.info("%s entries found (%s warnings and %s errors)", len(infos),
                 len([i for i in infos
                      if i[swatbot.Field.STATUS] == swatbot.Status.ERROR]),
                 len([i for i in infos
                      if i[swatbot.Field.STATUS] == swatbot.Status.WARNING]))


@main.command()
@add_options(failures_list_options)
@click.option('--open-url-with',
              help="Open the swatbot url with given program")
def review_pending_failures(open_url_with: str, *args, **kwargs):
    infos = swatbot.get_failure_infos(*args, **kwargs)

    userinfos_file = DATADIR / "userinfos.yaml"
    if userinfos_file.exists():
        with userinfos_file.open('r') as f:
            pretty_userinfos = yaml.load(f, Loader=yaml.Loader)
            userinfos = {bid: {swatbot.Field(k): v for k, v in info.items()}
                         for bid, info in pretty_userinfos.items()}
    else:
        userinfos = {}

    if not infos:
        return

    running = True
    entry = 0
    while running:
        info = infos[entry]
        userinfo = userinfos.setdefault(info[swatbot.Field.BUILD], {})

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
        usernotes = userinfo.get(swatbot.Field.USER_NOTES)
        if usernotes:
            table.append([swatbot.Field.USER_NOTES, usernotes])

        print()
        print(tabulate.tabulate(table))
        print()

        if open_url_with:
            url = info[swatbot.Field.SWAT_URL]
            subprocess.run(shlex.split(f"{open_url_with} {url}"))

        print("n(ext)")
        print("p(revious)")
        print("s(et-status)")
        print("e(dit-notes)")
        print("q(uit)")

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
                running = False
            elif line.strip() in ["e", "edit-notes"]:
                editor = os.environ.get("EDITOR", "vim")

                with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
                    if usernotes:
                        f.write(usernotes)
                    f.close()
                    subprocess.run(shlex.split(f"{editor} {f.name}"))
                    with open(f.name, mode='r') as fr:
                        userinfo[swatbot.Field.USER_NOTES] = fr.read()
                    os.unlink(f.name)

            else:
                logger.warning("Invalid command")
                continue
            break

    pretty_userinfos = {bid: {str(k): v for k, v in info.items()}
                        for bid, info in userinfos.items()}

    # TODO: version file
    with userinfos_file.open('w') as f:
        yaml.dump(pretty_userinfos, f)


if __name__ == '__main__':
    main()
