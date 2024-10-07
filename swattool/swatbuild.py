#!/usr/bin/env python3

"""Interraction with the swatbot Django server."""

import enum
import logging
import shutil
import textwrap
from datetime import datetime
from typing import Any, Iterable, Optional

import tabulate

from . import bugzilla
from . import logs
from . import swatbot
from . import utils

logger = logging.getLogger(__name__)


class Field(enum.StrEnum):
    """A filed in failure info."""

    BUILD = 'Build'
    STATUS = 'Status'
    TEST = 'Test'
    OWNER = 'Owner'
    WORKER = 'Worker'
    COMPLETED = 'Completed'
    SWAT_URL = 'SWAT URL'
    AUTOBUILDER_URL = 'Autobuilder URL'
    FAILURES = 'Failures'
    USER_NOTES = 'Notes'
    USER_STATUS = 'Triage'


class Failure:
    def __init__(self, failure_id: int, failure_data: dict, build: 'Build'):
        self.id = failure_id
        self.build = build
        self.stepnumber = int(failure_data['attributes']['stepnumber'])
        self.stepname = failure_data['attributes']['stepname']
        self.urls = {u.split()[0].rsplit('/')[-1]: u
                     for u in failure_data['attributes']['urls'].split()}

    def get_log_url(self, logname: str = "stdio") -> Optional[str]:
        if logname not in self.urls:
            return None
        return self.urls[logname]

    def get_log_raw_url(self, logname: str = "stdio") -> Optional[str]:
        return logs.get_log_raw_url(self.build.id, self.stepnumber, logname)


class Build:
    def __init__(self, buildid: int,
                 pending_failures: dict[int, dict]):
        build = swatbot.get_build(buildid)
        attributes = build['attributes']
        relationships = build['relationships']
        collectionid = relationships['buildcollection']['data']['id']
        collection = swatbot.get_build_collection(collectionid)

        swat_url = f"{swatbot.BASE_URL}/collection/{collection['id']}/"

        self.id = attributes['buildid']
        self.status = swatbot.Status.from_int(attributes['status'])
        self.test = attributes['targetname']
        self.worker = attributes['workername']
        self.completed = attributes['completed']
        self.swat_url = swat_url
        self.autobuilder_url = attributes['url']
        self.owner = collection['attributes']['owner']

        self.failures = {fid: Failure(fid, fdata, self)
                         for fid, fdata in pending_failures.items()}

    def match_filters(self, filters: dict[str, Any],
                      userinfo: dict[Field, Any] = {}
                      ) -> bool:
        if filters['build'] and self.id not in filters['build']:
            return False

        if filters['owner'] and self.owner not in filters['owner']:
            return False

        matches = [True for r in filters['test'] if r.match(self.test)]
        if filters['test'] and not matches:
            return False

        matches = [True for r in filters['ignore-test']
                   if r.match(self.test)]
        if filters['ignore-test'] and matches:
            return False

        if filters['status'] and self.status not in filters['status']:
            return False

        if filters['completed-after'] and self.completed:
            completed = datetime.fromisoformat(self.completed)
            if completed < filters['completed-after']:
                return False

        if filters['completed-before'] and self.completed:
            completed = datetime.fromisoformat(self.completed)
            if completed > filters['completed-before']:
                return False

        if filters['with-notes'] is not None:
            if filters['with-notes'] ^ bool(userinfo.get(Field.USER_NOTES)):
                return False

        if filters['with-new-status'] is not None:
            userstatus = userinfo.get(Field.USER_STATUS)
            if filters['with-new-status'] ^ bool(userstatus):
                return False

        return True

    def get(self, field: Field):
        if field == Field.BUILD:
            return self.id
        if field == Field.STATUS:
            return self.status
        if field == Field.TEST:
            return self.test
        if field == Field.WORKER:
            return self.worker
        if field == Field.COMPLETED:
            return self.completed
        if field == Field.SWAT_URL:
            return self.swat_url
        if field == Field.AUTOBUILDER_URL:
            return self.autobuilder_url
        if field == Field.OWNER:
            return self.owner
        if field == Field.FAILURES:
            return self.failures

        raise utils.SwattoolException(f"Invalid field: {field}")

    def get_first_failure(self) -> Failure:
        first_failure = min(self.failures)
        return self.failures[first_failure]

    def get_sort_tuple(self, keys: Iterable[Field],
                       userinfos: dict[int, dict[Field, Any]] = {}
                       ) -> tuple:
        def get_field(field):
            if field == Field.FAILURES:
                return sorted(fail['stepname']
                              for fail in self.failures.values())
            if field == Field.USER_STATUS:
                triage = userinfos[self.id].get(field)
                if triage:
                    return triage[0]['status']
                return swatbot.TriageStatus.PENDING
            if field in [Field.USER_STATUS, Field.USER_NOTES]:
                if field in userinfos.get(self.id, {}):
                    return userinfos[self.id][field]
                return ""
            return self.get(field)

        return tuple(get_field(k) for k in keys)

    def format_description(self, userinfo: dict[Field, Any] = {}) -> str:
        """Get info on one given failure in a pretty way."""
        abints = bugzilla.get_abints()

        simple_fields = [
            Field.BUILD,
            Field.STATUS,
            Field.TEST,
            Field.OWNER,
            Field.WORKER,
            Field.COMPLETED,
            Field.SWAT_URL,
            Field.AUTOBUILDER_URL,
        ]
        table = [[k, self.get(k)] for k in simple_fields]

        statuses = userinfo.get(Field.USER_STATUS, [])
        for i, (failureid, failure) in enumerate(self.failures.items()):
            status_str = ""

            # Create strings for all failures and the attributed new status (if
            # one was set).
            for status in statuses:
                if failureid in status['failures']:
                    statusfrags = []

                    statusname = status['status'].name.title()
                    statusfrags.append(f"{statusname}: {status['comment']}")

                    if status['status'] == swatbot.TriageStatus.BUG:
                        bugid = int(status['comment'])
                        if bugid in abints:
                            bugtitle = abints[bugid]
                            statusfrags.append(f", {bugtitle}")

                    if status.get('bugzilla-comment'):
                        statusfrags.append("\n")
                        bcomlines = status['bugzilla-comment'].split('\n')
                        bcom = [textwrap.fill(line) for line in bcomlines]
                        statusfrags.append("\n".join(bcom))

                    status_str += "".join(statusfrags)

                    break
            table.append([Field.FAILURES if i == 0 else "",
                          failure.stepname, status_str])

        desc = tabulate.tabulate(table, tablefmt="plain")

        usernotes = userinfo.get(Field.USER_NOTES)
        if usernotes:
            # Reserve chars for spacing.
            reserved = 8
            termwidth = shutil.get_terminal_size((80, 20)).columns
            width = termwidth - reserved
            wrapped_lns = [textwrap.indent(textwrap.fill(line, width), " " * 4)
                           for line in usernotes.splitlines()]
            wrapped = "\n".join(wrapped_lns)
            desc += f"\n\n{Field.USER_NOTES}:\n{wrapped}"

        return desc
