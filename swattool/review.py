#!/usr/bin/env python3

"""Swatbot review functions.

This module provides functionality for interactive review and triage of
build failures from Swatbot.
"""

import copy
import logging
import shutil
import sys
import textwrap
from typing import Any, Optional

import click
from simple_term_menu import TerminalMenu  # type: ignore

from . import logfingerprint
from . import logsview
from . import pokyciarchive
from . import swatbotrest
from . import swatbuild
from . import swatlogs
from .bugzilla import Bugzilla
from . import utils
from . import userdata

logger = logging.getLogger(__name__)


class ReviewMenu:
    """Interactive review session manager.

    Provides a menu-based interface for reviewing and triaging build failures.
    """

    def __init__(self,
                 builds: list[swatbuild.Build],
                 userinfos: userdata.UserInfos,
                 urlopens: Optional[set[str]] = None):
        if urlopens is None:
            urlopens = set()

        self.builds = builds
        self.userinfos = userinfos
        self.urlopens = urlopens
        self.entry = 0
        self.done = True
        self.need_refresh = False
        self.failure_menu = FailureMenu(self.builds, self.userinfos)

    def show(self):
        """Allow a user to interactively triage a list of failures.

        Presents an interactive interface for navigating through failures and
        managing their triage status.
        """
        utils.clear()

        prev_entry = None
        kbinter = False
        show_infos = True
        self.entry = 0
        self.done = False
        while not self.done:
            try:
                build = self.builds[self.entry]
                if prev_entry != self.entry:
                    build.open_urls(self.urlopens)

                if show_infos:
                    self._show_infos(build)
                    show_infos = False

                prev_entry = self.entry
                self.review_menu()
                if self.need_refresh:
                    self.need_refresh = False
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
                filename = self.userinfos.save(suffix="-crash")
                logging.error(
                    "Got exception, saving userinfos in a crash file: "
                    "You may want to retrieve data from there (%s)",
                    filename)
                raise error
            kbinter = False

    def _show_infos(self, build: swatbuild.Build):
        # Reserve chars for spacing.
        reserved = 8
        termwidth = shutil.get_terminal_size((80, 20)).columns
        width = termwidth - reserved

        userinfo = self.userinfos.get(build.id, {})
        print(_get_infos(build, userinfo, width))
        print()

    def _get_commands(self):
        build = self.builds[self.entry]
        commands = [
            "[t] triage failure",
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

    def _get_triage_commands(self):
        build = self.builds[self.entry]
        simcount = len(_get_similar_builds(build, self.builds)) - 1
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
            f"[s] copy status (show {simcount} similar failures)",
        ]

        return [c for c in commands if c != ""]

    def _show_menu(self, commands: list[str], cursor_index: Optional[int]
                   ) -> Optional[str]:
        status_bar = f"Progress: {self.entry + 1}/{len(self.builds)}"
        action_menu = TerminalMenu(commands, title="Action",
                                   status_bar=status_bar,
                                   cursor_index=cursor_index,
                                   raise_error_on_interrupt=True)

        try:
            command_index = action_menu.show()
            if command_index is None:
                return None
            command = commands[command_index]
            if command[0] == '[' and command[2] == ']':
                command = command[1]
        except EOFError:
            return None

        return command

    def batch_menu(self, ask_confirm: bool,
                   status: swatbotrest.TriageStatus, status_comment: str):
        """Allow a user batch triage a list of failures.

        Args:
            ask_confirm: Whether to ask for confirmation for each failure
            status: Triage status to set for all failures
            status_comment: Comment to set for the triage status
        """
        commands = [
            "[y] yes",
            "[n] no",
            "[q] quit"
        ]

        for build in self.builds:
            userinfo = self.userinfos.get(build.id, {})
            if ask_confirm:
                self._show_infos(build)

                command = self._show_menu(commands, None)
                if command == 'q' or command is None:
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

    def triage_menu(self):
        """Allow a user to interactively triage a failure.

        Displays a menu of triage options and handles selection.
        """
        commands = self._get_triage_commands()

        while True:
            command = self._show_menu(commands, None)
            if not command:
                break

            handled = self._handle_triage_command(command)
            if handled:
                break

    def review_menu(self):
        """Allow a user to interactively review a failure.

        Displays the main menu of review options and handles user selection.
        """
        commands = self._get_commands()

        default_action = "next pending failure"
        default_index = commands.index(default_action)

        while True:
            command = self._show_menu(commands, default_index)

            handled = self._handle_navigation_command(command)
            if handled:
                break

            handled = self._handle_view_command(command)
            if handled:
                break

            handled = self._handle_edit_command(command)
            if handled:
                break

            if command == "t":  # triage
                self.triage_menu()
                break

    def _get_abint_num(self) -> Optional[int]:

        def preview_bug(fstr):
            bugnum, _, _ = fstr.partition(' ')
            try:
                bugnum = int(bugnum)
            except ValueError:
                return None
            return Bugzilla.get_bug_description(bugnum)

        abints = Bugzilla.get_formatted_abints()
        abrefresh = "Refresh AB-INT list from server"

        while True:
            abint_list = [abrefresh, *abints]

            abint_menu = TerminalMenu(abint_list, title="Bug",
                                      search_key=None,
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

        return bugnum

    def _get_bug_num(self) -> Optional[int]:
        while True:
            try:
                bugnum_str = input('Bug number:').strip()
            except EOFError:
                return None
            if not bugnum_str or bugnum_str.strip() == "q":
                return None

            if bugnum_str.isnumeric():
                return int(bugnum_str)

            logger.warning("Invalid issue: %s", bugnum_str)

    def _print_last_bugs(self):
        """Print last used bug numbers.

        Shows a list of recently used bug numbers to help with consistency.
        """
        # We only look for failures with unpublished new triages, this is kind
        # of a dumb solution but should be enough for a start.
        lastbugs = {triage.comment
                    for userinfo in self.userinfos.values()
                    for triage in userinfo.triages
                    if triage.status == swatbotrest.TriageStatus.BUG}
        if lastbugs:
            print("Last used bugs:")
            for lastbug in lastbugs:
                line = f"{lastbug}: {Bugzilla.get_bug_title(lastbug)}"
                print(textwrap.indent(line, " " * 4))
            print()

    def _prompt_bug_infos(self, build: swatbuild.Build, is_abint: bool):
        """Create new status of type BUG for a given failure.

        Args:
            build: The build to create a bug for
            is_abint: Whether to use the AB-INT list for bug selection

        Returns:
            A new Triage object or None if cancelled
        """
        if is_abint:
            bugnum = self._get_abint_num()
        else:
            self._print_last_bugs()
            bugnum = self._get_bug_num()

        if bugnum is None:
            return None

        print("Please set the comment content")
        bcomment = _format_bugzilla_comment(build)
        try:
            bcomment = click.edit(bcomment, require_save=False)
        except click.exceptions.ClickException as err:
            logger.warning("Got exception, aborting triage: %s", err)
            return None

        newstatus = userdata.Triage()
        newstatus.status = swatbotrest.TriageStatus.BUG
        newstatus.comment = str(bugnum)
        newstatus.extra['bugzilla-comment'] = bcomment
        return newstatus

    def _create_new_status(self, build: swatbuild.Build, command: str,
                           ) -> Optional[userdata.Triage]:
        """Create new status for a given failure.

        Args:
            build: The build to create a status for
            command: The menu command selected by the user

        Returns:
            A new Triage object or None if cancelled
        """
        newstatus = userdata.Triage()
        if command in ["a", "b"]:
            newstatus = self._prompt_bug_infos(build, command == "a")
        elif command == "c":
            if build.status != swatbuild.Status.CANCELLED:
                logging.error(
                    "Only cancelled builds can be triaged as cancelled")
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

    def _handle_triage_command(self, command: str) -> bool:
        build = self.builds[self.entry]
        userinfo = self.userinfos[build.id]

        if command in ["a", "b", "c", "m", "i", "o", "f", "d", "t"]:
            # Set new status
            newstatus = self._create_new_status(build, command)
            if newstatus:
                newstatus.failures = list(build.failures.keys())
                userinfo.triages = [newstatus]
                self.need_refresh = True
            return True

        if command in ["s", "copy status"]:
            menubuilds = self.builds
            if command == "s":
                menubuilds = _get_similar_builds(build, self.builds)

            entry = menubuilds.index(build)

            failure_menu = FailureMenu(menubuilds, self.userinfos)
            entries = failure_menu.show_multi(entry)
            if entries:
                targetbuilds = [menubuilds[e] for e in entries if e != entry]
                for tbuild in targetbuilds:
                    triages = _copy_triages_for(userinfo.triages, tbuild)
                    self.userinfos[tbuild.id].triages = triages
            return True

        if command == "r":  # Reset status
            userinfo.triages = []
            self.need_refresh = True
            return True

        return False

    def _handle_navigation_command(self, command: str) -> bool:
        if command == "q":  # Quit
            self.done = True
            return True

        if command == "n":  # Next
            self.entry += 1
            self.need_refresh = True
        elif command == "next pending failure":
            self.entry += 1
            while (self.entry < len(self.builds)
                   and self.userinfos[self.builds[self.entry].id].triages):
                self.entry += 1
            self.need_refresh = True
        elif command == "p":  # Previous
            if self.entry >= 1:
                self.entry -= 1
                self.need_refresh = True
            else:
                logger.warning("This is the first entry")
        elif command == "s":  # List
            utils.clear()
            self.need_refresh = True
            newentry = self.failure_menu.show(self.entry)
            if newentry is not None:
                self.entry = newentry
                self.need_refresh = True
        else:
            return False

        if self.entry >= len(self.builds):
            self.done = True

        return True

    def _handle_view_command(self, command: str) -> bool:
        # pylint: disable=too-many-return-statements

        build = self.builds[self.entry]

        if command == "u":  # Open autobuilder URL
            click.launch(build.autobuilder_url)
            return True
        if command == "w":  # Open swatbot URL
            click.launch(build.swat_url)
            return True
        if command == "g":  # Open stdio log
            build.get_first_failure().open_log_url()
            return True
        if command == "l":  # View stdio log
            failure = build.get_first_failure()
            self.need_refresh = logsview.LogView(failure, 'stdio').show_menu()
            return True
        if command == "x":  # Explore logs
            logsview.show_logs_menu(build)
            return True
        if command in ["v", "view git log"]:  # View git log
            base = build.git_info['base_commit']
            tip = build.git_info['tip_commit']
            if command == "v":
                options = ['--oneline']
            else:
                options = ["--patch", "--name-only"]
            self.need_refresh = pokyciarchive.show_log(tip, base, options)
            return True

        return False

    def _handle_edit_command(self, command: str) -> bool:
        build = self.builds[self.entry]
        userinfo = self.userinfos[build.id]

        if command == "e":  # Edit notes
            userinfo.set_notes(click.edit(userinfo.get_notes(),
                                          require_save=False))
            self.need_refresh = True
            return True

        return False


class FailureMenu:
    """Show menu allowing to select one or several failures.

    Provides an interface for selecting from a list of failures,
    with optional multi-selection support.
    """

    shown_fields = [
        swatbuild.Field.BUILD,
        swatbuild.Field.BRANCH,
        swatbuild.Field.TEST,
        swatbuild.Field.WORKER,
        swatbuild.Field.COMPLETED,
        swatbuild.Field.OWNER,
        swatbuild.Field.USER_STATUS,
    ]

    def __init__(self, builds: list[swatbuild.Build],
                 userinfos: userdata.UserInfos):
        self.builds = builds
        self.userinfos = userinfos

    def _format_build(self, build: swatbuild.Build,
                      cur_build: swatbuild.Build,
                      cur_fprint: logfingerprint.LogFingerprint):
        userinfo = self.userinfos[build.id]
        data = [build.format_field(userinfo, f, False)
                for f in self.shown_fields]

        failure = build.get_first_failure()
        build_fprint = logfingerprint.get_log_fingerprint(failure)
        similarity = ""
        if build is cur_build:
            similarity = "--- selected ---"
        elif cur_fprint.is_similar_to(build_fprint):
            sim = cur_fprint.get_similarity_score(build_fprint)
            similarity = f"similarity: {int(sim*100):3}%"
        data.insert(len(data) - 1, similarity)

        return data

    def show(self, entry: int, **kwargs):
        """Show the failure selection menu.

        Args:
            entry: The index of the initially selected entry
            **kwargs: Additional arguments for TerminalMenu

        Returns:
            The index of the selected entry or None if cancelled
        """
        build = self.builds[entry]
        failure = build.get_first_failure()
        fingerprint = logfingerprint.get_log_fingerprint(failure)

        def preview_failure(fstr):
            fnum = int(fstr.split()[0])
            build = [b for (i, b) in enumerate(self.builds) if b.id == fnum][0]
            return _get_infos(build, self.userinfos[fnum], width, 1)

        termsize = shutil.get_terminal_size((80, 20))
        width = termsize.columns - 2  # Borders

        entries = [self._format_build(b, build, fingerprint)
                   for b in self.builds]
        failures_menu = utils.tabulated_menu(entries, title="Failures",
                                             cursor_index=entry,
                                             preview_command=preview_failure,
                                             preview_size=.5,
                                             **kwargs)
        return failures_menu.show()

    def show_multi(self, entry: int, **kwargs):
        """Show the failure selection menu in multi-selection mode.

        Args:
            entry: The index of the initially selected entry
            **kwargs: Additional arguments for TerminalMenu

        Returns:
            List of selected entry indices or None if cancelled
        """
        return self.show(entry,
                         multi_select=True,
                         show_multi_select_hint=True,
                         multi_select_select_on_accept=False,
                         multi_select_empty_ok=True,
                         **kwargs)


def _format_bugzilla_comment(build: swatbuild.Build) -> Optional[str]:
    """Format a comment for a Bugzilla bug.

    Creates a standardized comment for a bug report containing test information
    and the log URL for the build failure.

    Args:
        build: The build to create a comment for

    Returns:
        Formatted comment string or None if log URL is not available
    """
    logurl = build.get_first_failure().get_log_url()
    if logurl:
        testinfos = " ".join([build.test, build.worker, build.branch,
                              f'completed at {build.completed}'])
        bcomment = "\n".join([testinfos, logurl])
    else:
        bcomment = None

    return bcomment


def _copy_triages_for(source_triages: list[userdata.Triage],
                      tbuild: swatbuild.Build) -> list[userdata.Triage]:
    """Copy triage statuses from one build to another.

    Copies all triage objects from the source to target build, adjusting
    failure IDs and Bugzilla comments as needed.

    Args:
        source_triages: List of triage objects to copy from
        tbuild: Target build to copy to

    Returns:
        List of new triage objects for the target build
    """
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

    return [copy_status(s, tbuild) for s in source_triages]


def _can_show_git_log(build: swatbuild.Build) -> bool:
    """Check if git log can be shown for a build.

    Args:
        build: The build to check

    Returns:
        True if git log can be shown, False otherwise
    """
    return (build.git_info is not None
            and 'base_commit' in build.git_info
            and 'tip_commit' in build.git_info)


def _get_similar_builds(build: swatbuild.Build, builds: list[swatbuild.Build]
                        ) -> list[swatbuild.Build]:
    """Find builds with similar log fingerprints.

    Identifies builds that have similar error patterns in their logs.

    Args:
        build: The reference build to compare against
        builds: List of builds to check for similarity

    Returns:
        List of similar builds
    """
    fprint = logfingerprint.get_log_fingerprint(build.get_first_failure())

    def is_similar(b):
        return fprint.is_similar_to_failure(b.get_first_failure(), 'stdio')

    return [b for b in builds if is_similar(b)]


def _get_infos(build: swatbuild.Build, userinfo: userdata.UserInfo,
               width: int, maxfailures: Optional[int] = None) -> str:
    """Format detailed information about a build for display.

    Creates a formatted string containing build details and log highlights.

    Args:
        build: The build to display information for
        userinfo: User information for the build
        width: Maximum width for formatting
        maxfailures: Maximum number of failures to include

    Returns:
        Formatted string with build information
    """
    buf = []
    buf.append(build.format_description(userinfo, width, maxfailures))
    buf.append('')

    failure = build.get_first_failure()
    log = swatlogs.Log(failure)
    highlights = log.get_highlights_text()
    maxhighlights = 5
    wrapped_highlights = [textwrap.indent(line, " " * 4)
                          for highlight in highlights[:maxhighlights]
                          for line in textwrap.wrap(highlight, width)
                          ]
    buf.append("Key log infos:")
    buf.append("\n".join(wrapped_highlights))

    return '\n'.join(buf)


def get_new_reviews() -> dict[tuple[swatbotrest.TriageStatus, Any],
                              list[userdata.Triage]]:
    """Get a list of new reviews waiting to be published on swatbot server.

    Collects all local triage information that hasn't been published yet,
    checking which failures are still pending on the server.

    Returns:
        Dictionary mapping (status, comment) tuples to lists of triage objects
    """

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
