#!/usr/bin/env python3

"""Swatbot review functions."""

import logging
import sys
from typing import Any, Optional

import click
import requests
import tabulate
from simple_term_menu import TerminalMenu  # type: ignore

from . import swatbot
from . import swatbuild
from .bugzilla import Bugzilla
from . import utils
from . import userdata
from .webrequests import RefreshPolicy, Session

logger = logging.getLogger(__name__)


def _prompt_bug_infos(build: swatbuild.Build,
                      is_abint: bool):
    """Create new status of type BUG for a given failure."""
    if is_abint:
        abints = Bugzilla.get_abints()
        abint_list = [f"{k} {v}" for (k, v) in abints.items()]

        abint_menu = TerminalMenu(abint_list, title="Bug", search_key=None,
                                  raise_error_on_interrupt=True)
        abint_index = abint_menu.show()

        if not abint_index:
            return None
        bugnum, _, _ = abint_list[abint_index].partition(' ')
    else:
        while True:
            bugnum_str = input('Bug number:').strip()
            if bugnum_str.isnumeric():
                bugnum = int(bugnum_str)
                break

            if bugnum_str.strip() == "q":
                return None

            logger.warning("Invalid issue: %s", bugnum_str)

    print("Please set the comment content")
    logurl = build.get_first_failure().get_log_url()
    if logurl:
        testmachine = " ".join([build.test, build.worker])
        bcomment = click.edit("\n".join([testmachine, logurl]),
                              require_save=False)
    else:
        bcomment = click.edit(None, require_save=False)

    newstatus = userdata.Triage()
    newstatus.status = swatbot.TriageStatus.BUG
    newstatus.comment = bugnum
    newstatus.extra['bugzilla-comment'] = bcomment
    return newstatus


def _create_new_status(build: swatbuild.Build, command: str
                       ) -> userdata.Triage:
    """Create new status for a given failure."""
    newstatus = userdata.Triage()
    if command in ["a", "b"]:
        newstatus = _prompt_bug_infos(build, command == "a")
    elif command == "c":
        newstatus.status = swatbot.TriageStatus.CANCELLED
    elif command == "m":
        newstatus.status = swatbot.TriageStatus.MAIL_SENT
    elif command == "i" and utils.MAILNAME:
        newstatus.status = swatbot.TriageStatus.MAIL_SENT
        newstatus.comment = f"Mail sent by {utils.MAILNAME}"
    elif command == "o":
        newstatus.status = swatbot.TriageStatus.OTHER
    elif command == "f":
        newstatus.status = swatbot.TriageStatus.OTHER
        newstatus.comment = 'Fixed'
    elif command == "t":
        newstatus.status = swatbot.TriageStatus.NOT_FOR_SWAT

    if newstatus and not newstatus.comment:
        newstatus.comment = input('Comment:').strip()

    return newstatus


def _list_failures_menu(builds: list[swatbuild.Build],
                        userinfos: userdata.UserInfos,
                        entry: int) -> int:
    """Allow the user to select the failure to review in a menu."""
    def preview_failure(fstr):
        fnum = int(fstr.split()[0])
        build = [b for (i, b) in enumerate(builds) if b.id == fnum][0]
        return build.format_description(userinfos[fnum])

    shown_fields = [
        swatbuild.Field.BUILD,
        swatbuild.Field.TEST,
        swatbuild.Field.OWNER,
    ]
    entries = [[build.get(f) for f in shown_fields] for build in builds]
    tabulated_entries = tabulate.tabulate(entries, tablefmt="plain")
    failures_menu = TerminalMenu(tabulated_entries.splitlines(),
                                 title="Failures",
                                 cursor_index=entry,
                                 preview_command=preview_failure,
                                 raise_error_on_interrupt=True)
    newentry = failures_menu.show()
    if newentry is not None:
        entry = newentry

    return entry


def _handle_navigation_command(builds: list[swatbuild.Build],
                               userinfos: userdata.UserInfos,
                               command: str, entry: int
                               ) -> tuple[bool, Optional[int]]:
    if command == "q":  # Quit
        return (True, None)

    if command == "n":  # Next
        entry += 1
    elif command == "p":  # Previous
        if entry >= 1:
            entry -= 1
        else:
            logger.warning("This is the first entry")
    elif command == "l":  # List
        entry = _list_failures_menu(builds, userinfos, entry)
    else:
        return (False, entry)

    if entry >= len(builds):
        return (True, None)

    return (True, entry)


def _handle_view_command(build: swatbuild.Build, command: str) -> bool:
    if command == "u":  # Open autobuilder URL
        click.launch(build.autobuilder_url)
        return True
    if command == "w":  # Open swatbot URL
        click.launch(build.swat_url)
        return True
    if command == "g":  # Open stdio log
        build.get_first_failure().open_log_url()
        return True
    if command == "x":  # Open stdio log in pager  # TODO: rename ?
        try:
            logurl = build.get_first_failure().get_log_raw_url()
            if logurl:
                logdata = Session().get(logurl)
                click.echo_via_pager(logdata)
            else:
                logger.warning("Failed to find stdio log")
        except requests.exceptions.ConnectionError:
            logger.warning("Failed to download stdio log")
        return True

    return False


