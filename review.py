#!/usr/bin/env python3

import click
import logging
import tabulate
import swatbot
import bugzilla
import utils
import webrequests
from typing import Any, Optional

logger = logging.getLogger(__name__)


def prompt_bug_infos(info: dict[swatbot.Field, Any],
                     failures: dict[int, dict[str, Any]], is_abint: bool):
    abints = bugzilla.get_abints()
    if is_abint:
        print(tabulate.tabulate(abints.items()))

    first_failure = min(failures)
    while True:
        bugnum = input('Bug number:').strip()
        if bugnum.isnumeric() and (int(bugnum) in abints or not is_abint):
            print("Please set the comment content")
            if 'stdio' in failures[first_failure]['urls']:
                testmachine = " ".join([info[swatbot.Field.TEST],
                                        info[swatbot.Field.WORKER]])
                log = failures[first_failure]['urls']['stdio']
                bcomment = utils.edit_text("\n".join([testmachine, log]))
            else:
                bcomment = utils.edit_text(None)
            newstatus = {'status': swatbot.TriageStatus.BUG,
                         'comment': int(bugnum),
                         'bugzilla-comment': bcomment,
                         }
            return newstatus
        elif bugnum.strip() == "q":
            return None
        else:
            logging.warning("Invalid issue: %s", bugnum)


def review_menu(infos: list[dict[swatbot.Field, Any]],
                userinfos: dict[int, dict[swatbot.Field, Any]],
                entry: int) -> Optional[int]:
    print("a ab-int")
    print("b bug opened")
    print("m mail sent")
    if utils.MAILNAME:
        print(f"i mail sent by {utils.MAILNAME}")
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
            newnotes = utils.edit_text(userinfo.get(swatbot.Field.USER_NOTES))
            userinfo[swatbot.Field.USER_NOTES] = newnotes
        elif line.strip() in ["a", "b"]:
            newstatus = prompt_bug_infos(info, failures, line.strip() == "a")
        elif line.strip() == "m":
            newstatus = {'status': swatbot.TriageStatus.MAIL_SENT,
                         'comment': input('Comment:').strip(),
                         }
        elif line.strip() == "i" and utils.MAILNAME:
            newstatus = {'status': swatbot.TriageStatus.MAIL_SENT,
                         'comment': f"Mail sent by {utils.MAILNAME}",
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
        newstatus['failures'] = failures
        userinfo[swatbot.Field.USER_STATUS] = [newstatus]

    if entry >= len(infos):
        return None

    return entry


def get_new_reviews() -> dict[tuple[swatbot.TriageStatus, Any], list[dict]]:
    userinfos = swatbot.get_user_infos()

    reviews: dict[tuple[swatbot.TriageStatus, Any], list[dict]] = {}
    with click.progressbar(userinfos.items()) as userinfos_progress:
        for buildid, userinfo in userinfos_progress:
            if swatbot.Field.USER_STATUS not in userinfo:
                continue

            userstatuses = userinfo.get(swatbot.Field.USER_STATUS, [])
            for userstatus in userstatuses:
                status = userstatus.get('status')
                comment = userstatus.get('comment')
                if not status or not comment:
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