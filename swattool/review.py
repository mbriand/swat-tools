#!/usr/bin/env python3

"""Swatbot review functions."""

import click
import logging
import tabulate
from simple_term_menu import TerminalMenu
from typing import Any, Optional

from . import swatbot
from . import bugzilla
from . import utils
from . import webrequests

logger = logging.getLogger(__name__)


def _prompt_bug_infos(info: dict[swatbot.Field, Any],
                      is_abint: bool):
    """Create new status of type BUG for a given failure."""
    failures = info[swatbot.Field.FAILURES]
    first_failure = min(failures)
    if is_abint:
        abints = bugzilla.get_abints()
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
            elif bugnum_str.strip() == "q":
                return None
            else:
                logger.warning("Invalid issue: %s", bugnum_str)

    print("Please set the comment content")
    if 'stdio' in failures[first_failure]['urls']:
        testmachine = " ".join([info[swatbot.Field.TEST],
                                info[swatbot.Field.WORKER]])
        log = failures[first_failure]['urls']['stdio']
        bcomment = click.edit("\n".join([testmachine, log]),
                              require_save=False)
    else:
        bcomment = click.edit(None, require_save=False)

    newstatus = {'status': swatbot.TriageStatus.BUG,
                 'comment': bugnum,
                 'bugzilla-comment': bcomment,
                 }
    return newstatus


def _create_new_status(info: dict[swatbot.Field, Any],
                       userinfo: dict[swatbot.Field, Any],
                       command: str) -> dict:
    """Create new status for a given failure."""
    if command in ["a", "b"]:
        newstatus = _prompt_bug_infos(info, command == "a")
    elif command == "m":
        newstatus = {'status': swatbot.TriageStatus.MAIL_SENT,
                     'comment': input('Comment:').strip(),
                     }
    elif command == "i" and utils.MAILNAME:
        newstatus = {'status': swatbot.TriageStatus.MAIL_SENT,
                     'comment': f"Mail sent by {utils.MAILNAME}",
                     }
    elif command == "o":
        newstatus = {'status': swatbot.TriageStatus.OTHER,
                     'comment': input('Comment:').strip(),
                     }
    elif command == "f":
        newstatus = {'status': swatbot.TriageStatus.OTHER,
                     'comment': 'Fixed',
                     }
    elif command == "t":
        newstatus = {'status': swatbot.TriageStatus.NOT_FOR_SWAT,
                     'comment': input('Comment:').strip(),
                     }

    return newstatus


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
    None,
    "[n] next",
    "[p] previous",
    "[l] list all failures",
    "[q] quit",
]


valid_commands = [c for c in _commands if c != ""]


def review_menu(infos: list[dict[swatbot.Field, Any]],
                userinfos: dict[int, dict[swatbot.Field, Any]],
                entry: int,
                statusbar: str) -> Optional[int]:
    """Allow a user to interactively triage a failure."""
    default_action = "n"
    default_index = [c[1] if c and len(c) > 1 else None
                     for c in valid_commands].index(default_action)
    action_menu = TerminalMenu(valid_commands, title="Action",
                               cursor_index=default_index,
                               status_bar=statusbar,
                               raise_error_on_interrupt=True)

    info = infos[entry]
    userinfo = userinfos.setdefault(info[swatbot.Field.BUILD], {})
    failures = info[swatbot.Field.FAILURES]
    newstatus: Optional[dict] = None

    while True:
        try:
            command_index = action_menu.show()
            if command_index is None:
                return None
            command = valid_commands[command_index][1]
        except EOFError:
            return None

        if command == "q":  # Quit
            return None
        elif command == "n":  # Next
            entry += 1
        elif command == "p":  # Previous
            if entry >= 1:
                entry -= 1
            else:
                logger.warning("This is the first entry")
                continue
        elif command == "l":  # List
            entry = _list_failures_menu(infos, userinfos, entry)
        elif command == "e":  # Edit notes
            newnotes = click.edit(userinfo.get(swatbot.Field.USER_NOTES),
                                  require_save=False)
            userinfo[swatbot.Field.USER_NOTES] = newnotes
        elif command == "u":  # Open autobuilder URL
            click.launch(info[swatbot.Field.AUTOBUILDER_URL])
        elif command == "w":  # Open swatbot URL
            click.launch(info[swatbot.Field.SWAT_URL])
        elif command == "g":  # Open stdio log
            first_failure = min(failures)
            if 'stdio' in failures[first_failure]['urls']:
                click.launch(failures[first_failure]['urls']['stdio'])
            else:
                logger.warning("Failed to find stdio log")
        elif command in ["a", "b", "m", "i", "o", "f", "t"]:  # Set new status
            newstatus = _create_new_status(info, userinfo, command)
        elif command == "r":  # Reset status
            userinfo[swatbot.Field.USER_STATUS] = []
        else:
            continue
        break

    if newstatus:
        newstatus['failures'] = failures
        userinfo[swatbot.Field.USER_STATUS] = [newstatus]

    if entry >= len(infos):
        return None

    return entry


def _list_failures_menu(infos: list[dict[swatbot.Field, Any]],
                        userinfos: dict[int, dict[swatbot.Field, Any]],
                        entry: int) -> int:
    """Allow the user to select the failure to review in a menu."""
    def preview_failure(fstr):
        fnum = int(fstr.split()[0])
        idx = [i for (i, info) in enumerate(infos)
               if info[swatbot.Field.BUILD] == fnum][0]
        return swatbot.get_failure_description(infos[idx], userinfos[fnum])

    shown_fields = [
        swatbot.Field.BUILD,
        swatbot.Field.TEST,
        swatbot.Field.OWNER,
    ]
    entries = [[info[f] for f in shown_fields] for info in infos]
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


def get_new_reviews() -> dict[tuple[swatbot.TriageStatus, Any], list[dict]]:
    """Get a list of new reviews waiting to be published on swatbot server."""
    userinfos = swatbot.get_user_infos()

    logger.info("Loading pending reviews...")
    reviews: dict[tuple[swatbot.TriageStatus, Any], list[dict]] = {}
    with click.progressbar(userinfos.items()) as userinfos_progress:
        for buildid, userinfo in userinfos_progress:
            if swatbot.Field.USER_STATUS not in userinfo:
                continue

            userstatuses = userinfo.get(swatbot.Field.USER_STATUS, [])
            for userstatus in userstatuses:
                status = userstatus.get('status')
                comment = userstatus.get('comment')
                if not status:
                    continue

                if not comment:
                    logger.warning("Review for failure %s is missing comment: "
                                   "skipping", buildid)
                    continue

                def is_pending(failure_id):
                    refresh = refresh = webrequests.RefreshPolicy.FORCE
                    failure = swatbot.get_stepfailure(failure_id,
                                                      refresh=refresh)
                    return failure['attributes']['triage'] == 0

                # Make sure failures are still pending
                userstatus['failures'] = {k: v for k, v
                                          in userstatus['failures'].items()
                                          if is_pending(k)}

                reviews.setdefault((status, comment), []).append(userstatus)

            # Cleaning old reviews
            if userstatuses and not any([s['failures'] for s in userstatuses]):
                del userinfo[swatbot.Field.USER_STATUS]

    swatbot.save_user_infos(userinfos)

    return reviews
