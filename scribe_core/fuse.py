"""Fusion: who said each word (plan 1.4; fixes T3, T5, T7).

Two independent signals with different failure modes:
* Forum voice events know IDENTITY (user id) but have sloppy edges and mic bleed.
* Deepgram diarization knows WHICH VOICE (acoustically) with tight edges, but its clusters
  are anonymous and it sometimes splits one person into two clusters or merges two.

Forum logs STUDENTS' microphones only. Across 717 real classes the professor
(``teacher-user-id``) had zero voice events, so the professor has to be inferred.

1. Map clusters to people. The professor's clusters come first: the biggest cluster that
   talks mostly while no student mic is on, plus any other such cluster spread across the
   class (diarizers split long talkers; ``teacher_clusters``). The rest is a
   seconds-overlap matrix cluster x student, solved as an assignment problem (Hungarian).
   Leftover clusters that still overwhelmingly match one person are treated as a split of
   that person. Anything else stays anonymous.
2. Label each word, recording ``how``:
     forum            one Forum user covers the word; no cluster opinion
     forum+cluster    Forum and the mapped cluster agree (strongest)
     cluster          no Forum coverage (edge jitter, mic off) or crosstalk resolved by voice
     teacher-cluster  no student mic on, and the word is in the professor's cluster
     conflict         one Forum user, but the mapped cluster says someone else. Mic bleed is
                      common, so strong acoustic evidence (cluster_conf > 0.8) wins.
     forum-overlap    crosstalk the cluster can't resolve: the user with most overlap wins
     no-student-mic   no clusters (groq/local) and no student mic on: the professor, by the
                      old v2 rule. Weaker than teacher-cluster: it can't tell the professor
                      from a guest, a video, or a student whose mic didn't register.
     none             nothing: speaker None, rendered "Unattributed"
3. Smoothing: short flickers between identical neighbours get relabelled when weaker.

With groq/local there are no clusters, so this runs on Forum signals only.
"""

from __future__ import annotations

from collections import Counter, defaultdict

import numpy as np
from scipy.optimize import linear_sum_assignment

EDGE = 0.25          # seconds of slack on Forum interval edges (T7)
MIN_WORD = 0.01      # zero-length ASR words still need to overlap something
OVERRIDE_CONF = 0.8  # cluster confidence needed to overrule a single Forum user
TEACHER_SHARE = 0.6  # share of a cluster's speech heard while no student mic is on
TEACHER_MIN_S = 30.0  # seconds; a professor talks for minutes, so smaller clusters never are
TEACHER_SPREAD = 3   # a second professor cluster speaks in >= 3 different 5-minute stretches
SPREAD_S = 300.0
PROFESSOR = "professor"  # stand-in user id when the class's teacher-user-id is unknown

HOW_RANK = {"forum+cluster": 5, "forum": 4, "cluster": 3, "teacher-cluster": 3,
            "forum-overlap": 2, "conflict": 1, "no-student-mic": 1, "none": 0}


def overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _span(w: dict) -> tuple[float, float]:
    s = float(w["start"])
    return s, max(float(w["end"]), s + MIN_WORD)


def overlapping(spans: list[tuple[float, float]], intervals: list[dict], pad: float = 0.0
                ) -> list[list[dict]]:
    """For each (start, end) span, the intervals overlapping it (interval edges padded).

    Sweep line: O(n log n + m log m + matches). Correct with overlapping and very long
    intervals (the plan's ``voice[k-8:k]`` neighbour window can miss those).
    """
    order = sorted(range(len(spans)), key=lambda i: spans[i][0])
    ivs = sorted(intervals, key=lambda v: v["start"])
    result: list[list[dict]] = [[] for _ in spans]
    active: list[dict] = []
    p = 0
    for i in order:
        a, b = spans[i]
        while p < len(ivs) and ivs[p]["start"] - pad < b:
            active.append(ivs[p])
            p += 1
        active = [v for v in active if v["end"] + pad > a]
        result[i] = [v for v in active if v["start"] - pad < b]
    return result


def cluster_overlap_matrix(words, voice):
    clusters = sorted({w.get("cluster") for w in words if w.get("cluster") is not None})
    users = sorted({v["user_id"] for v in voice if v.get("user_id") is not None})
    m = np.zeros((len(clusters), len(users)))
    if not clusters or not users:
        return clusters, users, m
    ci = {c: i for i, c in enumerate(clusters)}
    ui = {u: j for j, u in enumerate(users)}
    spans = [_span(w) for w in words]
    for w, (a, b), hits in zip(words, spans, overlapping(spans, voice), strict=True):
        c = w.get("cluster")
        if c is None:
            continue
        for v in hits:
            if v.get("user_id") in ui:
                m[ci[c], ui[v["user_id"]]] += overlap(a, b, v["start"], v["end"])
    return clusters, users, m