def _handle_edit_command(build: swatbuild.Build, userinfo: userdata.UserInfo,
                         command: str) -> tuple[bool, bool]:
    if command == "e":  # Edit notes
        userinfo.set_notes(click.edit(userinfo.get_notes(),
                                      require_save=False))
        return (True, True)
    if command in ["a", "b", "c", "m", "i", "o", "f", "t"]:
        # Set new status
        newstatus = _create_new_status(build, command)
        if newstatus:
            newstatus.failures = list(build.failures.keys())
            userinfo.triages = [newstatus]
            return (True, True)
        return (True, False)
    if command == "r":  # Reset status
        userinfo.triages = []
        return (True, True)

    return (False, False)


_commands = [
    "[a] ab-int",
    "[b] bug opened",
    "[c] cancelled no errors",
    "[m] mail sent",
    f"[i] mail sent by {utils.MAILNAME}" if utils.MAILNAME else "",
    "[o] other",
    "[f] other: Fixed",
    "[t] not for swat",
    "[r] reset status",
    None,
    "[e] edit notes",
    "[u] open autobuilder URL",
    "[w] open swatbot URL",
    "[g] open stdio log of first failed step URL",
    "[x] open stdio log of first failed step in pager",
    None,
    "[n] next",
    "[p] previous",
    "[l] list all failures",
    "[q] quit",
]


valid_commands = [c for c in _commands if c != ""]


def review_menu(builds: list[swatbuild.Build],
                userinfos: userdata.UserInfos,
                entry: int,
                statusbar: str) -> tuple[Optional[int], bool]:
    """Allow a user to interactively triage a failure."""
    changed = False

    default_action = "n"
    default_index = [c[1] if c and len(c) > 1 else None
                     for c in valid_commands].index(default_action)
    action_menu = TerminalMenu(valid_commands, title="Action",
                               cursor_index=default_index,
                               status_bar=statusbar,
                               raise_error_on_interrupt=True)

    build = builds[entry]
    userinfo = userinfos[build.id]

    while True:
        try:
            command_index = action_menu.show()
            if command_index is None:
                return (None, False)
            command = valid_commands[command_index][1]
        except EOFError:
            return (None, False)

        (handled, new_entry) = _handle_navigation_command(builds, userinfos,
                                                          command, entry)
        if handled:
            break
        new_entry = entry

        handled = _handle_view_command(build, command)
        if handled:
            break

        handled, changed = _handle_edit_command(build, userinfo, command)
        if handled:
            break

    return (new_entry, changed)


def review_failures(builds: list[swatbuild.Build],
                    userinfos: userdata.UserInfos,
                    open_autobuilder_url: bool,
                    open_swatbot_url: bool,
                    open_stdio_url: bool):
    """Allow a user to interactively triage a list of failures."""
    utils.clear()

    entry: Optional[int] = 0
    prev_entry = None
    kbinter = False
    show_infos = True
    while entry is not None:
        try:
            build = builds[entry]
            userinfo = userinfos.get(build.id, {})

            if prev_entry != entry:
                if open_autobuilder_url:
                    click.launch(build.autobuilder_url)
                if open_swatbot_url:
                    click.launch(build.swat_url)
                if open_stdio_url:
                    if build.test in ["a-full", "a-quick"]:
                        # TODO: can we do anything better here ?
                        logger.warning("Test is %s, "
                                       "fail log might be the log of a child",
                                       build.test)
                    build.get_first_failure().open_log_url()

            if show_infos:
                print(build.format_description(userinfo))
                print()
                show_infos = False

            prev_entry = entry
            statusbar = f"Progress: {entry+1}/{len(builds)}"
            entry, changed = review_menu(builds, userinfos, entry, statusbar)
            if changed or entry != prev_entry:
                utils.clear()
                show_infos = True
        except KeyboardInterrupt:
            if kbinter:
                sys.exit(1)
            else:
                logger.warning("^C pressed. "
                               "Press again to quit without saving")
                kbinter = True
                continue
        except Exception as error:
            filename = userinfos.save(suffix="-crash")
            logging.error("Got exception, saving userinfos in a crash file: "
                          "You may want to retrieve data from there (%s)",
                          filename)
            raise error
        kbinter = False


def get_new_reviews() -> dict[tuple[swatbot.TriageStatus, Any],
                              list[userdata.Triage]]:
    """Get a list of new reviews waiting to be published on swatbot server."""
    userinfos = userdata.UserInfos()

    logger.info("Loading pending reviews...")
    reviews: dict[tuple[swatbot.TriageStatus, Any], list[userdata.Triage]] = {}
    with click.progressbar(userinfos.items()) as userinfos_progress:
        for buildid, userinfo in userinfos_progress:
            for triage in userinfo.triages:
                status = triage.status
                comment = triage.comment
                if not status:
                    continue

                if not comment:
                    logger.warning("Review for failure %s is missing comment: "
                                   "skipping", buildid)
                    continue

                def is_pending(failure_id):
                    refresh = RefreshPolicy.FORCE
                    failure = swatbot.get_stepfailure(failure_id,
                                                      refresh_override=refresh)
                    return failure['attributes']['triage'] == 0

                # Make sure failures are still pending
                triage.failures = {f for f in triage.failures if is_pending(f)}

                reviews.setdefault((status, comment), []).append(triage)

    userinfos.save()

    return reviews
