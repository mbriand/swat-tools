#!/usr/bin/env python3

"""Swatbot review functions."""

import copy
import logging
# Readline modifies input() behaviour when imported
import readline  # noqa: F401 # pylint: disable=unused-import
import shutil
import sys
import textwrap
from typing import Any, Optional

import click
from simple_term_menu import TerminalMenu  # type: ignore

from . import logsview
from . import pokyciarchive
from . import swatbotrest
from . import swatbuild
from .bugzilla import Bugzilla
from . import utils
from . import userdata

logger = logging.getLogger(__name__)


def _format_bugzilla_comment(build: swatbuild.Build) -> Optional[str]:
    logurl = build.get_first_failure().get_log_url()
    if logurl:
        testinfos = " ".join([build.test, build.worker, build.branch,
                              f'completed at {build.completed}'])
        bcomment = "\n".join([testinfos, logurl])
    else:
        bcomment = None

    return bcomment


def _print_last_bugs(userinfos: userdata.UserInfos):
    """Print last used bug numbers."""
    # We only look for failures with unpublished new triages, this is kind
    # of a dumb solution but should be enough for a start.
    lastbugs = {triage.comment
                for userinfo in userinfos.values()
                for triage in userinfo.triages
                if triage.status == swatbotrest.TriageStatus.BUG}
    if lastbugs:
        print("Last used bugs:")
        for lastbug in lastbugs:
            line = f"{lastbug}: {Bugzilla.get_bug_title(lastbug)}"
            print(textwrap.indent(line, " " * 4))
        print()


def _prompt_bug_infos(build: swatbuild.Build, is_abint: bool,
                      userinfos: userdata.UserInfos):
    """Create new status of type BUG for a given failure."""

    def preview_bug(fstr):
        bugnum, _, _ = fstr.partition(' ')
        try:
            bugnum = int(bugnum)
        except ValueError:
            return None
        return Bugzilla.get_bug_description(bugnum)

    if is_abint:
        abints = Bugzilla.get_formatted_abints()
        abrefresh = "Refresh AB-INT list from server"

        while True:
            abint_list = [abrefresh, *abints]

            abint_menu = TerminalMenu(abint_list, title="Bug", search_key=None,
                                      raise_error_on_interrupt=True,
                                      preview_command=preview_bug)
            abint_index = abint_menu.show()

            if abint_index is None:
                return None

            if abint_list[abint_index] == abrefresh:
                abints = Bugzilla.get_formatted_abints(force_refresh=True)
            else:
                break
        bugnum, _, _ = abint_list[abint_index].partition(' ')
    else:
        _print_last_bugs(userinfos)

        while True:
            try:
                bugnum_str = input('Bug number:').strip()
            except EOFError:
                return None
            if not bugnum_str or bugnum_str.strip() == "q":
                return None

            if bugnum_str.isnumeric():
                bugnum = int(bugnum_str)
                break

            logger.warning("Invalid issue: %s", bugnum_str)

    print("Please set the comment content")
    bcomment = _format_bugzilla_comment(build)
    try:
        bcomment = click.edit(bcomment, require_save=False)
    except click.exceptions.ClickException as err:
        logger.warning("Got exception, aborting triage: %s", err)
        return None

    newstatus = userdata.Triage()
    newstatus.status = swatbotrest.TriageStatus.BUG
    newstatus.comment = bugnum
    newstatus.extra['bugzilla-comment'] = bcomment
    return newstatus


def _create_new_status(build: swatbuild.Build, command: str,
                       userinfos: userdata.UserInfos
                       ) -> Optional[userdata.Triage]:
    """Create new status for a given failure."""
    newstatus = userdata.Triage()
    if command in ["a", "b"]:
        newstatus = _prompt_bug_infos(build, command == "a", userinfos)
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
        try:
            newstatus.comment = input('Comment:').strip()
        except EOFError:
            return None
        if not newstatus.comment:
            return None

    return newstatus


