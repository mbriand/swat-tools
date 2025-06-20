#!/usr/bin/env python3

"""Interaction with the swatbot Django server.

This module provides classes for representing and interacting with
build and failure information from the swatbot system.
"""

import enum
import logging
from datetime import datetime
import textwrap
from typing import Any, Iterable, Optional
import urllib

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


class Failure:
    """A Swatbot failure.

    Represents a build step failure with its status and log information.
    """

    # pylint: disable=too-many-instance-attributes

    def __init__(self, failure_id: int, failure_data: dict, build: 'Build'):
        self.id = failure_id
        self.build = build
        self.stepnumber = int(failure_data['attributes']['stepnumber'])
        self.status = Status.from_int(failure_data['attributes']['status'])
        self.stepname = failure_data['attributes']['stepname']
        self.urls = {u.split()[0].rsplit('/')[-1]: u
                     for u in failure_data['attributes']['urls'].split()}
        triage = failure_data['attributes']['triage']
        self.triage = swatbotrest.TriageStatus(triage)
        self.triagenotes = failure_data['attributes']['triagenotes']

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

    def get_log_raw_url(self, logname: str = "stdio"
                        ) -> Optional[str]:
        """Get the URL of a raw log file.

        Args:
            logname: The name of the log to get URL for (default: "stdio")

        Returns:
            URL to the raw log file or None if not found
        """
        rest_url = self.build.rest_api_url()
        return buildbotrest.get_log_raw_url(rest_url, self.build.id,
                                            self.stepnumber, logname)

    def get_log(self, logname: str) -> Optional[str]:
        """Get content of a given log file.

        Args:
            logname: The name of the log to retrieve

        Returns:
            Content of the log file or None if retrieval fails
        """
        logurl = self.get_log_raw_url(logname)
        if not logurl:
            logging.error("Failed to find log")
            return None

        try:
            logdata = Session().get(logurl)
        except requests.exceptions.ConnectionError:
            logger.warning("Failed to download stdio log")
            return None

        return logdata

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
    """

    # pylint: disable=too-many-instance-attributes

    def __init__(self, buildid: int,
                 failures: dict[int, dict]):
        build = swatbotrest.get_build(buildid)
        attributes = build['attributes']
        relationships = build['relationships']
        collectionid = relationships['buildcollection']['data']['id']
        collection = swatbotrest.get_build_collection(collectionid)

        swat_url = f"{swatbotrest.BASE_URL}/collection/{collection['id']}/"

        self.id = attributes['buildid']
        self.status = Status.from_int(attributes['status'])
        self.test = attributes['targetname']
        self.worker = attributes['workername']
        self.completed = datetime.fromisoformat(attributes['completed'])
        self.swat_url = swat_url
        self.autobuilder_url = attributes['url']
        self.owner = collection['attributes']['owner']
        self.branch = collection['attributes']['branch']

        self.parent_builder_name = None
        self.parent_builder = self.parent_build_number = None

        if collection['attributes']['buildid'] != self.id:
            pbid = collection['attributes']['buildid']
            buildboturl = Build._rest_api_url(self.autobuilder_url)
            parent_build = buildbotrest.get_build(buildboturl, pbid)
            self.parent_builder_name = collection['attributes']["targetname"]
            self.parent_builder = self.parent_build_number = None
            if parent_build:
                self.parent_builder = parent_build['builds'][0]['builderid']
                self.parent_build_number = parent_build['builds'][0]['number']

        self._git_info: Optional[dict[str, Any]] = None

        self.failures = {fid: Failure(fid, fdata, self)
                         for fid, fdata in failures.items()}

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

            if self._git_info is None:
                buildboturl = Build._rest_api_url(self.autobuilder_url)
                buildbot_build = buildbotrest.get_build(buildboturl, self.id)
                if buildbot_build:
                    try:
                        properties = buildbot_build['builds'][0]['properties']
                        rev = properties['yp_build_revision'][0]

                        self._git_info = {'description': f"On commit {rev}"}
                    except (KeyError, IndexError):
                        pass

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

        regex_filters = [Field.BUILD, Field.OWNER, Field.TEST]
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
        ab_url = Build._autobuilder_base_url(self.autobuilder_url)
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

    @staticmethod
    def _autobuilder_base_url(autobuilder_url) -> str:
        url, _, _ = autobuilder_url.partition('/#/builders')
        return url

    @staticmethod
    def _rest_api_url(autobuilder_url) -> str:
        base_url = Build._autobuilder_base_url(autobuilder_url)
        return buildbotrest.rest_api_url(base_url)

    def rest_api_url(self) -> str:
        """Get the REST API URL prefix for this build.

        Returns:
            REST API URL prefix for this build
        """
        return self._rest_api_url(self.autobuilder_url)

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
