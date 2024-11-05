#!/usr/bin/env python3

"""Swatbot review functions."""

import logging
import shutil
import sys
import textwrap
from typing import Any, Optional

import click
from simple_term_menu import TerminalMenu  # type: ignore

from . import logsview
from . import swatbotrest
from . import swatbuild
from .bugzilla import Bugzilla
from . import utils
from . import userdata

logger = logging.getLogger(__name__)


def _prompt_bug_infos(build: swatbuild.Build,
                      is_abint: bool):
    """Create new status of type BUG for a given failure."""
    if is_abint:
        abints = Bugzilla.get_abints()
        abrefresh = "Refresh AB-INT list from server"

        while True:
            abint_list = [
                abrefresh,
                *[f"{k} {v}" for (k, v) in abints.items()],
            ]

            abint_menu = TerminalMenu(abint_list, title="Bug", search_key=None,
                                      raise_error_on_interrupt=True)
            abint_index = abint_menu.show()

            if abint_index is None:
                return None

            if abint_list[abint_index] == abrefresh:
                abints = Bugzilla.get_abints(force_refresh=True)
            else:
                break
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
        testinfos = " ".join([build.test, build.worker, build.branch])
        bcomment = click.edit("\n".join([testinfos, logurl]),
                              require_save=False)
    else:
        bcomment = click.edit(None, require_save=False)

    newstatus = userdata.Triage()
    newstatus.status = swatbotrest.TriageStatus.BUG
    newstatus.comment = bugnum
    newstatus.extra['bugzilla-comment'] = bcomment
    return newstatus


def _create_new_status(build: swatbuild.Build, command: str
                       ) -> Optional[userdata.Triage]:
    """Create new status for a given failure."""
    newstatus = userdata.Triage()
    if command in ["a", "b"]:
        newstatus = _prompt_bug_infos(build, command == "a")
    elif command == "c":
        if build.status != swatbuild.Status.CANCELLED:
            logging.error("Only cancelled builds can be triaged as cancelled")
            return None
        newstatus.status = swatbotrest.TriageStatus.CANCELLED
        newstatus.comment = "Cancelled"
    elif command == "m":
        newstatus.status = swatbotrest.TriageStatus.MAIL_SENT
    elif command == "i" and utils.MAILNAME:
        newstatus.status = swatbotrest.TriageStatus.MAIL_SENT
        newstatus.comment = f"Mail sent by {utils.MAILNAME}"
    elif command == "o":
        newstatus.status = swatbotrest.TriageStatus.OTHER
    elif command == "f":
        newstatus.status = swatbotrest.TriageStatus.OTHER
        newstatus.comment = 'Fixed'
    elif command == "d":
        newstatus.status = swatbotrest.TriageStatus.OTHER
        newstatus.comment = 'Patch dropped'
    elif command == "t":
        newstatus.status = swatbotrest.TriageStatus.NOT_FOR_SWAT

    if newstatus and not newstatus.comment:
        newstatus.comment = input('Comment:').strip()

    return newstatus


def _list_failures_menu(builds: list[swatbuild.Build],
                        userinfos: userdata.UserInfos,
                        entry: int) -> int:
    """Allow the user to select the failure to review in a menu."""
    termsize = shutil.get_terminal_size((80, 20))
    width = termsize.columns - 2  # Borders

    def preview_failure(fstr):
        fnum = int(fstr.split()[0])
        build = [b for (i, b) in enumerate(builds) if b.id == fnum][0]
        return build.format_description(userinfos[fnum], width)

    shown_fields = [
        swatbuild.Field.BUILD,
        swatbuild.Field.TEST,
        swatbuild.Field.OWNER,
    ]
    entries = [[build.get(f) for f in shown_fields] for build in builds]
    failures_menu = utils.tabulated_menu(entries, title="Failures",
                                         cursor_index=entry,
                                         preview_command=preview_failure)
    newentry = failures_menu.show()
    if newentry is not None:
        entry = newentry

    return entry


def _handle_navigation_command(builds: list[swatbuild.Build],
                               userinfos: userdata.UserInfos,
                               command: str, entry: int
                               ) -> tuple[bool, bool, Optional[int]]:
    need_refresh = False

    if command == "q":  # Quit
        return (True, need_refresh, None)

    if command == "n":  # Next
        entry += 1
        need_refresh = True
    elif command == "p":  # Previous
        if entry >= 1:
            entry -= 1
            need_refresh = True
        else:
            logger.warning("This is the first entry")
    elif command == "s":  # List
        utils.clear()
        need_refresh = True
        entry = _list_failures_menu(builds, userinfos, entry)
    else:
        return (False, need_refresh, entry)

    if entry >= len(builds):
        return (True, need_refresh, None)

    return (True, need_refresh, entry)


