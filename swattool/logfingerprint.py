#!/usr/bin/env python3

"""Swatbot log comparison functions.

This module provides functionality for comparing log files by generating
fingerprints and computing similarity scores between them.
"""

import logging
import re
from typing import Optional

import jellyfish  # type: ignore

from . import swatlogs
from . import swatbuild

logger = logging.getLogger(__name__)


class LogFingerprint:
    """A logfile fingerprint, allowing to compute similarity with others.

    Creates a fingerprint of a log file based on highlighted lines and
    provides methods to compare it with other log fingerprints.
    """

    _similarity_scores: dict[tuple[tuple[int, str], ...], float] = {}
    threshold = .7

    def __init__(self, failure: swatbuild.Failure, logname: str):
        self.failure = failure
        self.logname = logname

        # Limit finger print to the 100 first highlights. This is way above the
        # number of hilights for most log files but allow to handle rare cases
        # with thousands of matches.
        log = swatlogs.Log(failure, logname)
        self.lines = log.get_highlights_text()[:100]

    def _get_similarity_score(self, other: 'LogFingerprint') -> float:
        """Get similarity score between log of this entry and another log.

        Computes a similarity score between 0.0 and 1.0 based on Jaro similarity
        of highlighted lines in both logs.

        Args:
            other: Another LogFingerprint to compare with

        Returns:
            Similarity score between 0.0 and 1.0
        """
        if not self.lines or not other.lines:
            return 1 if not self.lines and not other.lines else 0

        specific_error_re = re.compile(r"^\S+error:",
                                       flags=re.IGNORECASE | re.MULTILINE)

        # Compute scores for all fingerprint fragment combinations
        # Only consider combinations with similar positions in the files:
        # reduce both false positives and computation time.
        scores = [[0 for _ in other.lines] for _ in self.lines]
        lendiff = len(self.lines) - len(other.lines)
        for i, fing1 in enumerate(self.lines):
            for j, fing2 in enumerate(other.lines):
                maxdist = 2
                startdist = i - j
                enddist = lendiff - startdist
                if min(abs(startdist), abs(enddist)) > maxdist:
                    continue
                scores[i][j] = jellyfish.jaro_similarity(fing1, fing2)

        # Compute the final score as 2 half-scores: fingerprint A to B, then B
        # to A, so the similarity score is commutative.
        def half_score(fingerprint, hafnum):
            num = 0
            denom = 0
            for i, fragment in enumerate(fingerprint):
                # Lines with a specific error, such as "AssertionError" and not
                # just "ERROR:" are more likely to be decisive: reflect this in
                # the similarity score.
                factor = 5 if any(specific_error_re.finditer(fragment)) else 1

                if hafnum == 0:
                    bestsim = max(scores[i])
                else:
                    bestsim = max(s[i] for s in scores)
                num += factor * bestsim if bestsim > .7 else 0
                denom += factor

            score = num / denom
            return score

        s1 = half_score(self.lines, 0)
        s2 = half_score(other.lines, 1)

        return (s1 + s2) / 2

    def _get_cached_score(self,
                          failure: Optional[swatbuild.Failure] = None,
                          logname: Optional[str] = None,
                          other: Optional['LogFingerprint'] = None,
                          ) -> float:
        if failure is None or logname is None:
            assert failure is None and logname is None
            assert other is not None
            failure = other.failure
            logname = other.logname
        else:
            assert other is None
        key = tuple(sorted(((self.failure.id, self.logname),
                            (failure.id, logname))))
        score = self._similarity_scores.get(key)
        if score is None:
            if other is None:
                other = LogFingerprint(failure, logname)
            score = self._get_similarity_score(other)
            self._similarity_scores[key] = score

        return score

    def get_similarity_score(self, other: 'LogFingerprint') -> float:
        """Get similarity score between log of this entry and another log.

        Retrieves a cached similarity score or computes it if not available.

        Args:
            other: Another LogFingerprint to compare with

        Returns:
            Similarity score between 0.0 and 1.0
        """
        return self._get_cached_score(other=other)

    def is_similar_to(self, other: 'LogFingerprint') -> bool:
        """Check if a given log fingerprint is similar to this one.

        Determines if the similarity score exceeds the threshold.

        Args:
            other: Another LogFingerprint to compare with

        Returns:
            True if logs are similar, False otherwise
        """
        return self._get_cached_score(other=other) > self.threshold

    def is_similar_to_failure(self, failure: swatbuild.Failure, logname: str
                              ) -> bool:
        """Check if a given log fingerprint is similar to this one.

        Determines if the similarity score exceeds the threshold.

        Args:
            failure: The failure to compare with
            logname: The name of the log file to compare with

        Returns:
            True if logs are similar, False otherwise
        """
        return self._get_cached_score(failure, logname) > self.threshold


_cached_log_fingerprint: dict[tuple[swatbuild.Failure, str],
                              LogFingerprint] = {}


def get_log_fingerprint(failure: swatbuild.Failure,
                        logname: str = 'stdio') -> LogFingerprint:
    """Get a fingerprint of the log, allowing to compare it with others.

    Creates or retrieves a cached LogFingerprint for the given failure and log.

    Args:
        failure: The failure containing the log
        logname: The name of the log file (default: 'stdio')

    Returns:
        LogFingerprint object for the specified log
    """
    fingerprint = _cached_log_fingerprint.get((failure, logname), None)
    if fingerprint is not None:
        return fingerprint

    fingerprint = LogFingerprint(failure, logname)
    _cached_log_fingerprint[(failure, logname)] = fingerprint

    return fingerprint