def speech_without_mic(words, voice, *, edge: float = EDGE) -> tuple[Counter, Counter]:
    """Per cluster: seconds of speech, and the seconds of it heard while no student mic is on."""
    spans = [_span(w) for w in words]
    total: Counter = Counter()
    quiet: Counter = Counter()
    for w, (a, b), cands in zip(words, spans, overlapping(spans, voice, edge), strict=True):
        cl = w.get("cluster")
        if cl is None:
            continue
        total[cl] += b - a
        if not any(v.get("user_id") is not None for v in cands):
            quiet[cl] += b - a
    return total, quiet


def teacher_clusters(words, voice, *, min_share: float = TEACHER_SHARE,
                     min_seconds: float = TEACHER_MIN_S, min_spread: int = TEACHER_SPREAD,
                     edge: float = EDGE) -> list:
    """The professor's clusters, biggest first ([] if none). A candidate has at least
    ``min_seconds`` of speech, at least ``min_share`` of it while no student mic is on
    (students' own speech sits inside their voice events, so their clusters fail this).

    The biggest candidate is the professor. Diarizers often split one long-talking voice in
    two, so any other candidate counts too if it speaks in at least ``min_spread`` different
    5-minute stretches; a video clip or a guest is one block and stays anonymous."""
    total, quiet = speech_without_mic(words, voice, edge=edge)
    ok = sorted((cl for cl, s in total.items() if s >= min_seconds and quiet[cl] / s >= min_share),
                key=lambda cl: -total[cl])
    if not ok:
        return []
    stretches = defaultdict(set)
    for w in words:
        if w.get("cluster") in ok:
            stretches[w["cluster"]].add(int(float(w["start"]) // SPREAD_S))
    return ok[:1] + [cl for cl in ok[1:] if len(stretches[cl]) >= min_spread]


def teacher_cluster(words, voice, **kw):
    """The professor's main (biggest) cluster, or None. See ``teacher_clusters``."""
    found = teacher_clusters(words, voice, **kw)
    return found[0] if found else None


def calibration_words(words, voice, *, min_share: float = 0.5) -> list[dict]:
    """The words to calibrate the clock on: all but the professor's cluster.

    Calibration lines up "someone is talking" in the audio with "a student mic is on" in
    Forum. The professor has no mic events and talks in the gaps between students, so their
    speech makes every offset look alike. Their cluster is found on the uncalibrated clock;
    misaligned student events cover some of it, hence the lower share. Without clusters
    (groq/local) every word is kept."""
    tc = teacher_cluster(words, voice, min_share=min_share)   # the biggest only: on this clock
    rest = [w for w in words if w.get("cluster") != tc] if tc is not None else words
    return rest or list(words)


def cluster_to_user(words, voice, *, teacher=None, min_share: float = 0.4,
                    min_seconds: float = 1.0, split_share: float = 0.6,
                    report: dict | None = None) -> dict:
    """{cluster: user_id}. With ``teacher`` (the class's teacher-user-id), the professor's
    clusters are found first and mapped to it. They have to go first: the professor has no
    voice events, so the overlap matrix only sees the moments their voice overlaps a
    student's mic and would call the whole cluster a split of that student. Then Hungarian
    over the other clusters (one-to-one), then split clusters (many-to-one)."""
    mapping: dict = {}
    if teacher is not None:
        for tc in teacher_clusters(words, voice):
            mapping[tc] = teacher
    clusters, users, m = cluster_overlap_matrix(words, voice)
    free = [r for r, cl in enumerate(clusters) if cl not in mapping]
    if free and users:
        sub = m[free]
        rows, cols = linear_sum_assignment(-sub)  # maximise total agreement
        for r, c in zip(rows, cols, strict=True):
            total = sub[r].sum()
            if total >= min_seconds and sub[r, c] / total >= min_share:
                mapping[clusters[free[r]]] = users[c]
        for r in free:
            cl = clusters[r]
            if cl in mapping:
                continue
            total = m[r].sum()
            if total < min_seconds:
                continue
            c = int(np.argmax(m[r]))
            if m[r, c] / total >= split_share:
                mapping[cl] = users[c]  # one person split across clusters
    if report is not None:
        speech, quiet = speech_without_mic(words, voice)
        report["clusters"] = [
            {"cluster": cl, "seconds": round(float(m[r].sum()), 2),
             "best_user": users[int(np.argmax(m[r]))] if m[r].sum() else None,
             "best_share": round(float(m[r].max() / m[r].sum()), 3) if m[r].sum() else 0.0,
             "speech_s": round(float(speech[cl]), 2),
             "no_mic_share": round(quiet[cl] / speech[cl], 3) if speech[cl] else 0.0,
             "mapped_to": mapping.get(cl)}
            for r, cl in enumerate(clusters)
        ]
    return mapping


def fuse(words, voice, mapping: dict | None = None, *, teacher=None, edge: float = EDGE,
         override_conf: float = OVERRIDE_CONF) -> list[dict]:
    """Label every word with ``user_id`` and ``how`` (see module docstring). ``teacher`` is
    the professor's user id; without it no word is ever given to the professor."""
    mapping = mapping or {}
    spans = [_span(w) for w in words]
    out = []
    for w, (a, b), cands in zip(words, spans, overlapping(spans, voice, edge), strict=True):
        hits: dict = {}
        for v in cands:
            uid = v.get("user_id")
            if uid is None:
                continue
            ov = overlap(a, b, v["start"] - edge, v["end"] + edge)
            if ov > 0:
                hits[uid] = hits.get(uid, 0.0) + ov
        cl = w.get("cluster")
        by_cluster = mapping.get(cl) if cl is not None else None
        extra = {}
        if len(hits) == 1:
            uid = next(iter(hits))
            if by_cluster is None:
                how = "forum"
            elif by_cluster == uid:
                how = "forum+cluster"
            else:
                how = "conflict"
                extra["forum_user_id"] = uid
                if (w.get("cluster_conf") or 0.0) > override_conf:
                    uid = by_cluster  # strong acoustic evidence wins over mic bleed
        elif len(hits) > 1:  # crosstalk: let the voice decide
            if by_cluster in hits:
                uid, how = by_cluster, "cluster"
            else:
                uid, how = max(hits, key=hits.get), "forum-overlap"
        elif by_cluster is not None:
            uid = by_cluster
            how = "teacher-cluster" if teacher is not None and uid == teacher else "cluster"
        elif teacher is not None and cl is None:
            uid, how = teacher, "no-student-mic"
        else:
            uid, how = None, "none"
        out.append({**w, "user_id": uid, "how": how, **extra})
    return out


def smooth(words: list[dict], *, max_run_s: float = 0.6, max_gap_s: float = 1.2) -> list[dict]:
    """Relabel short runs (< max_run_s) sandwiched between identical neighbours when the
    run's best ``how`` is weaker than both neighbours'. Kills one-word flickers at turn
    edges. Relabelled words keep ``user_id_raw`` and get ``smoothed=True``."""
    words = [dict(w) for w in words]
    if len(words) < 3:
        return words
    runs = []  # [first, last] index ranges of equal user_id
    start = 0
    for i in range(1, len(words) + 1):
        if i == len(words) or words[i].get("user_id") != words[start].get("user_id"):
            runs.append((start, i - 1))
            start = i
    for r in range(1, len(runs) - 1):
        (_pf, pl), (cf, cl), (nf, _nl) = runs[r - 1], runs[r], runs[r + 1]
        left, right = words[pl].get("user_id"), words[nf].get("user_id")
        cur = words[cf].get("user_id")
        if left is None or left != right or cur == left:
            continue
        if words[cl]["end"] - words[cf]["start"] >= max_run_s:
            continue
        if (words[cf]["start"] - words[pl]["end"] > max_gap_s
                or words[nf]["start"] - words[cl]["end"] > max_gap_s):
            continue
        run_rank = max(HOW_RANK.get(words[i].get("how"), 0) for i in range(cf, cl + 1))
        nb_rank = min(HOW_RANK.get(words[pl].get("how"), 0), HOW_RANK.get(words[nf].get("how"), 0))
        if run_rank >= nb_rank:
            continue
        for i in range(cf, cl + 1):
            words[i]["user_id_raw"] = words[i].get("user_id")
            words[i]["user_id"] = left
            words[i]["smoothed"] = True
    return words


def how_mix(words: list[dict]) -> dict:
    counts = Counter(w.get("how", "none") for w in words)
    n = sum(counts.values()) or 1
    mix = {k: round(v / n, 4) for k, v in counts.most_common()}
    mix["smoothed"] = round(sum(1 for w in words if w.get("smoothed")) / n, 4)
    return mix
