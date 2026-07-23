"""Fast core: data model, settings, ranked search, and SQLite persistence."""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, fields, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol


class ClipKind(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    FILES = "files"


@dataclass(frozen=True, slots=True)
class ClipItem:
    id: str
    kind: ClipKind
    content_hash: str
    created_at: float
    updated_at: float
    text: str = ""
    files: tuple[str, ...] = ()
    image_path: str = ""
    mime_type: str = ""
    width: int = 0
    height: int = 0
    byte_size: int = 0
    source_app: str = ""
    pinned: bool = False
    use_count: int = 0
    last_used_at: float = 0.0

    @property
    def title(self) -> str:
        if self.kind is ClipKind.TEXT:
            first_line = next((line.strip() for line in self.text.splitlines() if line.strip()), "")
            return first_line or "空白文本"
        if self.kind is ClipKind.IMAGE:
            size = f"{self.width} × {self.height}" if self.width and self.height else "图片"
            return f"图片 {size}"
        if not self.files:
            return "文件"
        first = Path(self.files[0]).name or self.files[0]
        extra = len(self.files) - 1
        return first if extra == 0 else f"{first} 及另外 {extra} 个文件"

    @property
    def detail(self) -> str:
        if self.kind is ClipKind.TEXT:
            compact = " ".join(self.text.split())
            return compact[:280]
        if self.kind is ClipKind.IMAGE:
            parts = []
            if self.width and self.height:
                parts.append(f"{self.width} × {self.height}")
            if self.byte_size:
                parts.append(format_bytes(self.byte_size))
            return " · ".join(parts) or "PNG 图片"
        return "\n".join(self.files)

    @property
    def searchable_text(self) -> str:
        if self.kind is ClipKind.TEXT:
            body = self.text
        elif self.kind is ClipKind.FILES:
            body = " ".join(self.files)
        else:
            body = f"图片 image {self.width}x{self.height}"
        # For one-line text title == body. Do not duplicate it: exact searches
        # must remain distinguishable from prefix matches.
        return " ".join(part for part in (body, self.source_app) if part)

    def with_pin(self, pinned: bool) -> ClipItem:
        return replace(self, pinned=pinned)


@dataclass(frozen=True, slots=True)
class ValidatedFileItem:
    item: ClipItem
    revision: int


class FileItemClaimStatus(StrEnum):
    MISSING = "missing"
    REFRESHED = "refreshed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class FileItemClaim:
    status: FileItemClaimStatus
    item: ClipItem | None = None


def format_bytes(size: int) -> str:
    value = float(max(size, 0))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


class Clock(Protocol):
    def now(self) -> float: ...


class HistoryStore(Protocol):
    def list_items(self, limit: int | None = None) -> list[ClipItem]: ...

    def get(self, item_id: str) -> ClipItem | None: ...

    def add_text(self, text: str, source_app: str = "") -> ClipItem: ...

    def add_files(self, paths: Sequence[str], source_app: str = "") -> ClipItem: ...

    def add_image(
        self,
        png: bytes,
        width: int,
        height: int,
        source_app: str = "",
    ) -> ClipItem: ...

    def mark_used(self, item_id: str) -> None: ...

    def set_pinned(self, item_id: str, pinned: bool) -> None: ...

    def delete(self, item_id: str) -> None: ...

    def prune_missing_file_items(
        self, item_ids: Sequence[str] | None = None
    ) -> tuple[str, ...]: ...

    def validate_file_item(self, item_id: str) -> ValidatedFileItem | None: ...

    def consume_validated_file_item(
        self,
        validated: ValidatedFileItem,
        consumer: Callable[[ClipItem], bool],
    ) -> FileItemClaim: ...

    def cleanup(self, max_items: int, retention_days: int) -> int: ...


class ClipboardAdapter(Protocol):
    def write_item(self, item: ClipItem) -> bool: ...


class ForegroundTarget(Protocol):
    def activate(self) -> bool: ...


class PasteAdapter(Protocol):
    def paste(self) -> bool: ...


# Settings -----------------------------------------------------------------

WINDOWS_DEFAULT_HOTKEY = "combo:ctrl+shift+space"


@dataclass(slots=True)
class AppSettings:
    hotkey: str = "double:ctrl"
    double_tap_interval_ms: int = 420
    max_history_items: int = 500
    retention_days: int = 90
    paste_delay_ms: int = 180
    paste_after_selection: bool = True
    hide_on_deactivate: bool = True
    capture_enabled: bool = True
    remember_selection: bool = False
    selection_memory_seconds: int = 3
    launch_at_login: bool = False
    panel_x: int | None = None
    panel_y: int | None = None
    theme: str = "system"

    def validated(self) -> AppSettings:
        return AppSettings(
            hotkey=self.hotkey if valid_hotkey(self.hotkey) else "double:ctrl",
            double_tap_interval_ms=_clamp(self.double_tap_interval_ms, 180, 900),
            max_history_items=_clamp(self.max_history_items, 50, 10_000),
            retention_days=_clamp(self.retention_days, 0, 3_650),
            paste_delay_ms=_clamp(self.paste_delay_ms, 60, 2_000),
            paste_after_selection=bool(self.paste_after_selection),
            hide_on_deactivate=bool(self.hide_on_deactivate),
            capture_enabled=bool(self.capture_enabled),
            remember_selection=bool(self.remember_selection),
            selection_memory_seconds=_clamp(self.selection_memory_seconds, 1, 300),
            launch_at_login=bool(self.launch_at_login),
            panel_x=_optional_coordinate(self.panel_x),
            panel_y=_optional_coordinate(self.panel_y),
            theme=self.theme if self.theme in {"system", "light", "dark"} else "system",
        )


class JsonSettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> AppSettings:
        if not self.path.exists():
            return AppSettings()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return AppSettings()
            allowed = {field.name for field in fields(AppSettings)}
            return AppSettings(**{key: value for key, value in data.items() if key in allowed}).validated()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return AppSettings()

    def save(self, settings: AppSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps(asdict(settings.validated()), ensure_ascii=False, indent=2)
        temporary.write_text(payload + "\n", encoding="utf-8")
        os.replace(temporary, self.path)


class ObservableSettings:
    def __init__(self, store: JsonSettingsStore) -> None:
        self._store = store
        self._value = store.load()
        self._listeners: list[Callable[[AppSettings], None]] = []

    @property
    def value(self) -> AppSettings:
        return self._value

    def subscribe(self, listener: Callable[[AppSettings], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def update(self, **changes: Any) -> AppSettings:
        values = asdict(self._value)
        values.update(changes)
        updated = AppSettings(**values).validated()
        self._store.save(updated)
        self._value = updated
        for listener in tuple(self._listeners):
            listener(self._value)
        return self._value


def valid_hotkey(value: str) -> bool:
    if value.startswith("double:"):
        return value.removeprefix("double:") in {"ctrl", "shift", "alt", "meta"}
    if not value.startswith("combo:"):
        return False
    keys = {part for part in value.removeprefix("combo:").split("+") if part}
    modifiers = {"ctrl", "shift", "alt", "meta"}
    return bool(keys & modifiers) and bool(keys - modifiers)


def _clamp(value: Any, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return minimum
    return min(maximum, max(minimum, parsed))


def _optional_coordinate(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return _clamp(int(value), -100_000, 100_000)
    except (TypeError, ValueError):
        return None


# Persistence ---------------------------------------------------------------


class SystemClock:
    def now(self) -> float:
        return time.time()


class HistoryRepository:
    def __init__(self, data_dir: Path, *, clock: Clock | None = None) -> None:
        self.data_dir = data_dir
        self.image_dir = data_dir / "images"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock or SystemClock()
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.data_dir / "history.sqlite3", timeout=10, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()
        self._remove_orphan_images()

    def _create_schema(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS clips (
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
                    last_used_at REAL NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_clips_recent
                    ON clips(pinned DESC, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_clips_kind ON clips(kind);
                """
            )
            columns = {
                str(row["name"])
                for row in self._connection.execute("PRAGMA table_info(clips)").fetchall()
            }
            if "revision" not in columns:
                self._connection.execute(
                    "ALTER TABLE clips ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
                )

    def list_items(self, limit: int | None = None) -> list[ClipItem]:
        sql = "SELECT * FROM clips ORDER BY pinned DESC, updated_at DESC, created_at DESC"
        parameters: tuple[int, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            parameters = (max(0, int(limit)),)
        with self._lock:
            rows = self._connection.execute(sql, parameters).fetchall()
        return [self._row_to_item(row) for row in rows]

    def get(self, item_id: str) -> ClipItem | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM clips WHERE id = ?", (item_id,)).fetchone()
        return None if row is None else self._row_to_item(row)

    def add_text(self, text: str, source_app: str = "") -> ClipItem:
        if not text:
            raise ValueError("empty text is not stored")
        payload = text.encode("utf-8")
        return self._upsert(
            kind=ClipKind.TEXT,
            content_hash=_digest(ClipKind.TEXT, payload),
            text=text,
            byte_size=len(payload),
            mime_type="text/plain",
            source_app=source_app,
        )

    def add_files(self, paths: Sequence[str], source_app: str = "") -> ClipItem:
        normalized = tuple(_absolute_path(path) for path in paths if path)
        if not normalized:
            raise ValueError("at least one file path is required")
        hash_paths = [os.path.normcase(os.path.normpath(path)) for path in normalized]
        payload = json.dumps(hash_paths, ensure_ascii=False, separators=(",", ":")).encode()
        return self._upsert(
            kind=ClipKind.FILES,
            content_hash=_digest(ClipKind.FILES, payload),
            files=normalized,
            byte_size=sum(_safe_file_size(Path(path)) for path in normalized),
            mime_type="text/uri-list",
            source_app=source_app,
        )

    def add_image(
        self,
        png: bytes,
        width: int,
        height: int,
        source_app: str = "",
    ) -> ClipItem:
        if not png:
            raise ValueError("empty image is not stored")
        digest = _digest(ClipKind.IMAGE, png)
        image_name = f"{digest.removeprefix('image:')}.png"
        destination = self.image_dir / image_name
        temporary: Path | None = None
        created = False
        if not destination.exists():
            temporary = self.image_dir / f".{image_name}.{uuid.uuid4().hex}.tmp"
            temporary.write_bytes(png)

        def finalize_image() -> None:
            nonlocal created
            if temporary is not None and not destination.exists():
                os.replace(temporary, destination)
                created = True

        try:
            return self._upsert(
                kind=ClipKind.IMAGE,
                content_hash=digest,
                image_name=image_name,
                byte_size=len(png),
                mime_type="image/png",
                width=max(0, int(width)),
                height=max(0, int(height)),
                source_app=source_app,
                before_commit=finalize_image,
            )
        except Exception:
            if created:
                destination.unlink(missing_ok=True)
            raise
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _upsert(
        self,
        *,
        kind: ClipKind,
        content_hash: str,
        text: str = "",
        files: Sequence[str] = (),
        image_name: str = "",
        mime_type: str = "",
        width: int = 0,
        height: int = 0,
        byte_size: int = 0,
        source_app: str = "",
        before_commit: Callable[[], None] | None = None,
    ) -> ClipItem:
        now, item_id = self._clock.now(), str(uuid.uuid4())
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT id FROM clips WHERE content_hash = ?", (content_hash,)
            ).fetchone()
            if existing is not None:
                item_id = str(existing["id"])
                self._connection.execute(
                    """
                    UPDATE clips
                    SET updated_at = ?, source_app = ?, text_content = ?, file_paths = ?,
                        image_name = ?, mime_type = ?, width = ?, height = ?, byte_size = ?,
                        revision = revision + 1
                    WHERE id = ?
                    """,
                    (
                        now,
                        source_app,
                        text,
                        json.dumps(list(files), ensure_ascii=False),
                        image_name,
                        mime_type,
                        width,
                        height,
                        byte_size,
                        item_id,
                    ),
                )
            else:
                self._connection.execute(
                    """
                    INSERT INTO clips (
                        id, kind, content_hash, text_content, file_paths, image_name,
                        mime_type, width, height, byte_size, source_app, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item_id,
                        kind.value,
                        content_hash,
                        text,
                        json.dumps(list(files), ensure_ascii=False),
                        image_name,
                        mime_type,
                        width,
                        height,
                        byte_size,
                        source_app,
                        now,
                        now,
                    ),
                )
            if before_commit is not None:
                before_commit()
        item = self.get(item_id)
        if item is None:  # pragma: no cover
            raise RuntimeError("stored clip could not be read back")
        return item

    def mark_used(self, item_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE clips
                SET use_count = use_count + 1, last_used_at = ?, revision = revision + 1
                WHERE id = ?
                """,
                (self._clock.now(), item_id),
            )

    def set_pinned(self, item_id: str, pinned: bool) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE clips SET pinned = ?, revision = revision + 1 WHERE id = ?",
                (int(pinned), item_id),
            )

    def delete(self, item_id: str) -> bool:
        return self.delete_many((item_id,)) == 1

    def delete_many(self, item_ids: Sequence[str]) -> int:
        unique_ids = tuple(dict.fromkeys(item_ids))
        if not unique_ids:
            return 0
        placeholders = ",".join("?" for _ in unique_ids)
        with self._lock, self._connection:
            rows = self._connection.execute(
                f"SELECT image_name FROM clips WHERE id IN ({placeholders})", unique_ids
            ).fetchall()
            self._connection.execute(f"DELETE FROM clips WHERE id IN ({placeholders})", unique_ids)
        for image_name in {str(row["image_name"]) for row in rows if row["image_name"]}:
            self._delete_image_if_orphan(image_name)
        return len(rows)

    def prune_missing_file_items(
        self, item_ids: Sequence[str] | None = None
    ) -> tuple[str, ...]:
        selected_ids = None if item_ids is None else tuple(dict.fromkeys(item_ids))
        if selected_ids == ():
            return ()
        sql = "SELECT id, file_paths, revision FROM clips WHERE kind = ?"
        parameters: list[object] = [ClipKind.FILES.value]
        if selected_ids is not None:
            placeholders = ",".join("?" for _ in selected_ids)
            sql += f" AND id IN ({placeholders})"
            parameters.extend(selected_ids)
        sql += " ORDER BY pinned DESC, updated_at DESC, created_at DESC"
        with self._lock:
            rows = self._connection.execute(sql, parameters).fetchall()

        missing_rows: list[tuple[str, int]] = []
        for row in rows:
            paths = _stored_file_paths(row)
            if paths is None:
                continue
            if any(_file_path_is_definitively_missing(path) for path in paths):
                missing_rows.append((str(row["id"]), int(row["revision"])))
        if not missing_rows:
            return ()

        deleted_ids: list[str] = []
        with self._lock, self._connection:
            for item_id, revision in missing_rows:
                cursor = self._connection.execute(
                    "DELETE FROM clips WHERE id = ? AND kind = ? AND revision = ?",
                    (item_id, ClipKind.FILES.value, revision),
                )
                if cursor.rowcount:
                    deleted_ids.append(item_id)
        return tuple(deleted_ids)

    def validate_file_item(self, item_id: str) -> ValidatedFileItem | None:
        # A revision compare-and-swap prevents an old filesystem scan from
        # deleting or returning a row refreshed by a concurrent clipboard
        # capture. The filesystem probe intentionally runs outside the DB lock.
        for _attempt in range(8):
            with self._lock:
                row = self._connection.execute(
                    "SELECT * FROM clips WHERE id = ? AND kind = ?",
                    (item_id, ClipKind.FILES.value),
                ).fetchone()
            if row is None:
                return None
            paths = _stored_file_paths(row)
            if paths is None:
                return None
            revision = int(row["revision"])
            missing = any(_file_path_is_definitively_missing(path) for path in paths)

            with self._lock, self._connection:
                current = self._connection.execute(
                    "SELECT * FROM clips WHERE id = ? AND kind = ?",
                    (item_id, ClipKind.FILES.value),
                ).fetchone()
                if current is None:
                    return None
                if int(current["revision"]) != revision:
                    continue
                if missing:
                    cursor = self._connection.execute(
                        "DELETE FROM clips WHERE id = ? AND kind = ? AND revision = ?",
                        (item_id, ClipKind.FILES.value, revision),
                    )
                    if cursor.rowcount:
                        return None
                    continue
                return ValidatedFileItem(self._row_to_item(current), revision)
        # Continuous mutation is not a safe basis for emitting CF_HDROP.
        return None

    def consume_validated_file_item(
        self,
        validated: ValidatedFileItem,
        consumer: Callable[[ClipItem], bool],
    ) -> FileItemClaim:
        # This short critical section is intentionally filesystem-free. It
        # closes the queued-signal window by preventing repository deletion or
        # refresh between the revision check and the clipboard write.
        with self._lock:
            current = self._connection.execute(
                "SELECT * FROM clips WHERE id = ? AND kind = ?",
                (validated.item.id, ClipKind.FILES.value),
            ).fetchone()
            if current is None:
                return FileItemClaim(FileItemClaimStatus.MISSING)
            item = self._row_to_item(current)
            if int(current["revision"]) != validated.revision:
                return FileItemClaim(FileItemClaimStatus.REFRESHED, item)
            accepted = consumer(item)
            return FileItemClaim(
                FileItemClaimStatus.ACCEPTED if accepted else FileItemClaimStatus.REJECTED,
                item,
            )

    def cleanup(self, max_items: int, retention_days: int) -> int:
        max_items, retention_days = max(1, int(max_items)), max(0, int(retention_days))
        with self._lock:
            remove_ids: list[str] = []
            if retention_days:
                cutoff = self._clock.now() - retention_days * 86_400
                rows = self._connection.execute(
                    "SELECT id FROM clips WHERE pinned = 0 AND updated_at < ?", (cutoff,)
                ).fetchall()
                remove_ids.extend(str(row["id"]) for row in rows)

            pinned_count = int(
                self._connection.execute("SELECT COUNT(*) FROM clips WHERE pinned = 1").fetchone()[0]
            )
            allowed_unpinned = max(0, max_items - pinned_count)
            excluded = set(remove_ids)
            unpinned = self._connection.execute(
                "SELECT id FROM clips WHERE pinned = 0 ORDER BY updated_at DESC"
            ).fetchall()
            kept = 0
            for row in unpinned:
                item_id = str(row["id"])
                if item_id in excluded:
                    continue
                if kept < allowed_unpinned:
                    kept += 1
                else:
                    remove_ids.append(item_id)

        deleted = 0
        for item_id in dict.fromkeys(remove_ids):
            deleted += int(self.delete(item_id))
        return deleted

    def clear_unpinned(self) -> int:
        return sum(int(self.delete(item.id)) for item in self.list_items() if not item.pinned)

    def clear_all(self) -> int:
        return self.delete_many(tuple(item.id for item in self.list_items()))

    def _delete_image_if_orphan(self, image_name: str) -> None:
        with self._lock:
            in_use = self._connection.execute(
                "SELECT 1 FROM clips WHERE image_name = ? LIMIT 1", (image_name,)
            ).fetchone()
        if in_use is None:
            (self.image_dir / image_name).unlink(missing_ok=True)

    def _remove_orphan_images(self) -> None:
        with self._lock:
            names = {
                str(row["image_name"])
                for row in self._connection.execute(
                    "SELECT image_name FROM clips WHERE image_name != ''"
                ).fetchall()
            }
        for path in self.image_dir.glob("*.png"):
            if path.name not in names:
                path.unlink(missing_ok=True)
        for path in self.image_dir.glob(".*.tmp"):
            path.unlink(missing_ok=True)

    def _row_to_item(self, row: sqlite3.Row) -> ClipItem:
        image_name = str(row["image_name"])
        return ClipItem(
            id=str(row["id"]),
            kind=ClipKind(str(row["kind"])),
            content_hash=str(row["content_hash"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            text=str(row["text_content"]),
            files=tuple(json.loads(str(row["file_paths"]))),
            image_path=str(self.image_dir / image_name) if image_name else "",
            mime_type=str(row["mime_type"]),
            width=int(row["width"]),
            height=int(row["height"]),
            byte_size=int(row["byte_size"]),
            source_app=str(row["source_app"]),
            pinned=bool(row["pinned"]),
            use_count=int(row["use_count"]),
            last_used_at=float(row["last_used_at"]),
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def _digest(kind: ClipKind, payload: bytes) -> str:
    return f"{kind.value}:{hashlib.sha256(payload).hexdigest()}"


def _absolute_path(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _safe_file_size(path: Path) -> int:
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def _file_path_is_definitively_missing(path: str) -> bool:
    try:
        Path(path).stat()
    except (FileNotFoundError, NotADirectoryError):
        storage_root = _file_storage_root(path)
        if storage_root is None or _same_storage_path(path, storage_root):
            return False
        if not _storage_root_is_reachable(storage_root):
            # A disconnected UNC share, mapped/removable Windows drive, or
            # unmounted macOS volume can report FileNotFoundError for every
            # child.  Preserve history until the storage root is reachable
            # and the individual path can be confirmed missing.
            return False
        try:
            Path(path).stat()
        except (FileNotFoundError, NotADirectoryError):
            # Confirm the root again after the second child probe. This closes
            # the race where a share briefly recovers between the first child
            # failure and root probe, then disconnects again.
            return _storage_root_is_reachable(storage_root)
        except OSError:
            return False
        return False
    except OSError:
        # Access denial, a temporarily unavailable device, or another
        # indeterminate I/O failure must not permanently erase history.
        return False
    return False


def _file_storage_root(path: str) -> str | None:
    windows_drive, _tail = ntpath.splitdrive(path)
    if windows_drive:
        return f"{windows_drive}\\"

    candidate = Path(path)
    parts = candidate.parts
    if len(parts) >= 3 and parts[0] == os.path.sep and parts[1] == "Volumes":
        return str(Path(os.path.sep, "Volumes", parts[2]))
    return candidate.anchor or None


def _storage_root_is_reachable(storage_root: str) -> bool:
    try:
        Path(storage_root).stat()
    except OSError:
        return False
    return True


def _same_storage_path(path: str, storage_root: str) -> bool:
    windows_drive, _tail = ntpath.splitdrive(path)
    if windows_drive:
        return ntpath.normcase(ntpath.normpath(path)) == ntpath.normcase(
            ntpath.normpath(storage_root)
        )
    return os.path.normcase(os.path.normpath(path)) == os.path.normcase(
        os.path.normpath(storage_root)
    )


def _stored_file_paths(row: sqlite3.Row) -> tuple[str, ...] | None:
    try:
        paths = json.loads(str(row["file_paths"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(paths, list) or not paths or not all(isinstance(path, str) for path in paths):
        return None
    return tuple(paths)
