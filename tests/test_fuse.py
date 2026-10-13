import pytest
from conftest import words_from_intervals

from scribe_core.fuse import (
    HOW_RANK,
    cluster_to_user,
    fuse,
    how_mix,
    overlapping,
    smooth,
    teacher_cluster,
    teacher_clusters,
)


def V(uid, s, e):
    return {"start": s, "end": e, "user_id": uid}


def W(s, e, cluster=None, conf=0.9, text="x", cluster_conf=0.5):
    return {"start": s, "end": e, "text": text, "conf": conf, "cluster": cluster,
            "cluster_conf": cluster_conf}


def test_overlapping_handles_long_and_nested_intervals():
    voice = [V("A", 0, 100)] + [V("B", 1 + i, 1.5 + i) for i in range(20)]
    hits = overlapping([(50.1, 50.2), (5.0, 5.2), (200, 201)], voice)
    assert [v["user_id"] for v in hits[0]] == ["A"]  # long interval found past 20 shorter ones
    assert sorted(v["user_id"] for v in hits[1]) == ["A", "B"]
    assert hits[2] == []


def test_hungarian_mapping_with_split_cluster_and_unmapped_cluster():
    voice = [V("A", 0, 30), V("B", 30, 60), V("A", 60, 90)]
    words = (words_from_intervals([(0, 30, 0)])            # A -> cluster 0
             + words_from_intervals([(30, 60, 1)])         # B -> cluster 1
             + words_from_intervals([(60, 90, 2)])         # A again, split into cluster 2
             + words_from_intervals([(95, 100, 3)]))       # noise nobody owns
    report = {}
    mapping = cluster_to_user(words, voice, report=report)
    assert mapping[0] == "A" and mapping[1] == "B"
    assert mapping[2] == "A"          # split cluster joins its owner
    assert 3 not in mapping           # no Forum overlap: stays anonymous
    by_cluster = {c["cluster"]: c for c in report["clusters"]}
    assert by_cluster[3]["mapped_to"] is None


def test_merged_cluster_is_left_unmapped():
    voice = [V("A", 0, 10), V("B", 10, 20)]
    words = words_from_intervals([(0, 20, 0)])  # one cluster covering two people 50/50
    words += words_from_intervals([(25, 26, 1)])
    voice.append(V("C", 25, 26))
    mapping = cluster_to_user(words, voice, min_share=0.6)
    assert 0 not in mapping


def test_fuse_how_branches():
    voice = [V("A", 0, 5), V("B", 10, 15), V("C", 12, 14), V("A", 20, 25), V("B", 30, 35)]
    mapping = {0: "A", 1: "B", 2: "C"}
    words = [
        W(1.0, 1.3, cluster=0),                         # forum+cluster
        W(2.0, 2.3, cluster=None),                      # forum (no cluster opinion)
        W(7.0, 7.3, cluster=1),                         # cluster (no Forum coverage)
        W(12.5, 12.8, cluster=2),                       # crosstalk B+C, cluster says C
        W(12.9, 13.1, cluster=0),                       # crosstalk, cluster A not in hits
        W(21.0, 21.3, cluster=1, cluster_conf=0.5),     # conflict, weak acoustics: keep A
        W(22.0, 22.3, cluster=1, cluster_conf=0.95),    # conflict, strong acoustics: B wins
        W(40.0, 40.3, cluster=None),                    # nothing: Unattributed
        W(31.0, 31.3, cluster=7),                       # unmapped cluster -> forum
    ]
    out = fuse(words, voice, mapping)
    got = [(w["user_id"], w["how"]) for w in out]
    assert got[0] == ("A", "forum+cluster")
    assert got[1] == ("A", "forum")
    assert got[2] == ("B", "cluster")
    assert got[3] == ("C", "cluster")
    assert got[4][1] == "forum-overlap" and got[4][0] in ("B", "C")
    assert got[5] == ("A", "conflict") and out[5]["forum_user_id"] == "A"
    assert got[6] == ("B", "conflict") and out[6]["forum_user_id"] == "A"
    assert got[7] == (None, "none")   # never "Professor"
    assert got[8] == ("B", "forum")


def test_forum_only_word_spanning_two_speakers_goes_to_larger_overlap():
    voice = [V("A", 0, 10.0), V("B", 10.0, 20)]
    out = fuse([W(9.2, 10.9)], voice, {})  # 0.8 s with A, 0.9 s with B (+edge both)
    assert out[0]["how"] == "forum-overlap" and out[0]["user_id"] == "B"


def test_edge_slack_catches_jittered_word_and_gap_is_unattributed():
    voice = [V("A", 0, 10.0), V("B", 20, 30)]
    out = fuse([W(10.1, 10.3), W(14, 15)], voice, {})
    assert (out[0]["user_id"], out[0]["how"]) == ("A", "forum")  # within EDGE of A's end
    assert (out[1]["user_id"], out[1]["how"]) == (None, "none")


def test_zero_length_words_still_attribute():
    out = fuse([W(5.0, 5.0)], [V("A", 0, 10)], {})
    assert out[0]["user_id"] == "A"


