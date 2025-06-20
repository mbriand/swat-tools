#!/usr/bin/env python3

"""Classes for representing builds and failures.

This module provides classes for representing and interacting with
build and failure information, sourced from the local SQLite database.
"""

import enum
import json
import logging
import sqlite3
import textwrap
import urllib
from datetime import datetime
from typing import Any, Iterable, Optional

import click
import requests
import tabulate

from . import buildbotrest
from . import bugzilla
from . import pokyciarchive
from . import swatbotrest
from . import userdata
from . import utils
from .webrequests import Session

logger = logging.getLogger(__name__)


class Status(enum.IntEnum):
    """The status of a failure.

    Represents the different status values that a build or failure can have.
    """

    WARNING = 1
    ERROR = 2
    CANCELLED = 6
    UNKNOWN = -1

    @staticmethod
    def from_int(status: int) -> 'Status':
        """Get Status instance from an integer status value.

        Args:
            status: Integer status value

        Returns:
            Status enum value or Status.UNKNOWN if value is invalid
        """
        try:
            return Status(status)
        except ValueError:
            return Status.UNKNOWN

    def __str__(self):
        return self.name.title()

    def _colorize(self, text: str):
        colors = {
            Status.WARNING: utils.Color.YELLOW,
            Status.ERROR: utils.Color.RED,
            Status.CANCELLED: utils.Color.PURPLE,
            Status.UNKNOWN: utils.Color.CYAN,
        }
        return utils.Color.colorize(text, colors[self])

    def as_colored_str(self):
        """Return status in a pretty colorized string.

        Returns:
            Colorized string representation of status
        """
        return self._colorize(self.name.title())

    def as_short_colored_str(self):
        """Return status in a short pretty colorized string.

        Returns:
            Short colorized string (first three characters) of status
        """
        return self._colorize(self.name[:3].title())


class Field(enum.StrEnum):
    """A field in failure info.

    Represents the different fields available in a build or failure record.
    """

    BUILD = 'Build'
    STATUS = 'Status'
    TEST = 'Test'
    OWNER = 'Owner'
    WORKER = 'Worker'
    COMPLETED = 'Completed'
    SWAT_URL = 'SWAT URL'
    AUTOBUILDER_URL = 'Autobuilder URL'
    FAILURES = 'Failures'
    BRANCH = 'Branch'
    USER_NOTES = 'Notes'
    USER_STATUS = 'New Triage'
    TRIAGE = 'Triage'
    PARENT_BUILD = 'Parent Build'


class Failure:
    """A Swatbot failure.

    Represents a build step failure with its status and log information.
    """

    # pylint: disable=too-many-instance-attributes

    def __init__(self, row: sqlite3.Row, build: 'Build'):
        self.id = row['failure_id']
        self.build = build
        self.stepnumber = int(row['step_number'])
        self.status = Status.from_int(row['status'])
        self.stepname = row['step_name']
        self.urls = json.loads(row['urls'])
        triage = row['remote_triage']
        self.triage = swatbotrest.TriageStatus(triage)
        self.triagenotes = row['remote_triage_notes']

    def get_log_url(self, logname: str = "stdio") -> Optional[str]:
        """Get the URL of a given log webpage.

        Args:
            logname: The name of the log to get URL for (default: "stdio")

        Returns:
            URL to the log webpage or None if not found
        """
        if logname not in self.urls:
            return None
        return self.urls[logname]

    def open_log_url(self, logname: str = "stdio"):
        """Open log URL in default browser.

        Args:
            logname: The name of the log to open (default: "stdio")
        """
        logurl = self.get_log_url()
        if logurl:
            click.launch(logurl)
        else:
            logger.error("Failed to find %s log", logname)

    def get_log_data(self, logname: str = "stdio") -> Optional[dict[str, Any]]:
        """Get the metadata of a log file.

        Args:
            logname: The name of the log to get URL for (default: "stdio")

        Returns:
            Dictionary containing log metadata or None if request fails
        """
        rest_url = self.build.rest_api_url()
        return buildbotrest.get_log_data(rest_url, self.build.id,
                                         self.stepnumber, logname)

    def get_log(self, logname: str) -> Optional[str]:
        """Get content of a given log file.

        Args:
            logname: The name of the log to retrieve

        Returns:
            Content of the log file or None if retrieval fails
        """
        rest_url = self.build.rest_api_url()
        logdata = buildbotrest.get_log_data(rest_url, self.build.id,
                                            self.stepnumber, logname)
        if not logdata:
            logging.error("Failed to find log")
            return None

        url = f"{rest_url}/logs/{logdata['logid']}/raw"
        try:
            data = Session().get(url, True, -1)
        except requests.exceptions.ConnectionError:
            logger.warning("Failed to download stdio log")
            return None

        return data

    def get_triage_with_notes(self) -> str:
        """Get triage description string, including notes.

        Returns:
            Formatted triage status with notes
        """
        notes = self.triagenotes
        if self.triage == swatbotrest.TriageStatus.BUG:
            bugid = bugzilla.Bugzilla.get_bug_id_from_url(notes)
            if bugid:
                notes = str(bugid)

        if notes:
            return f"{self.triage}: {notes}"

        return str(self.triage)

    def __str__(self):
        return (f"Failure {self.id}: "
                f"{self.status} on step {self.stepnumber} "
                f"of build {self.build}, {self.stepname}"
                )


