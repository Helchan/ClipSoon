from __future__ import annotations

import io
import json

import pytest

from clipsoon.windows_hotkey_host import (
    HEARTBEAT_TIMER_ID,
    MOD_CONTROL,
    MOD_SHIFT,
    RI_KEY_BREAK,
    RI_KEY_E0,
    RIDEV_INPUTSINK,
    VK_CONTROL,
    VK_LCONTROL,
    VK_RCONTROL,
    WM_CLOSE,
    WM_HOTKEY,
    WM_INPUT,
    WM_TIMER,
    DoubleCtrlDetector,
    DoubleModifierDetector,
    JsonLineEmitter,
    RawInputHotkeyEngine,
    RawKeyboardEvent,
    WindowsHotkeyHost,
    is_shutdown_command,
    parse_registered_hotkey,
)


def test_double_ctrl_accepts_left_right_taps_and_ignores_auto_repeat() -> None:
    hits: list[float] = []
    detector = DoubleCtrlDetector(420, hits.append)

    detector.feed(RawKeyboardEvent(VK_LCONTROL, 0), 0.00)
    detector.feed(RawKeyboardEvent(VK_LCONTROL, 0), 0.01)  # auto-repeat
    detector.feed(RawKeyboardEvent(VK_LCONTROL, RI_KEY_BREAK), 0.05)
    detector.feed(RawKeyboardEvent(VK_CONTROL, RI_KEY_E0), 0.25)
    detector.feed(RawKeyboardEvent(VK_CONTROL, RI_KEY_E0 | RI_KEY_BREAK), 0.30)

    assert hits == [0.30]


def test_double_ctrl_rejects_chords_long_holds_and_slow_pairs() -> None:
    hits: list[float] = []
    detector = DoubleCtrlDetector(420, hits.append)

    # A non-Control key already held makes the Control press a chord.
    detector.feed(RawKeyboardEvent(ord("C"), 0), 0.00)
    detector.feed(RawKeyboardEvent(VK_LCONTROL, 0), 0.01)
    detector.feed(RawKeyboardEvent(VK_LCONTROL, RI_KEY_BREAK), 0.04)
    detector.feed(RawKeyboardEvent(ord("C"), RI_KEY_BREAK), 0.05)

    # A non-Control key pressed during Control also makes it a chord.
    detector.feed(RawKeyboardEvent(VK_LCONTROL, 0), 1.00)
    detector.feed(RawKeyboardEvent(ord("V"), 0), 1.01)
    detector.feed(RawKeyboardEvent(ord("V"), RI_KEY_BREAK), 1.02)
    detector.feed(RawKeyboardEvent(VK_LCONTROL, RI_KEY_BREAK), 1.03)

    # A long hold and a pair outside the configured interval are invalid.
    detector.feed(RawKeyboardEvent(VK_RCONTROL, 0), 2.00)
    detector.feed(RawKeyboardEvent(VK_RCONTROL, RI_KEY_BREAK), 2.50)
    detector.feed(RawKeyboardEvent(VK_RCONTROL, 0), 3.00)
    detector.feed(RawKeyboardEvent(VK_RCONTROL, RI_KEY_BREAK), 3.03)
    detector.feed(RawKeyboardEvent(VK_RCONTROL, 0), 3.60)
    detector.feed(RawKeyboardEvent(VK_RCONTROL, RI_KEY_BREAK), 3.63)

    assert hits == []


def test_double_ctrl_recovers_from_a_missing_release_after_stale_timeout() -> None:
    hits: list[float] = []
    detector = DoubleCtrlDetector(420, hits.append, stale_after_ms=1_000)

    detector.feed(RawKeyboardEvent(ord("C"), 0), 0.00)
    detector.expire(1.10)
    detector.feed(RawKeyboardEvent(VK_LCONTROL, 0), 1.20)
    detector.feed(RawKeyboardEvent(VK_LCONTROL, RI_KEY_BREAK), 1.23)
    detector.feed(RawKeyboardEvent(VK_RCONTROL, 0), 1.40)
    detector.feed(RawKeyboardEvent(VK_RCONTROL, RI_KEY_BREAK), 1.43)

    assert hits == [1.43]


def test_other_configured_double_modifiers_use_the_same_raw_input_detector() -> None:
    hits: list[float] = []
    detector = DoubleModifierDetector("shift", 420, hits.append)

    detector.feed(RawKeyboardEvent(0x10, 0, make_code=0x2A), 0.00)  # left Shift
    detector.feed(RawKeyboardEvent(0x10, RI_KEY_BREAK, make_code=0x2A), 0.03)
    detector.feed(RawKeyboardEvent(0x10, 0, make_code=0x36), 0.20)  # right Shift
    detector.feed(RawKeyboardEvent(0x10, RI_KEY_BREAK, make_code=0x36), 0.23)

    assert hits == [0.23]


