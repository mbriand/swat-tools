#!/usr/bin/env python3


"""A tool helping triage of Yocto autobuilder failures.

This module provides the main entry point and CLI interface for the swattool
application, which helps with the triage of Yocto autobuilder failures.
"""

import datetime
import logging
import re
import textwrap
from typing import Any, Callable, Collection

import click
import tabulate
from tqdm.contrib.logging import tqdm_logging_redirect

from .bugzilla import Bugzilla
from . import initmanager
from . import review
from . import swatbotrest
from . import swatbuild
from . import userdata
from . import utils

logger = logging.getLogger(__name__)


def _add_options(options):
    def _add_options(func):
        for option in reversed(options):
            func = option(func)
        return func

    return _add_options


def parse_filters(filters_cmd, config: dict) -> dict[str, Any]:
    """Parse filter arguments.

    Parse filter values given as program arguments and configuration file,
    merging them to generate a dictionary to be used with get_failure_infos().

    Args:
        filters_cmd: Dictionary of filter arguments from CLI options
        config: Configuration dictionary loaded from config file

    Returns:
        Dictionary of parsed filters ready for use with get_failure_infos()
    """

    def regex_filter(lst):
        # Create a regex for each entry. If it is an alphanumeric string use
        # full match, so user can pass exact name without having to bother with
        # regex.
        return [
            re.compile(f"^{f}$" if str(f).isalnum() else f, flags=re.I)
            for f in lst
        ]

    def bool_filter(val):
        if val in ["yes", True, "true"]:
            return True
        if val in ["no", False, "false"]:
            return False
        return None

    filters_in = filters_cmd.copy()
    for k, v in config.get("swattool-filters", {}).items():
        if not filters_cmd[k] and not isinstance(filters_cmd[k], bool):
            filters_in[k] = v

    statuses = [
        swatbuild.Status[s.upper()] for s in filters_in["status_filter"]
    ]
    triages = [
        swatbotrest.TriageStatus.from_str(s)
        for s in filters_in.get("triage_filter", [])
    ]

    completed_after = completed_before = None
    if filters_in["completed_after"]:
        completed_after = filters_in["completed_after"].astimezone()
    elif not filters_in["completed_before"]:
        after_date = datetime.datetime.now() - datetime.timedelta(days=30)
        completed_after = after_date.astimezone()
        logger.warning(
            "Only considering builds after %s", after_date.strftime("%Y-%m-%d")
        )

    if filters_in["completed_before"]:
        completed_before = filters_in["completed_before"].astimezone()

    filters = {
        "build": regex_filter(filters_in["build_filter"]),
        "parent_build": regex_filter(filters_in["parent_build_filter"]),
        "test": regex_filter(filters_in["test_filter"]),
        "ignore-test": regex_filter(filters_in["ignore_test_filter"]),
        "status": statuses,
        "owner": regex_filter(filters_in["owner_filter"]),
        "completed-after": completed_after,
        "completed-before": completed_before,
        "with-notes": bool_filter(filters_in["with_notes"]),
        "with-new-status": bool_filter(filters_in["with_new_status"]),
        "triage": triages,
        "log-matches": [re.compile(r) for r in filters_in["log_matches"]],
    }
    return filters


def parse_sort(kwargs, config: dict) -> list[swatbuild.Field]:
    """Parse sort arguments.

    Parse sort values given as program arguments and configuration file,
    merging them to generate a list of Field objects for sorting builds.

    Args:
        kwargs: Dictionary of arguments from CLI options
        config: Configuration dictionary loaded from config file

    Returns:
        List of Field objects for sorting builds
    """
    sort_cmd = kwargs.get("sort")
    sort_config = config.get("swattool", {}).get("sort", [])
    sort_in = sort_cmd if sort_cmd else sort_config
    if not sort_in:
        sort_in = ["Build"]
    return [swatbuild.Field(k) for k in sort_in]