def _handle_view_command(build: swatbuild.Build, command: str
                         ) -> tuple[bool, bool]:
    if command == "u":  # Open autobuilder URL
        click.launch(build.autobuilder_url)
        return (True, False)
    if command == "w":  # Open swatbot URL
        click.launch(build.swat_url)
        return (True, False)
    if command == "g":  # Open stdio log
        build.get_first_failure().open_log_url()
        return (True, False)
    if command == "l":  # View stdio log
        failure = build.get_first_failure()
        need_refresh = logsview.show_log_menu(failure, 'stdio')
        return (True, need_refresh)
    if command == "x":  # Explore logs
        need_refresh = logsview.show_logs_menu(build)
        return (True, need_refresh)

    return (False, False)


def _handle_edit_command(build: swatbuild.Build, userinfo: userdata.UserInfo,
                         command: str) -> tuple[bool, bool]:
    if command == "e":  # Edit notes
        userinfo.set_notes(click.edit(userinfo.get_notes(),
                                      require_save=False))
        return (True, True)
    if command in ["a", "b", "c", "m", "i", "o", "f", "d", "t"]:
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
    "[d] other: Patch dropped",
    "[t] not for swat",
    "[r] reset status",
    None,
    "[e] edit notes",
    "[u] open autobuilder URL",
    "[w] open swatbot URL",
    "[g] open stdio log of first failed step URL",
    "[l] show stdio log of first failed step",
    "[x] explore all logs",
    None,
    "[n] next",
    "[p] previous",
    "[s] select in failures list",
    "[q] quit",
]


valid_commands = [c for c in _commands if c != ""]


def review_menu(builds: list[swatbuild.Build],
                userinfos: userdata.UserInfos,
                entry: int,
                statusbar: str) -> tuple[Optional[int], bool]:
    """Allow a user to interactively triage a failure."""
    need_refresh = False

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

        (handled, need_refresh, new_entry) = \
            _handle_navigation_command(builds, userinfos, command, entry)
        if handled:
            break

        handled, need_refresh = _handle_view_command(build, command)
        if handled:
            break

        handled, need_refresh = _handle_edit_command(build, userinfo, command)
        if handled:
            break

    return (new_entry, need_refresh)


def _show_infos(build: swatbuild.Build, userinfo: userdata.UserInfo):
    # Reserve chars for spacing.
    reserved = 8
    termwidth = shutil.get_terminal_size((80, 20)).columns
    width = termwidth - reserved
    maxhighlights = 5

    print(build.format_description(userinfo, width))
    print()

    failure = build.get_first_failure()
    highlights = logsview.get_log_highlights(failure, "stdio")
    wrapped_highlights = [textwrap.indent(line, " " * 4)
                          for highlight in highlights[:maxhighlights]
                          for line in textwrap.wrap(highlight, width)
                          ]
    print("Key log infos:")
    print("\n".join(wrapped_highlights))
    print()


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
                    build.get_first_failure().open_log_url()

            if show_infos:
                _show_infos(build, userinfo)
                show_infos = False

            prev_entry = entry
            statusbar = f"Progress: {entry+1}/{len(builds)}"
            entry, need_refresh = review_menu(builds, userinfos, entry,
                                              statusbar)
            if need_refresh or entry != prev_entry:
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


def get_new_reviews() -> dict[tuple[swatbotrest.TriageStatus, Any],
                              list[userdata.Triage]]:
    """Get a list of new reviews waiting to be published on swatbot server."""
    userinfos = userdata.UserInfos()

    logger.info("Loading pending reviews...")
    reviews: dict[tuple[swatbotrest.TriageStatus, Any],
                  list[userdata.Triage]] = {}
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
                    r = swatbotrest.RefreshPolicy.FORCE
                    failure = swatbotrest.get_stepfailure(failure_id,
                                                          refresh_override=r)
                    return failure['attributes']['triage'] == 0

                # Make sure failures are still pending
                triage.failures = {f for f in triage.failures if is_pending(f)}

                reviews.setdefault((status, comment), []).append(triage)

    userinfos.save()

    return reviews