def test_json_protocol_emits_ready_heartbeat_and_hotkey_with_monotonic_ids() -> None:
    output = io.StringIO()
    times = iter((10.0, 11.0, 12.0))
    engine = RawInputHotkeyEngine(
        JsonLineEmitter(output),
        interval_ms=420,
        clock=lambda: next(times),
        process_id=321,
        session_id="session-test",
    )

    engine.ready()
    engine.heartbeat()
    engine.feed_keyboard(RawKeyboardEvent(VK_LCONTROL, 0), 20.00)
    engine.feed_keyboard(RawKeyboardEvent(VK_LCONTROL, RI_KEY_BREAK), 20.03)
    engine.feed_keyboard(RawKeyboardEvent(VK_RCONTROL, 0), 20.20)
    engine.feed_keyboard(RawKeyboardEvent(VK_RCONTROL, RI_KEY_BREAK), 20.23)

    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [message["event_id"] for message in messages] == [1, 2, 3]
    assert [message["type"] for message in messages] == ["ready", "heartbeat", "hotkey"]
    assert {message["session_id"] for message in messages} == {"session-test"}
    assert {message["protocol"] for message in messages} == {1}
    assert {message["role"] for message in messages} == {"hotkey"}
    assert messages[0]["pid"] == 321
    assert messages[2]["hotkey"] == "double:ctrl"
    assert messages[2]["monotonic_ms"] == 20_230


def test_shutdown_command_supports_plain_and_json_lines() -> None:
    assert is_shutdown_command("shutdown\n")
    assert is_shutdown_command('{"type":"shutdown"}\n')
    assert is_shutdown_command('{"command":"shutdown"}\n')
    assert not is_shutdown_command('{"type":"heartbeat"}\n')
    assert not is_shutdown_command("not-json\n")


class _FakeWindowsApi:
    def __init__(self) -> None:
        self.wndproc = None
        self.registered: list[tuple[int, int]] = []
        self.registered_hotkeys: list[tuple[int, int, int, int]] = []
        self.unregistered_hotkeys: list[tuple[int, int]] = []
        self.timers: list[tuple[int, int, int]] = []
        self.posted: list[tuple[int, int]] = []
        self.closed = False
        self.mutex_available = True
        self.parent_exited = False
        self._packets = iter(
            (
                RawKeyboardEvent(VK_LCONTROL, 0),
                RawKeyboardEvent(VK_LCONTROL, RI_KEY_BREAK),
                RawKeyboardEvent(VK_RCONTROL, 0),
                RawKeyboardEvent(VK_RCONTROL, RI_KEY_BREAK),
            )
        )

    def create_message_window(self, wndproc) -> int:
        self.wndproc = wndproc
        return 101

    def acquire_mutex(self, _name: str) -> bool:
        return self.mutex_available

    def wait_for_process_exit(self, _process_id: int) -> bool:
        return self.parent_exited

    def register_keyboard(self, hwnd: int, flags: int) -> None:
        self.registered.append((hwnd, flags))

    def register_hotkey(self, hwnd: int, hotkey_id: int, modifiers: int, virtual_key: int) -> None:
        self.registered_hotkeys.append((hwnd, hotkey_id, modifiers, virtual_key))

    def unregister_hotkey(self, hwnd: int, hotkey_id: int) -> None:
        self.unregistered_hotkeys.append((hwnd, hotkey_id))

    def set_timer(self, hwnd: int, timer_id: int, interval_ms: int) -> None:
        self.timers.append((hwnd, timer_id, interval_ms))

    def read_keyboard(self, _lparam: int) -> RawKeyboardEvent:
        return next(self._packets)

    def message_loop(self) -> int:
        assert self.wndproc is not None
        for index in range(4):
            self.wndproc(101, WM_INPUT, 0, index + 1)
        self.wndproc(101, WM_TIMER, HEARTBEAT_TIMER_ID, 0)
        return 0

    def def_window_proc(self, _hwnd: int, _message: int, _wparam: int, _lparam: int) -> int:
        return 0

    def destroy_window(self, _hwnd: int) -> None:
        return None

    def post_quit(self) -> None:
        return None

    def post_message(self, hwnd: int, message: int) -> None:
        self.posted.append((hwnd, message))

    def close(self) -> None:
        self.closed = True


def test_host_is_platform_independent_with_injected_windows_api() -> None:
    output = io.StringIO()
    api = _FakeWindowsApi()
    host = WindowsHotkeyHost(
        interval_ms=420,
        heartbeat_interval_ms=1_250,
        output=output,
        control_input=None,
        api=api,
        clock=lambda: 5.0,
        process_id=456,
        session_id="host-test",
    )

    assert host.run() == 0

    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [message["type"] for message in messages] == ["ready", "hotkey", "heartbeat"]
    assert [message["event_id"] for message in messages] == [1, 2, 3]
    assert {message["session_id"] for message in messages} == {"host-test"}
    assert api.registered == [(101, RIDEV_INPUTSINK)]
    assert api.timers == [(101, HEARTBEAT_TIMER_ID, 1_250)]
    assert api.closed