def parse_urlopens(kwargs) -> set[str]:
    """Parse url open arguments.

    Args:
        kwargs: Dictionary of URL open arguments from CLI options

    Returns:
        Set of URL types to open
    """
    opens = set()
    for urltype in ["autobuilder", "swatbot", "stdio"]:
        if kwargs.get(f"open_{urltype}_url"):
            opens.add(urltype)

    return opens


class AliasedGroup(click.Group):
    """Support of subcommand aliases."""

    aliases = {
        "show": "show-pending-failures",
        "review": "review-pending-failures",
        "publish": "publish-new-reviews",
        "push": "publish-new-reviews",
    }

    def get_command(self, ctx, cmd_name):
        if cmd_name in self.aliases:
            cmd_name = self.aliases[cmd_name]
        return click.Group.get_command(self, ctx, cmd_name)


@click.group(cls=AliasedGroup)
@click.option("-v", "--verbose", count=True, help="Increase verbosity")
def maingroup(verbose: int):
    """Handle triage of Yocto autobuilder failures.

    Args:
        verbose: Verbosity level for logging
    """
    utils.setup_logging(verbose)
    utils.setup_readline()


def handle_login_exception(err: utils.LoginRequiredException) -> bool:
    """Handle a login exception by authenticating on remote service.

    Args:
        err: The login exception containing service information

    Returns:
        True if login was successful, raises exception otherwise
    """
    config = utils.load_config()
    if err.service == "swatbot":
        logger.warning("Login required to swatbot server")
        user = config.get("credentials", {}).get("swatbot_login")
        if not user:
            user = click.prompt("swatbot user")
        password = click.prompt("swatbot password", hide_input=True)
        return swatbotrest.login(user, password)

    if err.service == "bugzilla":
        logger.warning("Login required to bugzilla server")
        user = config.get("credentials", {}).get("bugzilla_login")
        if not user:
            user = click.prompt("bugzilla user")
        password = click.prompt("bugzilla password", hide_input=True)
        return Bugzilla.login(user, password)

    raise err


def shared_main(fn: Callable):
    """Shared entry point for swattool applications.

    Provides common initialization and error handling for swattool
    applications. Handles login exceptions by prompting for credentials when
    needed.

    Args:
        fn: The main function to execute
    """
    try:
        fn()
    except utils.LoginRequiredException as err:
        try:
            success = handle_login_exception(err)
        except RuntimeError:
            logger.error("Aborting login")
            return
        if success:
            fn()
    except utils.SwattoolException as err:
        logging.error(str(err))


def main():
    """Handle triage of Yocto autobuilder failures.

    Main entry point for the application. Sets up logging and handles login if
    needed.
    """
    shared_main(maingroup)


@maingroup.command()
@click.option("--user", "-u", prompt=True)
@click.option("--password", "-p", prompt=True, hide_input=True)
def login(user: str, password: str):
    """Login to the swatbot Django interface.

    Args:
        user: Username for swatbot login
        password: Password for swatbot login
    """
    swatbotrest.login(user, password)


@maingroup.command()
@click.option("--user", "-u", prompt=True)
@click.option("--password", "-p", prompt=True, hide_input=True)
def bugzilla_login(user: str, password: str):
    """Login to Yocto Project Bugzilla.

    Args:
        user: Username for Bugzilla login
        password: Password for Bugzilla login
    """
    Bugzilla.login(user, password)