def _list_failures_menu(builds: list[swatbuild.Build],
                        userinfos: userdata.UserInfos,
                        entry: int, **kwargs):
    termsize = shutil.get_terminal_size((80, 20))
    width = termsize.columns - 2  # Borders

    build = builds[entry]
    fingerprint = logsview.get_log_fingerprint(build.get_first_failure(),
                                               'stdio')

    def preview_failure(fstr):
        fnum = int(fstr.split()[0])
        build = [b for (i, b) in enumerate(builds) if b.id == fnum][0]
        return _get_infos(build, userinfos[fnum], width, 1)

    shown_fields = [
        swatbuild.Field.BUILD,
        swatbuild.Field.BRANCH,
        swatbuild.Field.TEST,
        swatbuild.Field.WORKER,
        swatbuild.Field.COMPLETED,
        swatbuild.Field.OWNER,
        swatbuild.Field.USER_STATUS,
    ]

    def format_build(build):
        userinfo = userinfos[build.id]
        data = [build.format_field(userinfo, f, False) for f in shown_fields]

        bfing = logsview.get_log_fingerprint(build.get_first_failure(),
                                             'stdio')
        similarity = logsview.get_similarity_score(fingerprint, bfing)
        data.insert(len(data) - 1,
                    f"similarity: {int(similarity*100):3}%" if similarity > .7
                    else "")
        return data
    entries = [format_build(b) for b in builds]
    failures_menu = utils.tabulated_menu(entries, title="Failures",
                                         cursor_index=entry,
                                         preview_command=preview_failure,
                                         preview_size=.5,
                                         **kwargs)
    return failures_menu.show()


def _select_failures_menu(builds: list[swatbuild.Build],
                          userinfos: userdata.UserInfos,
                          entry: int) -> list[int]:
    """Allow the user to select the failure to review in a menu."""
    return _list_failures_menu(builds, userinfos, entry, multi_select=True,
                               show_multi_select_hint=True,
                               multi_select_select_on_accept=False,
                               multi_select_empty_ok=True)


def _go_failures_menu(builds: list[swatbuild.Build],
                      userinfos: userdata.UserInfos,
                      entry: int) -> int:
    """Allow the user to select the failure to review in a menu."""
    newentry = _list_failures_menu(builds, userinfos, entry)
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
    elif command == "next pending failure":
        entry += 1
        while entry < len(builds) and userinfos[builds[entry].id].triages:
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
        entry = _go_failures_menu(builds, userinfos, entry)
    else:
        return (False, need_refresh, entry)

    if entry >= len(builds):
        return (True, need_refresh, None)

    return (True, need_refresh, entry)


def _handle_view_command(build: swatbuild.Build, command: str
                         ) -> tuple[bool, bool]:
    # pylint: disable=too-many-return-statements
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
    if command in ["v", "view git log"]:  # View git log
        base = build.git_info['base_commit']
        tip = build.git_info['tip_commit']
        if command == "v":
            options = ['--oneline']
        else:
            options = ["--patch", "--name-only"]
        success = pokyciarchive.show_log(tip, base, options)
        return (True, success)

    return (False, False)


def _can_show_git_log(build: swatbuild.Build) -> bool:
    return (build.git_info is not None
            and 'base_commit' in build.git_info
            and 'tip_commit' in build.git_info)


def _handle_edit_command(builds: list[swatbuild.Build],
                         userinfos: userdata.UserInfos,
                         command: str, entry: int) -> tuple[bool, bool]:
    build = builds[entry]
    userinfo = userinfos[build.id]

    if command == "e":  # Edit notes
        userinfo.set_notes(click.edit(userinfo.get_notes(),
                                      require_save=False))
        return (True, True)
    if command in ["a", "b", "c", "m", "i", "o", "f", "d", "t"]:
        # Set new status
        newstatus = _create_new_status(build, command, userinfos)
        if newstatus:
            newstatus.failures = list(build.failures.keys())
            userinfo.triages = [newstatus]
            return (True, True)
        return (True, False)
    if command == "copy status":
        def copy_status(status, build):
            newstatus = userdata.Triage()
            newstatus.status = status.status
            newstatus.comment = status.comment
            newstatus.extra = copy.deepcopy(status.extra)
            newstatus.failures = list(build.failures.keys())
            if newstatus.status == swatbotrest.TriageStatus.BUG:
                newstatus.extra['bugzilla-comment'] = \
                    _format_bugzilla_comment(build)
            return newstatus

        entries = _select_failures_menu(builds, userinfos, entry)
        if entries:
            targetbuilds = [builds[e] for e in entries if e != entry]
            for tbuild in targetbuilds:
                userinfos[tbuild.id].triages = [copy_status(s, tbuild)
                                                for s in userinfo.triages]
        return (True, False)
    if command == "r":  # Reset status
        userinfo.triages = []
        return (True, True)

    return (False, False)


def _get_commands(build: swatbuild.Build):
    commands = [
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
        "copy status",
        None,
        "[e] edit notes",
        "[u] open autobuilder URL",
        "[w] open swatbot URL",
        "[g] open stdio log of first failed step URL",
        "[l] show stdio log of first failed step",
        "[x] explore all logs",
        "[v] view git log (oneline)" if _can_show_git_log(build) else "",
        "view git log" if _can_show_git_log(build) else "",
        None,
        "[n] next",
        "next pending failure",
        "[p] previous",
        "[s] select in failures list",
        "[q] quit",
    ]

    return [c for c in commands if c != ""]


