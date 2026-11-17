from mangarr.tomes.plan import plan_tomes

VOLS = {1: [1, 2, 3], 2: [4, 5, 6], 3: [7, 8]}


def test_ready_when_all_local():
    ready, partial = plan_tomes(VOLS, set(range(1, 9)))
    assert [v for v, _ in ready] == [1, 2, 3] and not partial


def test_missing_local_chapter_blocks_volume():
    ready, partial = plan_tomes(VOLS, {1, 2, 3, 4, 5})
    assert [v for v, _ in ready] == [1]
    assert partial[0][0] == 2 and "manquants" in partial[0][1]


def test_gap_in_mangadex_data():
    ready, partial = plan_tomes({1: [1, 2, 4]}, {1, 2, 4})
    assert not ready and "trous" in partial[0][1]


def test_strict_neighbours_not_contiguous():
    vols = {1: [1, 2], 2: [5, 6]}   # chapitres 3-4 rattachés à aucun tome
    ready, partial = plan_tomes(vols, {1, 2, 5, 6}, strict=True)
    assert not ready and len(partial) == 2
    ready, _ = plan_tomes(vols, {1, 2, 5, 6}, strict=False)
    assert [v for v, _ in ready] == [1, 2]


def test_volume_without_local_chapters_ignored():
    ready, partial = plan_tomes({1: [1, 2], 2: [3, 4]}, {3, 4})
    assert [v for v, _ in ready] == [2] and not partial