failures_list_options = [
    click.option(
        "--limit",
        "-l",
        type=click.INT,
        default=None,
        help="Only parse the n last failures",
    ),
    click.option(
        "--sort",
        "-s",
        multiple=True,
        default=[],
        type=click.Choice(
            [str(f) for f in swatbuild.Field], case_sensitive=False
        ),
        help="Specify sort order",
    ),
    click.option(
        "--refresh",
        "-r",
        type=click.Choice(
            [p.name for p in swatbotrest.RefreshPolicy], case_sensitive=False
        ),
        default="auto",
        help="Fetch failures list from server instead of using cache",
    ),
    click.option(
        "--test-filter", "-t", multiple=True, help="Only show some tests"
    ),
    click.option(
        "--build-filter", "-b", multiple=True, help="Only show some builds"
    ),
    click.option(
        "--parent-build-filter",
        "-p",
        multiple=True,
        help="Only show some builds with parent build id",
    ),
    click.option(
        "--owner-filter",
        "-o",
        multiple=True,
        help='Only show some owners ("none" for no owner)',
    ),
    click.option(
        "--ignore-test-filter", "-T", multiple=True, help="Ignore some tests"
    ),
    click.option(
        "--status-filter",
        "-S",
        multiple=True,
        type=click.Choice(
            [str(s) for s in swatbuild.Status], case_sensitive=False
        ),
        help="Only show some statuses",
    ),
    click.option(
        "--completed-after",
        "-A",
        type=click.DateTime(),
        help="Only show failures after a given date",
    ),
    click.option(
        "--completed-before",
        "-B",
        type=click.DateTime(),
        help="Only show failures before a given date",
    ),
    click.option(
        "--with-notes",
        "-N",
        default="both",
        type=click.Choice(
            ["yes", "no", "both", "true", "false"], case_sensitive=False
        ),
        help="Only show failures with or without attached note",
    ),
    click.option(
        "--with-new-status",
        default=None,
        type=click.Choice(
            ["yes", "no", "both", "true", "false"], case_sensitive=False
        ),
        help="Only show failures with or without new (local) status",
    ),
    click.option(
        "--log-matches",
        multiple=True,
        default=None,
        help="Only show failures with logs matching a given regex. "
        "E.g. '.*Error.*'",
    ),
]

url_open_options = [
    click.option(
        "--open-autobuilder-url",
        "-u",
        is_flag=True,
        help="Open the autobuilder url in web browser",
    ),
    click.option(
        "--open-swatbot-url",
        "-w",
        is_flag=True,
        help="Open the swatbot url in web browser",
    ),
    click.option(
        "--open-stdio-url",
        "-g",
        is_flag=True,
        help="Open the first stdio url in web browser",
    ),
]


def _format_pending_failures(
    builds: list[swatbuild.Build],
    userinfos: userdata.UserInfos,
    shown_fields: list[swatbuild.Field],
) -> tuple[list[list[str]], list[str]]:
    # Generate a list of formatted builds on failures.
    def format_header(field):
        if field == swatbuild.Field.PARENT_BUILD:
            return "Parent"
        if field == swatbuild.Field.STATUS:
            return "Sts"
        return str(field)

    headers = [format_header(f) for f in shown_fields]
    table = [
        [
            build.format_field(userinfos.get(build.id, {}), field)
            for field in shown_fields
        ]
        for build in builds
    ]

    return (table, headers)


def _get_builds_infos(
    refresh: str,
    limit: int,
    sort: Collection[swatbuild.Field],
    filters: dict[str, Any],
    for_review: bool = False,
) -> tuple[list[swatbuild.Build], userdata.UserInfos]:
    swatbotrest.RefreshManager().set_policy_by_name(refresh)

    userinfos = userdata.UserInfos()
    init = initmanager.InitManager(
        userinfos, limit=limit, filters=filters, for_review=for_review
    )
    init.run()

    builds = init.get_builds(sort)

    return (builds, userinfos)


