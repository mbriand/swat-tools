#!/usr/bin/env python3

"""Interraction with the swatbot Django server."""

import enum
import logging
from datetime import datetime
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
    """The status of a failure."""

    WARNING = 1
    ERROR = 2
    CANCELLED = 6
    UNKNOWN = -1

    @staticmethod
    def from_int(status: int) -> 'Status':
        """Get Status instance from an integer status value."""
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
        """Return status in a pretty colorized string."""
        return self._colorize(self.name.title())

    def as_short_colored_str(self):
        """Return status in a short pretty colorized string."""
        return self._colorize(self.name[:3].title())


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
    BRANCH = 'Branch'
    USER_NOTES = 'Notes'
    USER_STATUS = 'New Triage'
    TRIAGE = 'Triage'


class Failure:
    """A Swatbot failure."""

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
        """Get the URL of a given log webpage."""
        if logname not in self.urls:
            return None
        return self.urls[logname]

    def open_log_url(self, logname: str = "stdio"):
        """Open log URL in default browser."""
        logurl = self.get_log_url()
        if logurl:
            click.launch(logurl)
        else:
            logger.error("Failed to find %s log", logname)

    def get_log_raw_url(self, logname: str = "stdio"
                        ) -> Optional[str]:
        """Get the URL of a raw log file."""
        rest_url = self.build.rest_api_url()
        return buildbotrest.get_log_raw_url(rest_url, self.build.id,
                                            self.stepnumber, logname)

    def get_log(self, logname: str) -> Optional[str]:
        """Get content of a given log file."""
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
        """Get triage desctiption string, including notes."""
        notes = self.triagenotes
        if self.triage == swatbotrest.TriageStatus.BUG:
            bugid = bugzilla.Bugzilla.get_bug_id_from_url(notes)
            if bugid:
                notes = str(bugid)

        if notes:
            return f"{self.triage}: {notes}"

        return str(self.triage)


class Build:
    """A Swatbot build."""

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
        self.completed = attributes['completed']
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

    @property
    def git_info(self) -> dict[str, Any]:
        """Get informations about built git branch."""
        if self._git_info is None:
            gittag = None
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

                gittag = f"{host}{aburl.path}{name}-{number}"
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
                    properties = buildbot_build['builds'][0]['properties']
                    rev = properties['yp_build_revision'][0]

                    self._git_info = {'description': f"On commit {rev}"}
                else:
                    self._git_info = {'description': "On unknown revision"}

        return self._git_info

    def _test_match_filters(self, filters: dict[str, Any]) -> bool:
        matches = [True for r in filters['test'] if r.match(self.test)]
        if filters['test'] and not matches:
            return False

        matches = [True for r in filters['ignore-test']
                   if r.match(self.test)]
        if filters['ignore-test'] and matches:
            return False

        return True

    def _completed_match_filters(self, filters: dict[str, Any]) -> bool:
        if filters['completed-after'] and self.completed:
            completed = datetime.fromisoformat(self.completed)
            if completed < filters['completed-after']:
                return False

        if filters['completed-before'] and self.completed:
            completed = datetime.fromisoformat(self.completed)
            if completed > filters['completed-before']:
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
        """Check if this build match given filters."""
        # pylint: disable=too-many-return-statements

        def simple_match(field: Field):
            filtr = filters[field.name.lower()]
            return not filtr or self.get(field) in filtr

        simple_filters = [Field.BUILD, Field.OWNER, Field.STATUS]
        if not all(simple_match(field) for field in simple_filters):
            return False

        if not self._test_match_filters(filters):
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
        """Get data from the given field."""
        if field == Field.BUILD:
            return self.id
        if field.name.lower() in self.__dict__:
            return self.__dict__[field.name.lower()]

        raise utils.SwattoolException(f"Invalid field: {field}")

    def get_first_failure(self) -> Failure:
        """Get the first failure of the build."""
        for _, failure in sorted(self.failures.items()):
            if failure.status == self.status:
                return failure
        return self.failures[min(self.failures)]

    def get_sort_tuple(self, keys: Iterable[Field],
                       userinfos: Optional[dict[int, dict[Field, Any]]] = None
                       ) -> tuple:
        """Get selected fields in sortable fashion."""
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
                           maxwidth: int) -> str:
        """Get info on one given failure in a pretty way."""
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

            table.append([Field.FAILURES if i == 0 else "",
                          failure.stepname,
                          triage.format_description() if triage else ""])

        desc = tabulate.tabulate(table, tablefmt="plain")

        if userinfo.notes:
            wrapped = userinfo.get_wrapped_notes(maxwidth, " " * 4)
            desc += f"\n\n{Field.USER_NOTES}:\n{wrapped}"

        return desc

    def format_short_description(self) -> str:
        """Get condensed info on one given failure in a pretty way."""
        return f"Build {self.id} ({self.branch}): " \
               f"{self.test} on {self.worker}, " \
               f"{str(self.status).lower()} at {self.completed}"

    def format_tiny_description(self) -> str:
        """Get very short info on one given failure."""
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
        """Get the REST API URL prefix for this build."""
        return self._rest_api_url(self.autobuilder_url)

    def open_urls(self, urlopens: set[str]) -> None:
        """Open requested URLs in default browser."""
        if 'autobuilder' in urlopens:
            click.launch(self.autobuilder_url)
        if 'swatbot' in urlopens:
            click.launch(self.swat_url)
        if 'stdio' in urlopens:
            self.get_first_failure().open_log_url()
