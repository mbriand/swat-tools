#!/usr/bin/env python3

"""Swatbot review functions."""

import logging
import multiprocessing.pool as mppool
import re
import time
from typing import Collection, Optional

import jellyfish
import yaml

from . import logsview
from . import utils
from . import swatbuild
from . import swatbotrest

logger = logging.getLogger(__name__)

TRIAGEHISTORY_FILE = utils.DATADIR / "triage-history.yaml"


class TriageHistoryEntry:
    """A build log fingerprint and triage status."""

    def __init__(self, values: Optional[dict] = None):
        self.log_fingerprint = []
        self.triage = None
        self.triagenotes = None

        if values:
            self.log_fingerprint = values['log-fingerprint']
            self.triage = swatbotrest.TriageStatus.from_str(values['triage'])
            self.triagenotes = values['triagenotes']

    def as_dict(self) -> dict:
        """Export data as a dictionary."""
        return {'log-fingerprint': self.log_fingerprint,
                'triage': self.triage.name,
                'triagenotes': self.triagenotes,
                }

    @staticmethod
    def from_build(build: swatbuild.Build) -> 'TriageHistoryEntry':
        """Get build log fingerprint and triage status."""
        failure = build.get_first_failure()
        fingerprint = logsview.get_log_fingerprint(failure, 'stdio')

        triage = TriageHistoryEntry()
        triage.log_fingerprint = fingerprint
        triage.triage = failure.triage
        triage.triagenotes = failure.triagenotes

        return triage

    def get_similarity_score(self, log_fingerprint: Collection[str]) -> float:
        """Get similarity score between log of this entry and another log."""
        if not self.log_fingerprint or not log_fingerprint:
            return 0

        specific_error_re = re.compile(r"^\S+error:",
                                       flags=re.IGNORECASE | re.MULTILINE)

        # Compute scores for all fingerprint fragment combinations
        # Only consider combinations with similar positions in the files:
        # reduce both false positives and computation time.
        scores = [[0 for f2 in log_fingerprint] for f1 in self.log_fingerprint]
        lendiff = len(self.log_fingerprint) - len(log_fingerprint)
        for i, f1 in enumerate(self.log_fingerprint):
            for j, f2 in enumerate(log_fingerprint):
                maxdist = 2
                startdist = i - j
                enddist = lendiff - startdist
                if min(abs(startdist), abs(enddist)) > maxdist:
                    continue
                scores[i][j] = jellyfish.jaro_similarity(f1, f2)

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
                num += factor * bestsim
                denom += factor

            return num / denom

        score = half_score(self.log_fingerprint, 0)
        score *= half_score(log_fingerprint, 1)
        return score


class SimilarTriage:
    """A similar triage with similarity score and triage data."""

    # pylint: disable=too-few-public-methods

    def __init__(self, buildid: int, entry: TriageHistoryEntry, score: float):
        self.buildid = buildid
        self.triage = entry.triage
        self.triagenotes = entry.triagenotes
        self.score = score
        # TODO: remove log fingerprint here, once the algorithm is stable
        self.log_fingerprint = entry.log_fingerprint


class TriageHistory:
    """A list of build logs fingerprint and triage statuses."""

    def __init__(self):
        self.entries = {}
        self.cache = {}

    def __len__(self):
        return len(self.entries)

    def add_build(self, build: swatbuild.Build):
        """Add triage info from a build."""
        self.entries[build.id] = TriageHistoryEntry.from_build(build)

    def load(self):
        """Load triage infos."""
        try:
            with TRIAGEHISTORY_FILE.open('r') as file:
                pretty_entries = yaml.load(file, Loader=yaml.Loader)
                self.entries = {k: TriageHistoryEntry(entry)
                                for k, entry in pretty_entries.items()}
        except FileNotFoundError:
            pass

    def save(self):
        """Export triage infos."""
        with TRIAGEHISTORY_FILE.open('w') as file:
            pretty_entries = {k: entry.as_dict()
                              for k, entry in self.entries.items()}
            yaml.dump(pretty_entries, file)

    def _compute_similar_triages(self, build: swatbuild.Build,
                                 timeout_s: Optional[float] = None
                                 ):
        # TODO: save this in some cache file ? Validated by a hash of the
        # history ?
        logging.debug("Starting compute_similar_triages() for %s", build.id)
        count = 10
        failure = build.get_first_failure()
        fingerprint = logsview.get_log_fingerprint(failure, 'stdio')
        timeout = time.time() + timeout_s if timeout_s else None

        similarity = {}
        for i, (buildid, entry) in enumerate(self.entries.items()):
            similarity[buildid] = entry.get_similarity_score(fingerprint)
            if timeout and time.time() > timeout:
                logging.warning("get_similar_triages() timeout "
                                "after parsing %s of %s triage history"
                                "for build %s",
                                i, len(self.entries), build.id)
                break

        sims = sorted(similarity.items(), key=lambda e: e[1],
                      reverse=True)[:count]
        return sims

    def compute_similar_triages(self, build: swatbuild.Build,
                                timeout_s: Optional[float] = None
                                ):
        """Compute a list of triage entries for builds similar to this one."""
        self.cache[build.id] = self._compute_similar_triages(build, timeout_s)

    def compute_similar_triages_in_mppool(self,
                                          pool: mppool.Pool,
                                          build: swatbuild.Build,
                                          timeout_s: Optional[float] = None
                                          ) -> mppool.AsyncResult:
        """Compute a list of triage entries for builds similar to this one."""
        def callback(sims):
            self.cache[build.id] = sims

        job = pool.apply_async(self._compute_similar_triages,
                               [build], {'timeout_s': timeout_s}, callback)
        return job

    def get_similar_triages(self, build: swatbuild.Build,
                            timeout_s: Optional[float] = None
                            ) -> list[SimilarTriage]:
        """Get a list of triage entries for builds similar to this one."""
        if build.id not in self.cache:
            self.compute_similar_triages(build, timeout_s)

        return [SimilarTriage(e[0], self.entries[e[0]], e[1])
                for e in self.cache[build.id]]
