# SWAT Tools

A Python toolset for triaging Yocto autobuilder failures reported on
[swatbot.yoctoproject.org](https://swatbot.yoctoproject.org).

This project provides two main tools:
- **swattool**: Interactive triage tool for managing build failures
- **swatbot_missing_builds**: Tool for importing missing buildbot builds into
  swatbot

## Overview

The Yocto Project autobuilders run continuous integration tests that sometimes
fail. When failures occur, they need to be triaged to determine if they
represent real issues or infrastructure problems. SWAT Tools streamlines this
process by providing command-line interfaces to review, categorize, and track
these failures.

## Installation

### Requirements

- Python 3.11 or higher

### Install from Source

```bash
pip install git+https://git.yoctoproject.org/git/swat-tools
```

### Development Installation

```bash
git clone https://git.yoctoproject.org/git/swat-tools
cd swat-tools
pip install -e .
```

## swattool Usage

The `swattool` command provides several subcommands for managing build failure
triage:

### Available Commands

- `login` - Authenticate with swatbot interface
- `bugzilla-login` - Authenticate with Yocto Project Bugzilla
- `show-failures` - Display all failures (including resolved ones)
- `show-pending-failures` - Display failures awaiting triage
- `review-pending-failures` - Interactive triage interface
- `batch-triage-failures` - Bulk triage operations
- `publish-new-reviews` - Upload local triage decisions to swatbot

### login

Authenticate with the swatbot Django interface to enable triage operations.

```bash
swattool login
```

### bugzilla-login

Authenticate with Yocto Project Bugzilla for bug reporting functionality.

```bash
swattool bugzilla-login
```

### show-failures

Display all build failures, including those already triaged. Useful for
reviewing historical data.

```bash
swattool show-failures [OPTIONS]
```

### show-pending-failures

Display all build failures that are awaiting triage. This is the primary
command for seeing what needs attention.

**Common Options:**
- `--owner-filter` - Show only failures assigned to a specific user. Can be
  "none" for unassigned entries.
- `--sort` - Sort by field values. Supported fields: 'Build', 'Status',
  'Test', 'Owner', 'Worker', 'Completed', 'SWAT URL', 'Autobuilder URL',
  'Failures', 'Branch', 'Notes', 'New Triage', 'Triage', 'Parent Build'.
- `--limit N` - Limit results to N failures
- `--test-filter PATTERN` - Filter by test name pattern

**Example:**

```
$ swattool show-pending-failures --owner-filter none --sort test
  Build  Sts    Test                 Worker            Completed             SWAT URL                                            Failures
-------  -----  -------------------  ----------------  --------------------  --------------------------------------------------  ----------------------------------------------
 565962  Err    check-layer-nightly  ubuntu2004-ty-1   2024-10-08T02:00:11Z  https://swatbot.yoctoproject.org/collection/36474/  Unpack shared repositories
 565726  Err    check-layer-nightly  debian11-ty-3     2024-10-07T03:06:27Z  https://swatbot.yoctoproject.org/collection/36342/  Test meta-virtualization YP Compatibility: Run
 565002  Err    check-layer-nightly  opensuse154-ty-1  2024-10-04T02:00:12Z  https://swatbot.yoctoproject.org/collection/35944/  Unpack shared repositories
 565931  Err    oe-selftest          alma9-ty-2        2024-10-08T03:34:56Z  https://swatbot.yoctoproject.org/collection/36469/  OE Selftest: Run cmds
 565693  Err    oe-selftest          ubuntu2204-ty-3   2024-10-07T03:15:15Z  https://swatbot.yoctoproject.org/collection/36335/  OE Selftest: Run cmds
 565277  Err    oe-selftest          alma8-ty-1        2024-10-05T03:18:20Z  https://swatbot.yoctoproject.org/collection/36082/  OE Selftest: Run cmds
 565432  Err    oe-selftest-centos   stream8-ty-1      2024-10-06T03:26:27Z  https://swatbot.yoctoproject.org/collection/36210/  OE Selftest: Run cmds
 565430  Err    oe-selftest-debian   debian12-ty-1     2024-10-06T03:26:17Z  https://swatbot.yoctoproject.org/collection/36210/  OE Selftest: Run cmds
 565433  Err    oe-selftest-fedora   fedora38-ty-6     2024-10-06T03:28:00Z  https://swatbot.yoctoproject.org/collection/36210/  OE Selftest: Run cmds
 565434  Err    oe-selftest-ubuntu   ubuntu2304-ty-1   2024-10-06T03:49:01Z  https://swatbot.yoctoproject.org/collection/36210/  OE Selftest: Run cmds
 565283  Err    qemuarm-alt          fedora38-ty-6     2024-10-05T01:28:48Z  https://swatbot.yoctoproject.org/collection/36082/  QA targets
 565967  Err    toaster              opensuse154-ty-3  2024-10-08T03:39:14Z  https://swatbot.yoctoproject.org/collection/36479/  Run cmds
 565731  Err    toaster              fedora38-ty-6     2024-10-07T03:39:56Z  https://swatbot.yoctoproject.org/collection/36346/  Run cmds
 565497  Err    toaster              opensuse154-ty-1  2024-10-06T03:39:09Z  https://swatbot.yoctoproject.org/collection/36220/  Run cmds
 565315  Err    toaster              opensuse154-ty-1  2024-10-05T03:40:33Z  https://swatbot.yoctoproject.org/collection/36093/  Run cmds
```

### review-pending-failures

Interactive triage interface for reviewing and categorizing build failures.
This command presents failures one by one and allows you to assign status and
take actions.

**Important:** All modifications are done locally until you use
`publish-new-reviews` to upload your decisions.

**Example Session:**

```bash
swattool review-pending-failures
```

```
Build            565962
Status           Error
Test             check-layer-nightly
Owner
Worker           ubuntu2004-ty-1
Completed        2024-10-08T02:00:11Z
SWAT URL         https://swatbot.yoctoproject.org/collection/36474/
Autobuilder URL  https://autobuilder.yoctoproject.org/typhoon/#/builders/121/builds/2311
Failures         Unpack shared repositories

Action
> [n] next
  [p] previous
  [l] list all failures
  [q] quit
Progress: 1/15
```

### batch-triage-failures

Perform bulk triage operations on multiple failures matching specific criteria.

```bash
swattool batch-triage-failures --status "ab-int" \
  --status-comment "Infrastructure issue" [OPTIONS]
```

### publish-new-reviews

Upload your local triage decisions to the swatbot Django interface. This
command publishes all pending local changes made during
`review-pending-failures` sessions.

```bash
swattool publish-new-reviews [--dry-run]
```

Use `--dry-run` to preview what would be published without making actual
changes.

## `swatbot_missing_builds` Usage

The `swatbot_missing_builds` tool helps import missing buildbot builds into
swatbot for tracking purposes.

```bash
swatbot_missing_builds [SUBCOMMAND] [OPTIONS]
```

See the tool's help output for available subcommands and options.

## Configuration

Both tools support configuration files to set default values and avoid
repetitive command-line options.

### Configuration File Location

Store your configuration in `~/.config/swattool.toml`.

### Configuration Options

The configuration file uses TOML format with these main sections:

#### `[swattool]` - General swattool settings

- `sort` - Default sort order for failure listings (array of field names)

#### `[swattool-filters]` - Default filter settings

These correspond to the command-line filter options and allow you to set
defaults:

- `test_filter` - Filter by test names (array of patterns)
- `build_filter` - Filter by build numbers (array of patterns)
- `parent_build_filter` - Filter by parent build patterns (array of patterns)
- `owner_filter` - Filter by owner names (array of patterns, use "none" for
  unassigned)
- `ignore_test_filter` - Exclude specific tests (array of patterns)
- `status_filter` - Filter by build status (array of status names: "Success",
  "Warnings", "Error", "Exception", "Cancelled", "Retry", "Skipped")
- `completed_after` - Only show builds completed after this date (ISO format)
- `completed_before` - Only show builds completed before this date (ISO format)
- `with_notes` - Show only builds with/without notes (boolean or "both")
- `with_new_status` - Show only builds with/without new triage status (boolean
  or "both")
- `triage_filter` - Filter by triage status (array of: "Pending", "AB-Int",
  "YP-Int", "Analysis", "Bug", "Ignore")
- `log_matches` - Filter by log content matching regex patterns (array of
  regex strings)

#### `[credentials]` - Login credentials (optional)

- `swatbot_login` - Default username for swatbot authentication
- `bugzilla_login` - Default username for Bugzilla authentication

**Note:** Passwords are never stored in configuration files for security
reasons.

### Supported Field Names for Sorting

Available field names for the `sort` configuration option:

- `Build` - Build number
- `Status` - Build status
- `Test` - Test name
- `Owner` - Assigned owner
- `Worker` - Worker machine name
- `Completed` - Completion timestamp
- `SWAT URL` - SWAT interface URL
- `Autobuilder URL` - Autobuilder URL
- `Failures` - Failure descriptions
- `Branch` - Git branch
- `Notes` - Triage notes
- `New Triage` - New triage status
- `Triage` - Current triage status
- `Parent Build` - Parent build number

### Example Configuration

```toml
[swattool]
sort = ['Parent Build', 'Test']

[swattool-filters]
parent_build_filter = ['vk/*']
with_new_status = false
status_filter = ['Error', 'Exception']
completed_after = "2024-01-01T00:00:00"

[credentials]
swatbot_login = 'your-username'
bugzilla_login = 'your-username'
```

# Known issues

- We use the `readline` module to have a bit fancier input() function, but
  `simple_term_menu` does not behave well when `readline` is loaded. This has
  been reproduced with upstream `simple_term_menu` demo code, just by
  additionally importing `readline`. We should either investigate the root
  cause of the issue or get rid of one of the modules.
  <https://github.com/IngoMeyer441/simple-term-menu/issues/98>

# Contributing

Please refer to our contributor guide here:
https://docs.yoctoproject.org/contributor-guide/ for full details on how to
submit changes.

As a quick guide, patches should be sent to
yocto-patches@lists.yoctoproject.org
The git command to do that would be:

```
git send-email -M -1 --subject-prefix='swat-tools][PATCH' \
  --to yocto-patches@lists.yoctoproject.org
```

The 'To' header and prefix can be set as default for this repository:

```
git config sendemail.to yocto-patches@lists.yoctoproject.org
git config format.subjectPrefix 'swat-tools][PATCH'
```