def test_smoothing_relabels_weak_short_flicker_only():
    words = [
        {"start": 0.0, "end": 0.3, "user_id": "A", "how": "forum+cluster"},
        {"start": 0.35, "end": 0.6, "user_id": "A", "how": "forum+cluster"},
        {"start": 0.65, "end": 0.9, "user_id": "B", "how": "forum-overlap"},  # flicker
        {"start": 0.95, "end": 1.2, "user_id": "A", "how": "forum+cluster"},
        {"start": 1.3, "end": 1.6, "user_id": None, "how": "none"},            # flicker
        {"start": 1.65, "end": 1.9, "user_id": "A", "how": "forum"},
        {"start": 2.0, "end": 3.0, "user_id": "C", "how": "forum-overlap"},   # too long
        {"start": 3.05, "end": 3.3, "user_id": "A", "how": "forum"},
        {"start": 3.35, "end": 3.6, "user_id": "D", "how": "forum+cluster"},  # stronger
        {"start": 3.65, "end": 3.9, "user_id": "A", "how": "forum"},
    ]
    out = smooth(words)
    assert out[2]["user_id"] == "A" and out[2]["smoothed"] and out[2]["user_id_raw"] == "B"
    assert out[4]["user_id"] == "A" and out[4]["smoothed"]
    assert out[6]["user_id"] == "C" and not out[6].get("smoothed")
    assert out[8]["user_id"] == "D" and not out[8].get("smoothed")
    assert words[2]["user_id"] == "B"  # input not mutated
    mix = how_mix(out)
    assert mix["smoothed"] == pytest.approx(0.2)


# ---- the professor has no voice events (real Forum data: 0 of 717 classes) -----------------


def professor_class():
    """Professor (cluster 0) lectures with no voice events; students A and B answer on mic.
    A's mic picks up the professor at 120-122 (bleed); a guest (cluster 3) speaks 5 s off-mic."""
    voice = [V("A", 60, 75), V("B", 80, 95), V("A", 120, 122)]
    words = (words_from_intervals([(0, 58, 0)])
             + words_from_intervals([(60, 75, 1)])
             + words_from_intervals([(80, 95, 2)])
             + words_from_intervals([(100, 200, 0)])
             + words_from_intervals([(210, 215, 3)]))
    return words, voice


def test_professor_cluster_is_the_big_one_heard_with_no_student_mic():
    words, voice = professor_class()
    assert teacher_cluster(words, voice) == 0
    report = {}
    mapping = cluster_to_user(words, voice, teacher="T", report=report)
    assert mapping == {0: "T", 1: "A", 2: "B"}   # the 5 s guest is never the professor
    by_cluster = {c["cluster"]: c for c in report["clusters"]}
    assert by_cluster[0]["mapped_to"] == "T" and by_cluster[0]["no_mic_share"] > 0.9
    assert by_cluster[1]["no_mic_share"] < 0.1


def test_without_the_teacher_the_professor_becomes_a_split_of_a_student():
    # The bug this guards against: only the 2 s of bleed overlap a mic, so the professor's
    # whole cluster looks like 100% student A.
    words, voice = professor_class()
    assert cluster_to_user(words, voice)[0] == "A"


def test_students_are_never_the_professor():
    voice = [V("A", 0, 60), V("B", 60, 120)]
    words = words_from_intervals([(0, 60, 0), (60, 120, 1)])
    assert teacher_cluster(words, voice) is None
    assert cluster_to_user(words, voice, teacher="T") == {0: "A", 1: "B"}


def test_fuse_labels_professor_words_and_keeps_bleed_a_conflict():
    words, voice = professor_class()
    mapping = cluster_to_user(words, voice, teacher="T")
    probe = [W(30.0, 30.3, cluster=0),                      # lecturing, no mic on
             W(121.0, 121.3, cluster=0, cluster_conf=0.95),  # bleed on A's mic, clear voice
             W(121.4, 121.7, cluster=0, cluster_conf=0.5),   # bleed, unsure voice: keep A
             W(65.0, 65.3, cluster=1),                       # A answering
             W(212.0, 212.3, cluster=3)]                     # guest
    out = fuse(probe, voice, mapping, teacher="T")
    assert [(w["user_id"], w["how"]) for w in out] == [
        ("T", "teacher-cluster"), ("T", "conflict"), ("A", "conflict"),
        ("A", "forum+cluster"), (None, "none")]
    assert set(w["how"] for w in out) <= set(HOW_RANK)


def test_without_clusters_no_student_mic_means_professor():
    voice = [V("A", 10, 15)]
    words = [W(2.0, 2.3), W(12.0, 12.3), W(30.0, 30.3)]
    out = fuse(words, voice, teacher="T")
    assert [(w["user_id"], w["how"]) for w in out] == [
        ("T", "no-student-mic"), ("A", "forum"), ("T", "no-student-mic")]
    assert [w["how"] for w in fuse(words, voice)] == ["none", "forum", "none"]  # no teacher id


def test_a_split_professor_is_found_but_a_video_clip_is_not():
    """Diarizers split long talkers: the professor's second voice is spread over the class.
    A 3-minute video (or a guest) is one block with no mic, and stays anonymous."""
    voice = [V("A", 290 + 600 * k, 300 + 600 * k) for k in range(8)]
    lecture = [(600 * k, 280 + 600 * k, 0 if k % 3 else 5) for k in range(8)]  # 5 = 2nd voice
    video = [(4900, 5080, 7)]
    words = words_from_intervals(lecture + [(290 + 600 * k, 300 + 600 * k, 1) for k in range(8)]
                                 + video)
    assert teacher_clusters(words, voice) == [0, 5]
    mapping = cluster_to_user(words, voice, teacher="T")
    assert mapping[0] == mapping[5] == "T" and mapping[1] == "A" and 7 not in mapping
