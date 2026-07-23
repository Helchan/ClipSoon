from __future__ import annotations

import sqlite3
import statistics
import sys
import threading
import time
from pathlib import Path

import pytest

import clipsoon.core as core_module
from clipsoon.core import (
    AppSettings,
    ClipItem,
    ClipKind,
    HistoryRepository,
    JsonSettingsStore,
    ObservableSettings,
    ValidatedFileItem,
    format_bytes,
    valid_hotkey,
)
from clipsoon.search import SearchEngine, normalize, rank_items, score_text


class FakeClock:
    def __init__(self, value: float = 1_700_000_000.0) -> None:
        self.value = value

    def now(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def item(
    item_id: str,
    text: str,
    *,
    updated: float = 100,
    pinned: bool = False,
    kind: ClipKind = ClipKind.TEXT,
) -> ClipItem:
    return ClipItem(
        id=item_id,
        kind=kind,
        content_hash=f"hash-{item_id}",
        created_at=updated,
        updated_at=updated,
        text=text if kind is ClipKind.TEXT else "",
        files=(text,) if kind is ClipKind.FILES else (),
        pinned=pinned,
    )


def test_clip_item_presentations_and_bytes() -> None:
    text = item("t", "\n Hello world\nsecond")
    files = ClipItem("f", ClipKind.FILES, "f", 1, 1, files=("/tmp/a.txt", "/tmp/b.txt"))
    image = ClipItem("i", ClipKind.IMAGE, "i", 1, 1, width=640, height=480, byte_size=1536)
    assert text.title == "Hello world"
    assert files.title == "a.txt 及另外 1 个文件"
    assert "640 × 480" in image.title
    assert "1.5 KB" in image.detail
    assert format_bytes(0) == "0 B"
    assert format_bytes(2 * 1024**2) == "2.0 MB"
    assert format_bytes(3 * 1024**3) == "3.0 GB"
    assert text.with_pin(True).pinned
    assert "Hello world" in text.searchable_text
    assert "/tmp/a.txt" in files.searchable_text
    assert "image" in image.searchable_text
    assert ClipItem("e", ClipKind.FILES, "e", 1, 1).title == "文件"
    assert ClipItem("i", ClipKind.IMAGE, "i", 1, 1).detail == "PNG 图片"


def test_settings_round_trip_validation_and_observation(tmp_path: Path) -> None:
    store = JsonSettingsStore(tmp_path / "settings.json")
    observed: list[AppSettings] = []
    settings = ObservableSettings(store)
    unsubscribe = settings.subscribe(observed.append)
    value = settings.update(
        hotkey="double:shift",
        max_history_items=1,
        paste_delay_ms=9_999,
        remember_selection=True,
        selection_memory_seconds=999,
        launch_at_login=True,
        panel_x=-320,
        panel_y=240,
    )
    assert value.hotkey == "double:shift"
    assert value.max_history_items == 50
    assert value.paste_delay_ms == 2_000
    assert value.remember_selection
    assert value.selection_memory_seconds == 300
    assert value.launch_at_login
    assert (value.panel_x, value.panel_y) == (-320, 240)
    assert store.load() == value
    assert observed == [value]
    unsubscribe()
    settings.update(theme="not-a-theme", hotkey="bad")
    assert settings.value.theme == "system"
    assert settings.value.hotkey == "double:ctrl"


@pytest.mark.parametrize("payload", ["[1, 2]", "{bad", "null", '"string"'])
def test_corrupt_or_wrong_shape_settings_fall_back(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "settings.json"
    path.write_text(payload, encoding="utf-8")
    assert JsonSettingsStore(path).load() == AppSettings()


def test_hotkey_validation() -> None:
    assert valid_hotkey("double:ctrl")
    assert valid_hotkey("combo:ctrl+shift+v")
    assert not valid_hotkey("combo:v")
    assert not valid_hotkey("double:space")
    assert not valid_hotkey("no-prefix")
    assert AppSettings(double_tap_interval_ms="bad").validated().double_tap_interval_ms == 180
    assert AppSettings().selection_memory_seconds == 3
    assert AppSettings(selection_memory_seconds=0).validated().selection_memory_seconds == 1
    assert AppSettings(panel_x="bad", panel_y=None).validated().panel_x is None
    assert AppSettings(panel_x=200_000).validated().panel_x == 100_000


def test_search_contract_exact_prefix_substring_subsequence_and_rejection() -> None:
    items = [
        item("exact", "invoice 2026", updated=1),
        item("prefix", "invoice 2026 final notes", updated=999),
        item("substring", "archive invoice 2026 paid", updated=999),
        item("subsequence", "i_n_v_o_i_c_e 2_0_2_6", updated=999),
        item("noise", "meeting notes", updated=999),
    ]
    ranked = rank_items(items, "invoice 2026", now=1_000)
    assert [result.item.id for result in ranked] == ["exact", "prefix", "substring", "subsequence"]
    assert not rank_items([item("noise", "memo notes")], "in", now=1_000)


def test_search_unicode_filter_browse_and_stable_tie_break() -> None:
    text = item("b", "发布计划 ＡＢＣ", updated=10, pinned=True)
    file_item = item("a", "/tmp/abc.txt", updated=20, kind=ClipKind.FILES)
    engine = SearchEngine([file_item, text])
    assert normalize("  ＡｂＣ  ") == "  abc  "
    assert engine.rank("abc", now=20)[0].item.id == "b"
    assert engine.rank("abc", now=20, kind=ClipKind.FILES)[0].item.id == "a"
    assert engine.rank("", now=20)[0].item.id == "a"
    assert len(engine.rank("abc", now=20, limit=1)) == 1


def test_search_source_app_and_ordered_match_rejection() -> None:
    sourced = ClipItem("s", ClipKind.TEXT, "s", 1, 1, text="payload", source_app="Visual Studio Code")
    unrelated = item("u", "completely unrelated material")
    results = SearchEngine([sourced, unrelated]).rank("visual studio", now=2)
    assert results[0].item.id == "s"
    assert not SearchEngine([unrelated]).rank("zzzxq", now=2)
    assert score_text("abc", "abc") > score_text("abc", "xabcx")
    assert score_text("abc", "xabcx") > score_text("abc", "prefix abc long suffix")
    assert score_text("abc", "a-b-c") > score_text("abc", "a--b--c")
    assert score_text("abc", "acb") is None
    assert score_text("a  b", "a b") is None


def test_search_chooses_globally_best_continuity_alignment() -> None:
    # The optimal path is 0, 1, 4, 5. A forward/backward greedy path can pick
    # 0, 2, 4, 5 and incorrectly lose one adjacent pair.
    expected = 5_000 * (4 / 6) + 3_500 * (2 / 3) + 1_500 * (4 / 6)
    assert score_text("abab", "abbaab") == pytest.approx(expected)


def test_search_performance_uses_cached_index() -> None:
    if sys.gettrace() is not None:
        pytest.skip("coverage tracing invalidates wall-clock performance measurements")
    clips = [item(str(index), f"project invoice {index} final release notes") for index in range(500)]
    engine = SearchEngine(clips)
    samples = []
    for query in ["i", "in", "inv", "invoice", "invoice 42"] * 4:
        started = time.perf_counter()
        engine.rank(query, now=1_000, limit=500)
        samples.append((time.perf_counter() - started) * 1_000)
    p95 = statistics.quantiles(samples, n=20)[18]
    assert p95 < 50, f"search p95 was {p95:.1f} ms"


def test_repository_text_file_image_dedup_and_persistence(tmp_path: Path) -> None:
    clock = FakeClock()
    repo = HistoryRepository(tmp_path, clock=clock)
    first = repo.add_text("Text\n", "Editor")
    clock.advance(5)
    duplicate = repo.add_text("Text\n", "Browser")
    different = repo.add_text("text\n")
    assert duplicate.id == first.id
    assert duplicate.updated_at == clock.now()
    assert duplicate.source_app == "Browser"
    assert different.id != first.id

    path = tmp_path / "file name.txt"
    path.write_text("abc", encoding="utf-8")
    files = repo.add_files([str(path), str(tmp_path)])
    assert files.files == (str(path.absolute()), str(tmp_path.absolute()))
    assert files.byte_size == 3

    png = b"not-decoded-here-but-lossless-payload"
    image = repo.add_image(png, 2, 3)
    assert Path(image.image_path).read_bytes() == png
    assert repo.add_image(png, 2, 3).id == image.id
    repo.set_pinned(first.id, True)
    repo.mark_used(first.id)
    repo.close()

    reopened = HistoryRepository(tmp_path, clock=clock)
    restored = reopened.get(first.id)
    assert restored and restored.pinned and restored.use_count == 1
    assert reopened.delete(image.id)
    assert not reopened.delete("missing")
    assert not Path(image.image_path).exists()
    assert len(reopened.list_items(limit=1)) == 1
    reopened.close()


def test_repository_rejects_empty_inputs_and_removes_startup_orphan(tmp_path: Path) -> None:
    image_dir = tmp_path / "images"
    image_dir.mkdir(parents=True)
    orphan = image_dir / "orphan.png"
    orphan.write_bytes(b"orphan")
    temporary = image_dir / ".stale.tmp"
    temporary.write_bytes(b"temp")
    repo = HistoryRepository(tmp_path)
    assert not orphan.exists()
    assert not temporary.exists()
    with pytest.raises(ValueError):
        repo.add_text("")
    with pytest.raises(ValueError):
        repo.add_files([])
    with pytest.raises(ValueError):
        repo.add_image(b"", 1, 1)
    repo.close()


def test_repository_migrates_existing_history_with_revision_column(tmp_path: Path) -> None:
    database = sqlite3.connect(tmp_path / "history.sqlite3")
    database.execute(
        """
        CREATE TABLE clips (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            content_hash TEXT NOT NULL UNIQUE,
            text_content TEXT NOT NULL DEFAULT '',
            file_paths TEXT NOT NULL DEFAULT '[]',
            image_name TEXT NOT NULL DEFAULT '',
            mime_type TEXT NOT NULL DEFAULT '',
            width INTEGER NOT NULL DEFAULT 0,
            height INTEGER NOT NULL DEFAULT 0,
            byte_size INTEGER NOT NULL DEFAULT 0,
            source_app TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            pinned INTEGER NOT NULL DEFAULT 0,
            use_count INTEGER NOT NULL DEFAULT 0,
            last_used_at REAL NOT NULL DEFAULT 0
        )
        """
    )
    database.commit()
    database.close()

    repo = HistoryRepository(tmp_path)
    columns = {
        str(row["name"])
        for row in repo._connection.execute("PRAGMA table_info(clips)").fetchall()
    }

    assert "revision" in columns
    assert repo.add_text("after migration").text == "after migration"
    repo.close()


def test_image_write_failure_does_not_leave_orphan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = HistoryRepository(tmp_path)

    def fail(**_kwargs):
        raise RuntimeError("database failed")

    monkeypatch.setattr(repo, "_upsert", fail)
    with pytest.raises(RuntimeError, match="database failed"):
        repo.add_image(b"png", 1, 1)
    assert not list(repo.image_dir.iterdir())
    repo.close()


def test_cleanup_keeps_pins_and_reports_actual_count(tmp_path: Path) -> None:
    clock = FakeClock()
    repo = HistoryRepository(tmp_path, clock=clock)
    ids = []
    for index in range(5):
        ids.append(repo.add_text(f"item {index}").id)
        clock.advance(86_400)
    for item_id in ids[:3]:
        repo.set_pinned(item_id, True)
    deleted = repo.cleanup(max_items=2, retention_days=2)
    remaining = repo.list_items()
    assert deleted == 2
    assert {clip.id for clip in remaining} == set(ids[:3])
    assert all(clip.pinned for clip in remaining)
    repo.close()


def test_cleanup_retention_and_clear_unpinned(tmp_path: Path) -> None:
    clock = FakeClock()
    repo = HistoryRepository(tmp_path, clock=clock)
    old = repo.add_text("old")
    clock.advance(4 * 86_400)
    recent = repo.add_text("recent")
    assert repo.cleanup(max_items=50, retention_days=2) == 1
    assert repo.get(old.id) is None
    assert repo.clear_unpinned() == 1
    assert repo.get(recent.id) is None
    repo.close()


def test_batch_delete_and_clear_all(tmp_path: Path) -> None:
    repo = HistoryRepository(tmp_path)
    first = repo.add_text("first")
    second = repo.add_text("second")
    third = repo.add_text("third")
    repo.set_pinned(third.id, True)

    assert repo.delete_many((first.id, second.id, first.id, "missing")) == 2
    assert repo.get(first.id) is None
    assert repo.clear_all() == 1
    assert repo.list_items() == []
    repo.close()


def test_prune_missing_file_items_keeps_complete_files_and_directories(tmp_path: Path) -> None:
    repo = HistoryRepository(tmp_path)
    file_path = tmp_path / "available.txt"
    file_path.write_text("available", encoding="utf-8")
    directory = tmp_path / "available-directory"
    directory.mkdir()
    complete = repo.add_files((str(file_path), str(directory)))
    text = repo.add_text("unrelated")

    assert repo.prune_missing_file_items() == ()
    assert repo.get(complete.id) == complete
    assert repo.get(text.id) == text
    repo.close()


def test_prune_missing_file_items_removes_pinned_missing_file(tmp_path: Path) -> None:
    repo = HistoryRepository(tmp_path)
    file_path = tmp_path / "deleted.txt"
    file_path.write_text("deleted", encoding="utf-8")
    missing = repo.add_files((str(file_path),))
    repo.set_pinned(missing.id, True)
    file_path.unlink()

    assert repo.prune_missing_file_items() == (missing.id,)
    assert repo.get(missing.id) is None
    repo.close()


def test_prune_missing_file_items_removes_whole_multi_file_record(tmp_path: Path) -> None:
    repo = HistoryRepository(tmp_path)
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    batch = repo.add_files((str(first), str(second)))
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("unrelated", encoding="utf-8")
    preserved = repo.add_files((str(unrelated),))
    second.unlink()

    assert repo.prune_missing_file_items((batch.id,)) == (batch.id,)
    assert repo.get(batch.id) is None
    assert repo.get(preserved.id) == preserved
    repo.close()


def test_prune_missing_file_items_treats_not_a_directory_as_missing(tmp_path: Path) -> None:
    repo = HistoryRepository(tmp_path)
    parent = tmp_path / "parent"
    parent.mkdir()
    file_path = parent / "child.txt"
    file_path.write_text("child", encoding="utf-8")
    item = repo.add_files((str(file_path),))
    file_path.unlink()
    parent.rmdir()
    parent.write_text("no longer a directory", encoding="utf-8")

    assert repo.prune_missing_file_items() == (item.id,)
    assert repo.get(item.id) is None
    repo.close()


def test_prune_missing_file_items_keeps_indeterminate_os_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = HistoryRepository(tmp_path)
    file_path = tmp_path / "temporarily-unavailable.txt"
    file_path.write_text("available", encoding="utf-8")
    item = repo.add_files((str(file_path),))
    original_stat = Path.stat

    def guarded_stat(path: Path, *args, **kwargs):
        if path == file_path:
            raise PermissionError("temporarily unavailable")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", guarded_stat)

    assert repo.prune_missing_file_items() == ()
    assert repo.get(item.id) == item
    repo.close()


@pytest.mark.parametrize(
    ("file_path", "storage_root"),
    [
        (r"Z:\projects\missing.txt", "Z:\\"),
        (r"\\server\share\projects\missing.txt", "\\\\server\\share\\"),
        ("/Volumes/Archive/projects/missing.txt", "/Volumes/Archive"),
    ],
)
def test_file_missing_probe_preserves_items_when_storage_root_is_unavailable(
    file_path: str,
    storage_root: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable_stat(path: Path, *_args, **_kwargs):
        if str(path) in {file_path, storage_root}:
            raise FileNotFoundError(str(path))
        raise AssertionError(f"unexpected path probe: {path}")

    monkeypatch.setattr(Path, "stat", unavailable_stat)

    assert not core_module._file_path_is_definitively_missing(file_path)


def test_file_missing_probe_removes_unc_child_when_share_is_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_path = r"\\server\share\projects\deleted.txt"
    storage_root = "\\\\server\\share\\"
    probes: list[str] = []

    def reachable_root_stat(path: Path, *_args, **_kwargs):
        probes.append(str(path))
        if str(path) == file_path:
            raise FileNotFoundError(str(path))
        if str(path) == storage_root:
            return object()
        raise AssertionError(f"unexpected path probe: {path}")

    monkeypatch.setattr(Path, "stat", reachable_root_stat)

    assert core_module._file_path_is_definitively_missing(file_path)
    assert probes == [file_path, storage_root, file_path, storage_root]


def test_file_missing_probe_preserves_child_that_recovers_on_second_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_path = r"\\server\share\projects\available.txt"
    storage_root = "\\\\server\\share\\"
    file_probes = 0

    def recovered_stat(path: Path, *_args, **_kwargs):
        nonlocal file_probes
        if str(path) == file_path:
            file_probes += 1
            if file_probes == 1:
                raise FileNotFoundError(str(path))
            return object()
        if str(path) == storage_root:
            return object()
        raise AssertionError(f"unexpected path probe: {path}")

    monkeypatch.setattr(Path, "stat", recovered_stat)

    assert not core_module._file_path_is_definitively_missing(file_path)
    assert file_probes == 2


def test_file_missing_probe_preserves_child_if_root_disconnects_during_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_path = r"\\server\share\projects\unknown.txt"
    storage_root = "\\\\server\\share\\"
    root_probes = 0

    def disconnecting_stat(path: Path, *_args, **_kwargs):
        nonlocal root_probes
        if str(path) == file_path:
            raise FileNotFoundError(str(path))
        if str(path) == storage_root:
            root_probes += 1
            if root_probes == 2:
                raise FileNotFoundError(str(path))
            return object()
        raise AssertionError(f"unexpected path probe: {path}")

    monkeypatch.setattr(Path, "stat", disconnecting_stat)

    assert not core_module._file_path_is_definitively_missing(file_path)
    assert root_probes == 2


def test_prune_missing_file_items_does_not_delete_concurrently_refreshed_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    repo = HistoryRepository(tmp_path, clock=clock)
    file_path = tmp_path / "recreated.txt"
    file_path.write_text("old", encoding="utf-8")
    item = repo.add_files((str(file_path),), "old source")
    file_path.unlink()
    scan_started = threading.Event()
    release_scan = threading.Event()

    def delayed_missing(_path: str) -> bool:
        scan_started.set()
        assert release_scan.wait(2)
        return True

    monkeypatch.setattr(core_module, "_file_path_is_definitively_missing", delayed_missing)
    removed: list[tuple[str, ...]] = []
    sweep = threading.Thread(target=lambda: removed.append(repo.prune_missing_file_items()))
    sweep.start()
    assert scan_started.wait(1)

    file_path.write_text("fresh", encoding="utf-8")
    refreshed = repo.add_files((str(file_path),), "fresh source")
    release_scan.set()
    sweep.join(2)

    assert not sweep.is_alive()
    assert refreshed.id == item.id
    assert removed == [()]
    assert repo.get(item.id) == refreshed
    repo.close()


def test_validate_file_item_retries_after_concurrent_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    repo = HistoryRepository(tmp_path, clock=clock)
    file_path = tmp_path / "validated-after-refresh.txt"
    file_path.write_text("old", encoding="utf-8")
    item = repo.add_files((str(file_path),), "old source")
    file_path.unlink()
    first_scan_started = threading.Event()
    release_first_scan = threading.Event()
    original_missing = core_module._file_path_is_definitively_missing
    calls = 0

    def delayed_first_scan(path: str) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_scan_started.set()
            assert release_first_scan.wait(2)
            return True
        return original_missing(path)

    monkeypatch.setattr(core_module, "_file_path_is_definitively_missing", delayed_first_scan)
    validated: list[ValidatedFileItem | None] = []
    validation = threading.Thread(
        target=lambda: validated.append(repo.validate_file_item(item.id))
    )
    validation.start()
    assert first_scan_started.wait(1)

    file_path.write_text("fresh", encoding="utf-8")
    refreshed = repo.add_files((str(file_path),), "fresh source")
    release_first_scan.set()
    validation.join(2)

    assert not validation.is_alive()
    assert len(validated) == 1
    assert validated[0] is not None
    assert validated[0].item == refreshed
    assert repo.get(item.id) == refreshed
    repo.close()


def test_concurrent_same_content_is_one_row(tmp_path: Path) -> None:
    repo = HistoryRepository(tmp_path)
    threads = [threading.Thread(target=repo.add_text, args=("same",)) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(repo.list_items()) == 1
    repo.close()