class Build:
    """A Swatbot build.

    Represents a build with its failures and provides filtering and formatting.
    Initialized from SQLite database rows containing build, collection, and
    failure data.
    """

    # pylint: disable=too-many-instance-attributes

    def __init__(self, sql_rows: list[sqlite3.Row]):
        """Initialize a Build from database rows.

        Args:
            sql_rows: List of SQLite Row objects containing build, collection,
                      and failure data joined together.
        """
        collection_id = sql_rows[0]['collection_id']
        swat_url = f"{swatbotrest.BASE_URL}/collection/{collection_id}/"

        self.id = sql_rows[0]['buildbot_build_id']
        self.status = Status.from_int(sql_rows[0]['status'])
        self.test = sql_rows[0]['test']
        self.worker = sql_rows[0]['worker']
        self.completed = datetime.fromisoformat(sql_rows[0]['completed'])
        self.swat_url = swat_url
        self.autobuilder_url = sql_rows[0]['ab_url']
        self.owner = sql_rows[0]['owner']
        self.branch = sql_rows[0]['branch']
        self.yp_build_revision = sql_rows[0]['yp_build_revision']

        self.parent_builder_name = None
        self.parent_builder = self.parent_build_number = None
        self.parent_build = ""

        self.collection_build_id = sql_rows[0]['collection_build_id']
        if self.collection_build_id != self.id:
            self.parent_builder_name = sql_rows[0]["target_name"]
            self.parent_builder = sql_rows[0]['parent_builder']
            self.parent_build_number = sql_rows[0]['parent_build_number']
            ab = buildbotrest.autobuilder_short_name(self.autobuilder_url)
            self.parent_build = \
                f'{ab}/{self.parent_builder_name}/{self.parent_build_number}'

        self._git_info: Optional[dict[str, Any]] = None

        self.failures = {row['failure_id']: Failure(row, self)
                         for row in sql_rows}

    def _get_git_tag(self) -> Optional[str]:
        if self.parent_builder_name and self.parent_build_number:
            name = self.parent_builder_name
            number = self.parent_build_number
        elif self.test in ['a-quick', 'a-full']:
            name = self.test
            _, _, number = self.autobuilder_url.rpartition('/')
        else:
            name = number = None

        if name and number:
            aburl = urllib.parse.urlparse(self.autobuilder_url)
            host = aburl.netloc.replace(':', '_')

            return f"{host}{aburl.path}{name}-{number}"

        return None

    @property
    def git_info(self) -> dict[str, Any]:
        """Get information about built git branch.

        Returns:
            Dictionary containing git information about the build
        """
        if self._git_info is None:
            gittag = self._get_git_tag()

            if gittag:
                basebranch = self.branch.split('/')[-1]
                if basebranch.endswith('-next'):
                    basebranch = basebranch[:-len('-next')]

                limit = 100
                git_info = pokyciarchive.get_build_commits(gittag, basebranch,
                                                           limit)

                if git_info is not None:
                    self._git_info = git_info

                    commitcount = len(self._git_info['commits'])
                    plus = '+' if commitcount == limit else ''
                    desc = f"{commitcount}{plus} commits ahead of {basebranch}"
                    self._git_info['description'] = desc

            if self._git_info is None and self.yp_build_revision:
                self._git_info = {'description':
                                  f"On commit {self.yp_build_revision}"}

            if self._git_info is None:
                self._git_info = {'description': "On unknown revision"}

        return self._git_info

    def _completed_match_filters(self, filters: dict[str, Any]) -> bool:
        if filters['completed-after'] and self.completed:
            if self.completed < filters['completed-after']:
                return False

        if filters['completed-before'] and self.completed:
            if self.completed > filters['completed-before']:
                return False

        return True

    def _userinfo_match_filters(self, filters: dict[str, Any],
                                userinfo: userdata.UserInfo
                                ) -> bool:
        if filters['with-notes'] is not None:
            if filters['with-notes'] ^ bool(userinfo.notes):
                return False

        if filters['with-new-status'] is not None:
            userstatus = userinfo.triages
            if filters['with-new-status'] ^ bool(userstatus):
                return False

        return True

    def _triage_match_filters(self, filters: dict[str, Any]) -> bool:
        if filters['triage']:
            triages = {f.triage for f in self.failures.values()}
            if triages.isdisjoint(filters['triage']):
                return False

        return True

    def _logs_match_filters(self, filters: dict[str, Any]) -> bool:
        if not filters['log-matches']:
            return True

        logdata = self.get_first_failure().get_log('stdio')
        if not logdata:
            return False

        for pat in filters['log-matches']:
            for line in logdata.splitlines():
                if pat.match(line):
                    return True

        return False

    def match_filters(self, filters: dict[str, Any],
                      userinfo: userdata.UserInfo
                      ) -> bool:
        """Check if this build matches given filters.

        Args:
            filters: Dictionary of filters to apply
            userinfo: User information for this build

        Returns:
            True if the build matches all filters, False otherwise
        """
        # pylint: disable=too-many-return-statements

        def simple_match(field: Field):
            filtr = filters[field.name.lower()]
            return not filtr or self.get(field) in filtr

        def regex_match(field: Field):
            value = str(self.get(field))

            select_re = filters.get(field.name.lower(), [])
            matches = [True for r in select_re if r.match(value)]
            if select_re and not matches:
                return False

            ignore_re = filters.get(f'ignore-{field.name.lower()}', [])
            matches = [True for r in ignore_re if r.match(value)]
            if ignore_re and matches:
                return False

            return True

        simple_filters = [Field.STATUS]
        if not all(simple_match(field) for field in simple_filters):
            return False

        regex_filters = [Field.BUILD, Field.OWNER, Field.TEST,
                         Field.PARENT_BUILD]
        if not all(regex_match(field) for field in regex_filters):
            return False

        if not self._completed_match_filters(filters):
            return False

        if not self._userinfo_match_filters(filters, userinfo):
            return False

        if not self._triage_match_filters(filters):
            return False

        if not self._logs_match_filters(filters):
            return False

        return True

    def get(self, field: Field):
        """Get data from the given field.

        Args:
            field: The field to retrieve data for

        Returns:
            The value of the requested field

        Raises:
            SwattoolException: If the field is invalid
        """
        if field == Field.BUILD:
            return self.id
        if field == Field.COMPLETED:
            return self.completed.astimezone().isoformat(timespec='minutes')
        if field.name.lower() in self.__dict__:
            return self.__dict__[field.name.lower()]

        raise utils.SwattoolException(f"Invalid field: {field}")

    def format_field(self, userinfo: userdata.UserInfo, field: Field,
                     multiline: bool = True) -> str:
        """Get formatted failure data.

        Args:
            userinfo: User information for this build
            field: The field to format
            multiline: Whether to allow multiple lines in output

        Returns:
            Formatted string representation of the field
        """

        def format_multi(data):
            return "\n".join(data) if multiline or len(data) <= 1 else data[0]

        if field == Field.STATUS:
            return self.get(Field.STATUS).as_short_colored_str()
        if field == Field.FAILURES:
            return format_multi([f.stepname for f in self.get(field).values()])
        if field == Field.TRIAGE:
            return format_multi([str(f.get_triage_with_notes())
                                 for f in self.failures.values()])
        if field == Field.USER_STATUS:
            statuses = [str(triage) for fail in self.failures.values()
                        if (triage := userinfo.get_failure_triage(fail.id))]
            return format_multi(statuses)
        if field == Field.USER_NOTES:
            notes = userinfo.get_notes()
            return textwrap.shorten(notes, 80)
        return str(self.get(field))

    def get_first_failure(self) -> Failure:
        """Get the first failure of the build.

        Returns first failure with matching status, or first failure by ID
        if none match the build status.

        Returns:
            Failure object
        """
        for _, failure in sorted(self.failures.items()):
            if failure.status == self.status:
                return failure
        return self.failures[min(self.failures)]

    def get_sort_tuple(self, keys: Iterable[Field],
                       userinfos: Optional[dict[int, dict[Field, Any]]] = None
                       ) -> tuple:
        """Get selected fields in sortable fashion.

        Creates a tuple of field values for sorting builds.

        Args:
            keys: Fields to include in the sort tuple
            userinfos: Optional dictionary of user information by build ID

        Returns:
            Tuple of field values
        """
        if not userinfos:
            userinfos = {}

        userinfo = userinfos.get(self.id)

        def get_field(field):
            # pylint: disable=too-many-return-statements
            if field == Field.FAILURES:
                return sorted(fail.stepname
                              for fail in self.failures.values())
            if field == Field.OWNER:
                return str(self.owner)
            if field == Field.TRIAGE:
                return self.get_first_failure().get_triage_with_notes()
            if field == Field.USER_STATUS:
                if userinfo and userinfo.triages:
                    return userinfo.triages[0]['status']
                return swatbotrest.TriageStatus.PENDING
            if field == Field.USER_NOTES:
                return "\n".join(userinfo.notes) if userinfo else ""
            return self.get(field)

        return tuple(get_field(k) for k in keys)

    def _format_parent_description(self) -> str:
        pbldr, pnmbr = self.parent_builder, self.parent_build_number
        ab_url = buildbotrest.autobuilder_base_url(self.autobuilder_url)
        parent_url = f"{ab_url}/#/builders/{pbldr}/builds/{pnmbr}"
        return f"{parent_url} ({self.parent_builder_name})"

    def format_description(self, userinfo: userdata.UserInfo,
                           maxwidth: int, maxfailures: Optional[int] = None
                           ) -> str:
        """Get info on one given failure in a pretty way.

        Args:
            userinfo: User information for this build
            maxwidth: Maximum width for formatting
            maxfailures: Maximum number of failures to include

        Returns:
            Formatted description of the build
        """
        def format_field(field):
            if field == Field.STATUS:
                if self.status == Status.ERROR:
                    return str(self.status)
                return self.status.as_colored_str()
            if field == Field.BRANCH:
                _, _, branchname = self.branch.rpartition('/')
                if branchname not in ["master", "master-next"]:
                    return utils.Color.colorize(self.branch,
                                                utils.Color.YELLOW)
                return self.branch
            return self.get(field)

        simple_fields = [
            Field.BUILD,
            Field.STATUS,
            Field.TEST,
            Field.OWNER,
            Field.BRANCH,
            Field.WORKER,
            Field.COMPLETED,
            Field.SWAT_URL,
            Field.AUTOBUILDER_URL,
        ]
        table = [[k, format_field(k)] for k in simple_fields]

        if self.parent_build_number:
            table.append(["Parent", self._format_parent_description()])

        if 'description' in self.git_info:
            table.append(["Git info", self.git_info['description']])

        for i, (failureid, failure) in enumerate(self.failures.items()):
            # Create strings for all failures and the attributed new status (if
            # one was set).
            triage = userinfo.get_failure_triage(failureid)

            if (maxfailures is not None and i >= maxfailures):
                if maxfailures != 0:
                    removed = len(self.failures) - i
                    table.append(["", f"... {removed} more failures ...", ""])
                break

            table.append([Field.FAILURES if i == 0 else "",
                          failure.stepname,
                          triage.format_description() if triage else ""])

        desc = tabulate.tabulate(table, tablefmt="plain")

        if userinfo.notes:
            wrapped = userinfo.get_wrapped_notes(maxwidth, " " * 4)
            desc += f"\n\n{Field.USER_NOTES}:\n{wrapped}"

        return desc

    def format_short_description(self) -> str:
        """Get condensed info on one given failure in a pretty way.

        Returns:
            Short formatted description of the build
        """
        return f"Build {self.id} ({self.branch}): " \
               f"{self.test} on {self.worker}, " \
               f"{str(self.status).lower()} at {self.completed}"

    def format_tiny_description(self) -> str:
        """Get very short info on one given failure.

        Returns:
            Minimal formatted description of the build
        """
        return f"{self.id} {self.test} ({self.branch})"

    def rest_api_url(self) -> str:
        """Get the REST API URL prefix for this build.

        Returns:
            REST API URL prefix for this build
        """
        base_url = buildbotrest.autobuilder_base_url(self.autobuilder_url)
        return buildbotrest.rest_api_url(base_url)

    def open_urls(self, urlopens: set[str]) -> None:
        """Open requested URLs in default browser.

        Args:
            urlopens: Set of URL types to open
        """
        if 'autobuilder' in urlopens:
            click.launch(self.autobuilder_url)
        if 'swatbot' in urlopens:
            click.launch(self.swat_url)
        if 'stdio' in urlopens:
            self.get_first_failure().open_log_url()
