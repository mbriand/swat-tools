A python tool helping triage of Yocto autobuilder failures reported on
https://swatbot.yoctoproject.org .

# Install

`pip install git+https://git.yoctoproject.org/git/swat-tools`

# Usage

The swattool command offers several subcommands, described here.

## login

Login to the swatbot Django interface.

## show-pending-failures

Show all failures waiting for triage. E.g.:

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

## review-pending-failures

Review failures waiting for triage.

All modifications are done locally, nothing is pushed to swatbot or bugzilla,
until you use the `publish-new-reviews` command.

E.g.:

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
  [a] ab-int
  [b] bug opened
  [c] cancelled no errors
  [m] mail sent
  [i] mail sent by Mathieu Dubois-Briand
  [o] other
  [f] other: Fixed
  [t] not for swat
  [r] reset status

  [e] edit notes
  [u] open autobuilder URL
  [w] open swatbot URL
  [g] open stdio log of first failed step URL
  [x] open stdio log of first failed step in pager

> [n] next
  [p] previous
  [l] list all failures
  [q] quit
Progress: 1/15
```

## publish-new-reviews

Publish new local triage status to swatbot Django interface.