def _show_failures(
    refresh: str,
    urlopens: set[str],
    limit: int,
    sort: Collection[swatbuild.Field],
    filters: dict[str, Any],
):
    """Show all failures waiting for triage.

    Args:
        refresh: Refresh policy name for data fetching
        urlopens: Set of URL types to open
        limit: Maximum number of failures to show
        sort: Collection of field names to sort by
        filters: Dictionary of filters to apply
    """
    builds, userinfos = _get_builds_infos(refresh, limit, sort, filters)

    for build in builds:
        build.open_urls(urlopens)

    has_user_status = any(userinfos[build.id].triages for build in builds)
    has_notes = any(userinfos[build.id].notes for build in builds)

    shown_fields_all = [
        *swatbuild.Field.get_base_fields(),
        swatbuild.Field.SWAT_URL,
        swatbuild.Field.FAILURES,
        swatbuild.Field.TRIAGE if len(filters["triage"]) != 1 else None,
        swatbuild.Field.USER_STATUS if has_user_status else None,
        swatbuild.Field.USER_NOTES if has_notes else None,
    ]
    shown_fields = [f for f in shown_fields_all if f]

    table, headers = _format_pending_failures(builds, userinfos, shown_fields)
    print(tabulate.tabulate(table, headers=headers))

    logging.info(
        "%s entries found (%s warnings, %s errors and %s cancelled)",
        len(builds),
        len([b for b in builds if b.status == swatbuild.Status.WARNING]),
        len([b for b in builds if b.status == swatbuild.Status.ERROR]),
        len([b for b in builds if b.status == swatbuild.Status.CANCELLED]),
    )


@maingroup.command()
@_add_options(failures_list_options)
@_add_options(url_open_options)
@click.option(
    "--triage-filter",
    multiple=True,
    type=click.Choice(
        [str(s) for s in swatbotrest.TriageStatus], case_sensitive=False
    ),
    help="Only show some triage statuses",
)
def show_failures(refresh: str, limit: int, **kwargs):
    """Show all failures, including the old ones.

    Args:
        refresh: Refresh policy name for data fetching
        limit: Maximum number of failures to show
        sort: List of field names to sort by
        **kwargs: Additional filter arguments from CLI options
    """
    urlopens = parse_urlopens(kwargs)
    config = utils.load_config()
    filters = parse_filters(kwargs, config)
    sort = parse_sort(kwargs, config)
    _show_failures(refresh, urlopens, limit, sort, filters)


@maingroup.command()
@_add_options(failures_list_options)
@_add_options(url_open_options)
def show_pending_failures(refresh: str, limit: int, **kwargs):
    """Show all failures waiting for triage.

    Args:
        refresh: Refresh policy name for data fetching
        limit: Maximum number of failures to show
        sort: List of field names to sort by
        **kwargs: Additional filter arguments from CLI options
    """
    urlopens = parse_urlopens(kwargs)
    config = utils.load_config()
    filters = parse_filters(kwargs, config)
    filters["triage"] = [swatbotrest.TriageStatus.PENDING]
    sort = parse_sort(kwargs, config)
    _show_failures(refresh, urlopens, limit, sort, filters)


@maingroup.command()
@_add_options(failures_list_options)
@_add_options(url_open_options)
def review_pending_failures(refresh: str, limit: int, **kwargs):
    """Review failures waiting for triage.

    Args:
        refresh: Refresh policy name for data fetching
        limit: Maximum number of failures to show
        sort: List of field names to sort by
        **kwargs: Additional filter arguments from CLI options
    """
    urlopens = parse_urlopens(kwargs)
    config = utils.load_config()
    filters = parse_filters(kwargs, config)
    filters["triage"] = [swatbotrest.TriageStatus.PENDING]
    sort = parse_sort(kwargs, config)

    builds, userinfos = _get_builds_infos(
        refresh, limit, sort, filters, for_review=True
    )

    if not builds:
        return

    reviewmenu = review.ReviewMenu(config, builds, userinfos, urlopens)
    reviewmenu.show()

    userinfos.save()


@maingroup.command()
@_add_options(failures_list_options)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Do not ask for confirmation for each failure",
)
@click.argument(
    "status",
    type=click.Choice(
        [str(s) for s in swatbotrest.TriageStatus], case_sensitive=False
    ),
)
@click.argument("status-comment", type=str)
def batch_triage_failures(
    refresh: str,
    limit: int,
    yes: bool,
    status: str,
    status_comment: str,
    **kwargs,
):
    """Triage pending failures matching given criteria.

    Args:
        refresh: Refresh policy name for data fetching
        limit: Maximum number of failures to show
        sort: List of field names to sort by
        yes: Skip confirmation for each failure if True
        status: Triage status to set for matching failures
        status_comment: Comment for the triage status (or bug number for 'Bug'
                        status)
        **kwargs: Additional filter arguments from CLI options
    """
    # pylint: disable=too-many-arguments,too-many-positional-arguments

    config = utils.load_config()
    filters = parse_filters(kwargs, config)
    filters["triage"] = [swatbotrest.TriageStatus.PENDING]
    sort = parse_sort(kwargs, config)

    builds, userinfos = _get_builds_infos(refresh, limit, sort, filters)
    reviewmenu = review.ReviewMenu(config, builds, userinfos)
    reviewmenu.batch_menu(
        not yes, swatbotrest.TriageStatus.from_str(status), status_comment
    )

    userinfos.save()