def review_menu(builds: list[swatbuild.Build],
                userinfos: userdata.UserInfos,
                entry: int,
                statusbar: str) -> tuple[Optional[int], bool]:
    """Allow a user to interactively triage a failure."""
    need_refresh = False

    build = builds[entry]

    commands = _get_commands(build)

    default_action = "n"
    default_index = [c[1] if c and len(c) > 1 else None
                     for c in commands].index(default_action)
    action_menu = TerminalMenu(commands, title="Action",
                               cursor_index=default_index,
                               status_bar=statusbar,
                               raise_error_on_interrupt=True)

    while True:
        try:
            command_index = action_menu.show()
            if command_index is None:
                return (None, False)
            command = commands[command_index]
            if command[0] == '[' and command[2] == ']':
                command = command[1]
        except EOFError:
            return (None, False)

        (handled, need_refresh, new_entry) = \
            _handle_navigation_command(builds, userinfos, command, entry)
        if handled:
            break

        handled, need_refresh = _handle_view_command(build, command)
        if handled:
            break

        handled, need_refresh = _handle_edit_command(builds, userinfos,
                                                     command, entry)
        if handled:
            break

    return (new_entry, need_refresh)


def _get_infos(build: swatbuild.Build, userinfo: userdata.UserInfo,
               width: int, maxfailures: Optional[int] = None) -> str:
    buf = []
    buf.append(build.format_description(userinfo, width, maxfailures))
    buf.append('')

    failure = build.get_first_failure()
    highlights = logsview.get_log_highlights(failure, "stdio")
    maxhighlights = 5
    wrapped_highlights = [textwrap.indent(line, " " * 4)
                          for highlight in highlights[:maxhighlights]
                          for line in textwrap.wrap(highlight, width)
                          ]
    buf.append("Key log infos:")
    buf.append("\n".join(wrapped_highlights))

    return '\n'.join(buf)


def _show_infos(build: swatbuild.Build, userinfo: userdata.UserInfo):
    # Reserve chars for spacing.
    reserved = 8
    termwidth = shutil.get_terminal_size((80, 20)).columns
    width = termwidth - reserved

    print(_get_infos(build, userinfo, width))
    print()


def review_failures(builds: list[swatbuild.Build],
                    userinfos: userdata.UserInfos,
                    urlopens: set[str]):
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
                build.open_urls(urlopens)

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


def batch_review_failures(builds: list[swatbuild.Build],
                          userinfos: userdata.UserInfos,
                          ask_confirm: bool, status: swatbotrest.TriageStatus,
                          status_comment: str):
    """Allow a user batch triage a list of failures."""
    commands = [
        "[y] yes",
        "[n] no",
        "[q] quit"
    ]

    action_menu = TerminalMenu(commands, title="Apply triage on this failure?",
                               raise_error_on_interrupt=True)

    for build in builds:
        userinfo = userinfos.get(build.id, {})
        if ask_confirm:
            _show_infos(build, userinfo)

            command_index = action_menu.show()
            if command_index is None:
                return
            command = commands[command_index][1]
            if command == 'q':
                return
            if command == 'n':
                continue

        newstatus = userdata.Triage()
        newstatus.status = status
        newstatus.comment = status_comment
        newstatus.failures = list(build.failures.keys())
        if status == swatbotrest.TriageStatus.BUG:
            newstatus.extra['bugzilla-comment'] = \
                _format_bugzilla_comment(build)
        userinfo.triages = [newstatus]

        logging.info("applying triage %s (%s) on build %s", status,
                     status_comment, build.format_tiny_description())


def get_new_reviews() -> dict[tuple[swatbotrest.TriageStatus, Any],
                              list[userdata.Triage]]:
    """Get a list of new reviews waiting to be published on swatbot server."""

    def update_userinfo(userinfo):
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
                pol = swatbotrest.RefreshPolicy.FORCE
                failure = swatbotrest.get_stepfailure(failure_id,
                                                      refresh_override=pol)
                return failure['attributes']['triage'] == 0

            # Make sure failures are still pending
            triage.failures = {f for f in triage.failures if is_pending(f)}

            if triage.failures:
                reviews.setdefault((status, comment), []).append(triage)

    userinfos = userdata.UserInfos()

    reviews: dict[tuple[swatbotrest.TriageStatus, Any],
                  list[userdata.Triage]] = {}
    executor = utils.ExecutorWithProgress(8)
    for buildid, userinfo in userinfos.items():
        executor.submit("Updating pending review", update_userinfo, userinfo)

    executor.run()
    userinfos.save()

    return reviews