def test_combo_hotkey_uses_register_hotkey_instead_of_raw_input() -> None:
    output = io.StringIO()
    api = _FakeWindowsApi()

    def combo_message_loop() -> int:
        assert api.wndproc is not None
        api.wndproc(101, WM_HOTKEY, 1, 0)
        return 0

    api.message_loop = combo_message_loop  # type: ignore[method-assign]
    host = WindowsHotkeyHost(
        hotkey="combo:ctrl+shift+v",
        output=output,
        control_input=None,
        api=api,
        clock=lambda: 7.0,
        session_id="combo-test",
    )

    assert host.run() == 0

    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [message["type"] for message in messages] == ["ready", "hotkey"]
    assert api.registered == []
    assert len(api.registered_hotkeys) == 1
    _, hotkey_id, modifiers, virtual_key = api.registered_hotkeys[0]
    assert hotkey_id == 1
    assert modifiers & 0x0002  # MOD_CONTROL
    assert modifiers & 0x0004  # MOD_SHIFT
    assert modifiers & 0x4000  # MOD_NOREPEAT
    assert virtual_key == ord("V")


def test_host_control_reader_posts_close_for_shutdown_or_eof() -> None:
    api = _FakeWindowsApi()
    exits: list[int] = []
    host = WindowsHotkeyHost(output=io.StringIO(), control_input=None, api=api, hard_exit=exits.append)
    host._hwnd = 707

    host._watch_control_stream(io.StringIO('{"type":"shutdown"}\n'))
    host._watch_control_stream(io.StringIO(""))

    assert api.posted == [(707, WM_CLOSE)]
    assert exits == [0]


def test_parent_exit_uses_hard_exit_even_when_window_thread_is_stuck() -> None:
    api = _FakeWindowsApi()
    api.parent_exited = True
    exits: list[int] = []
    host = WindowsHotkeyHost(
        output=io.StringIO(),
        control_input=None,
        api=api,
        parent_pid=123,
        hard_exit=exits.append,
    )

    host._watch_parent_process()

    assert exits == [0]


def test_existing_session_mutex_emits_fatal_without_creating_a_window() -> None:
    output = io.StringIO()
    api = _FakeWindowsApi()
    api.mutex_available = False
    host = WindowsHotkeyHost(
        output=output,
        control_input=None,
        api=api,
        session_id="duplicate-test",
    )

    assert host.run() == 3

    assert json.loads(output.getvalue()) == {
        "type": "error",
        "protocol": 1,
        "role": "hotkey",
        "session_id": "duplicate-test",
        "event_id": 1,
        "fatal": True,
        "code": "already_active",
    }
    assert api.wndproc is None
    assert api.closed


def test_combo_registration_failure_is_reported_without_restart_loop() -> None:
    output = io.StringIO()
    api = _FakeWindowsApi()
    api.register_hotkey = lambda *_args: (_ for _ in ()).throw(OSError("already registered"))  # type: ignore[method-assign]
    host = WindowsHotkeyHost(
        hotkey="combo:ctrl+shift+v",
        output=output,
        control_input=None,
        api=api,
        session_id="registration-test",
    )

    assert host.run() == 4

    message = json.loads(output.getvalue())
    assert message["type"] == "error"
    assert message["fatal"] is True
    assert message["code"] == "registration_failed"
    assert "already registered" in message["message"]


@pytest.mark.parametrize(
    ("spec", "virtual_key", "extra_modifier"),
    [
        ("combo:ctrl+,", 0xBC, 0),
        ("combo:ctrl+/", 0xBF, 0),
        ("combo:ctrl+?", 0xBF, MOD_SHIFT),
        ("combo:ctrl+pgup", 0x21, 0),
        ("combo:ctrl+del", 0x2E, 0),
        ("combo:ctrl+plus", 0xBB, MOD_SHIFT),
    ],
)
def test_common_qt_custom_keys_map_to_register_hotkey(
    spec: str,
    virtual_key: int,
    extra_modifier: int,
) -> None:
    parsed = parse_registered_hotkey(spec)

    assert parsed.virtual_key == virtual_key
    assert parsed.modifiers & MOD_CONTROL
    assert parsed.modifiers & extra_modifier == extra_modifier


def test_invalid_custom_hotkey_is_reported_without_restart_loop() -> None:
    output = io.StringIO()
    api = _FakeWindowsApi()
    host = WindowsHotkeyHost(
        hotkey="combo:ctrl+not-a-key",
        output=output,
        control_input=None,
        api=api,
        session_id="invalid-test",
    )

    assert host.run() == 5
    message = json.loads(output.getvalue())
    assert message["code"] == "invalid_hotkey"
    assert message["fatal"] is True
    assert api.wndproc is None


def test_raw_input_startup_failure_is_reported_and_can_be_retried() -> None:
    output = io.StringIO()
    api = _FakeWindowsApi()
    api.register_keyboard = lambda *_args: (_ for _ in ()).throw(OSError("raw input unavailable"))  # type: ignore[method-assign]
    host = WindowsHotkeyHost(
        output=output,
        control_input=None,
        api=api,
        session_id="startup-test",
    )

    assert host.run() == 6
    message = json.loads(output.getvalue())
    assert message["code"] == "startup_failed"
    assert message["fatal"] is True
    assert "raw input unavailable" in message["message"]