@maingroup.command()
@click.option(
    "--dry-run", "-n", is_flag=True, help="Only shows what would be done"
)
def publish_new_reviews(dry_run: bool):
    """Publish new local triage status to swatbot Django interface.

    Args:
        dry_run: Only show what would be done without making changes if True
    """
    reviews = review.get_new_reviews()

    bar_format = "{l_bar}{bar}| [{elapsed}<{remaining}, {postfix}]"
    with tqdm_logging_redirect(
        reviews.items(), bar_format=bar_format, desc="Publishing new reviews"
    ) as progress:
        if not progress:
            return

        for (status, comment), triages in progress:
            bugurl = None

            # Bug entry: need to also publish a new comment on bugzilla.
            if status == swatbotrest.TriageStatus.BUG:
                bugid = int(comment)
                logs = [
                    triage.extra["bugzilla-comment"]
                    for triage in sorted(triages, key=lambda d: d.change_date)
                    if triage.failures
                ]

                if any(logs):
                    comment = bugurl = Bugzilla.get_bug_url(bugid)
                    bugtitle = Bugzilla.get_bug_title(bugid)
                    logger.info(
                        "Need to update ticket %s (%s) with:\n%s",
                        bugtitle,
                        bugurl,
                        "\n".join(
                            textwrap.indent(log, "    ") for log in logs
                        ),
                    )
                    if not dry_run:
                        Bugzilla.add_bug_comment(bugid, "\n".join(logs))

            failureids = [fid for triage in triages for fid in triage.failures]
            wrappedfails = textwrap.wrap(
                ", ".join(str(fid) for fid in failureids),
                initial_indent="    ",
                subsequent_indent="    ",
            )

            logger.info(
                'Need to update failures to status %s with comment "%s"\n%s\n',
                status,
                comment,
                "\n".join(wrappedfails),
            )
            if not dry_run:
                for failureid in failureids:
                    swatbotrest.publish_status(failureid, status, comment)

    if not dry_run:
        swatbotrest.invalidate_stepfailures_cache()


CACHE_CLEAN_DATE_FILE = utils.CACHEDIR / "cache_clean"


def _do_clean_cache():
    """Remove cache files that have not been used for some time."""
    clean_limit_date = datetime.datetime.now() - datetime.timedelta(days=30)
    clean_limit_time = clean_limit_date.timestamp()

    logging.info("Cleaning old cache entries.")

    for path in utils.CACHEDIR.rglob("*"):
        if not path.is_file():
            continue
        if path.stat().st_atime > clean_limit_time:
            continue
        path.unlink()

    with CACHE_CLEAN_DATE_FILE.open("w") as file:
        file.write(str(datetime.datetime.now().timestamp()))


@maingroup.result_callback()
def maybe_clean_cache(*args, **kwargs):
    """Trigger cache cleaning if it was not done lately."""
    # pylint: disable=unused-argument

    clean_date_min = datetime.datetime.now() - datetime.timedelta(days=7)

    try:
        with CACHE_CLEAN_DATE_FILE.open("r") as file:
            last_clean = float(file.read())
    except (FileNotFoundError, ValueError):
        last_clean = 0

    if last_clean < clean_date_min.timestamp():
        _do_clean_cache()


@maingroup.command()
def clean_cache():
    """Remove cache files that have not been used for some time."""
    _do_clean_cache()
