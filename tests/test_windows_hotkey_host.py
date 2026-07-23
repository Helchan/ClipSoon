from __future__ import annotations

import ctypes
import io
import json
import types

import pytest

from clipsoon.windows_hotkey_host import (
    HEARTBEAT_TIMER_ID,
    MOD_CONTROL,
    MOD_SHIFT,
    WM_CLOSE,
    WM_HOTKEY,
    WM_TIMER,
    JsonLineEmitter,
    NativeHotkeyEngine,
    WindowsHotkeyHost,
    _GuiThreadInfo,
    _Win32Api,
    is_shutdown_command,
    parse_registered_hotkey,
)


def test_json_protocol_emits_ready_heartbeat_and_hotkey_with_monotonic_ids() -> None:
    output = io.StringIO()
    times = iter((10.0, 11.0))
    engine = NativeHotkeyEngine(
        JsonLineEmitter(output),
        hotkey="combo:ctrl+shift+space",
        clock=lambda: next(times),
        process_id=321,
        session_id="session-test",
    )

    engine.ready()
    engine.heartbeat()
    engine.activate(20.23)

    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [message["event_id"] for message in messages] == [1, 2, 3]
    assert [message["type"] for message in messages] == ["ready", "heartbeat", "hotkey"]
    assert {message["session_id"] for message in messages} == {"session-test"}
    assert {message["protocol"] for message in messages} == {1}
    assert {message["role"] for message in messages} == {"hotkey"}
    assert messages[0]["pid"] == 321
    assert messages[2]["hotkey"] == "combo:ctrl+shift+space"
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
        self.registered_hotkeys: list[tuple[int, int, int, int]] = []
        self.unregistered_hotkeys: list[tuple[int, int]] = []
        self.timers: list[tuple[int, int, int]] = []
        self.posted: list[tuple[int, int]] = []
        self.closed = False
        self.mutex_available = True
        self.parent_exited = False
        self.foreground = 909
        self.focus = 808
        self.target_thread_id = 7001
        self.target_process_id = 8001
        self.focus_thread_id = 7002
        self.focus_process_id = 8001
        self.foreground_handoffs: list[int] = []
        self.activation_order: list[tuple[str, int]] = []

    def create_message_window(self, wndproc) -> int:
        self.wndproc = wndproc
        return 101

    def acquire_mutex(self, _name: str) -> bool:
        return self.mutex_available

    def wait_for_process_exit(self, _process_id: int) -> bool:
        return self.parent_exited

    def foreground_window(self) -> int:
        self.activation_order.append(("target", self.foreground))
        return self.foreground

    def window_thread_process_id(self, hwnd: int) -> tuple[int, int]:
        self.activation_order.append(("identity", hwnd))
        if hwnd == self.foreground:
            return self.target_thread_id, self.target_process_id
        if hwnd == self.focus:
            return self.focus_thread_id, self.focus_process_id
        raise OSError("unknown window")

    def focus_window(self, thread_id: int) -> int:
        self.activation_order.append(("focus", thread_id))
        assert thread_id == self.target_thread_id
        return self.focus

    def allow_set_foreground_window(self, process_id: int) -> bool:
        self.foreground_handoffs.append(process_id)
        self.activation_order.append(("grant", process_id))
        return True

    def register_hotkey(self, hwnd: int, hotkey_id: int, modifiers: int, virtual_key: int) -> None:
        self.registered_hotkeys.append((hwnd, hotkey_id, modifiers, virtual_key))

    def unregister_hotkey(self, hwnd: int, hotkey_id: int) -> None:
        self.unregistered_hotkeys.append((hwnd, hotkey_id))

    def set_timer(self, hwnd: int, timer_id: int, interval_ms: int) -> None:
        self.timers.append((hwnd, timer_id, interval_ms))

    def message_loop(self) -> int:
        assert self.wndproc is not None
        self.wndproc(101, WM_HOTKEY, 1, 0)
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


def test_win32_api_reads_window_identity_and_gui_thread_focus(monkeypatch) -> None:
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(ctypes, "set_last_error", lambda _value: None, raising=False)

    def get_window_thread_process_id(hwnd, process_pointer) -> int:
        calls.append(("identity", int(hwnd.value)))
        ctypes.cast(process_pointer, ctypes.POINTER(ctypes.c_uint32)).contents.value = 8001
        return 7001

    def get_gui_thread_info(thread_id, information_pointer) -> int:
        calls.append(("focus", int(thread_id.value)))
        information = ctypes.cast(
            information_pointer,
            ctypes.POINTER(_GuiThreadInfo),
        ).contents
        assert information.cbSize == ctypes.sizeof(_GuiThreadInfo)
        information.hwndFocus = 808
        return 1

    api = object.__new__(_Win32Api)
    api.user32 = types.SimpleNamespace(
        GetWindowThreadProcessId=get_window_thread_process_id,
        GetGUIThreadInfo=get_gui_thread_info,
    )

    assert api.window_thread_process_id(909) == (7001, 8001)
    assert api.focus_window(7001) == 808
    assert calls == [("identity", 909), ("focus", 7001)]


def test_default_hotkey_registers_and_emits_hotkey_and_heartbeat() -> None:
    output = io.StringIO()
    api = _FakeWindowsApi()
    host = WindowsHotkeyHost(
        heartbeat_interval_ms=1_250,
        output=output,
        control_input=None,
        api=api,
        clock=lambda: 5.0,
        process_id=456,
        session_id="host-test",
        parent_pid=654,
    )

    assert host.run() == 0

    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [message["type"] for message in messages] == ["ready", "hotkey", "heartbeat"]
    assert [message["event_id"] for message in messages] == [1, 2, 3]
    assert {message["session_id"] for message in messages} == {"host-test"}
    assert messages[0]["hotkey"] == "combo:ctrl+shift+space"
    assert messages[1]["target_hwnd"] == 909
    assert messages[1]["target_thread_id"] == 7001
    assert messages[1]["target_process_id"] == 8001
    assert messages[1]["focus_hwnd"] == 808
    assert messages[1]["focus_thread_id"] == 7002
    assert messages[1]["focus_process_id"] == 8001
    assert messages[1]["foreground_granted"] is True
    assert api.activation_order[:5] == [
        ("target", 909),
        ("identity", 909),
        ("focus", 7001),
        ("identity", 808),
        ("grant", 654),
    ]
    assert len(api.registered_hotkeys) == 1
    hwnd, hotkey_id, modifiers, virtual_key = api.registered_hotkeys[0]
    assert (hwnd, hotkey_id, virtual_key) == (101, 1, 0x20)
    assert modifiers & MOD_CONTROL
    assert modifiers & MOD_SHIFT
    assert modifiers & 0x4000  # MOD_NOREPEAT
    assert api.unregistered_hotkeys == [(101, 1)]
    assert api.timers == [(101, HEARTBEAT_TIMER_ID, 1_250)]
    assert api.closed


def test_hotkey_context_omits_focus_fields_when_target_thread_has_no_focus() -> None:
    api = _FakeWindowsApi()
    api.focus = 0
    host = WindowsHotkeyHost(
        output=io.StringIO(),
        control_input=None,
        api=api,
        parent_pid=654,
    )

    context = host._activation_context()

    assert context == {
        "target_hwnd": 909,
        "target_thread_id": 7001,
        "target_process_id": 8001,
        "foreground_granted": True,
    }


@pytest.mark.parametrize(
    "failure_stage",
    ("target_identity", "gui_thread_info", "focus_identity"),
)
def test_hotkey_context_focus_snapshot_failure_falls_back_to_top_level_target(
    failure_stage: str,
) -> None:
    api = _FakeWindowsApi()
    original_identity = api.window_thread_process_id

    if failure_stage == "target_identity":
        api.window_thread_process_id = (  # type: ignore[method-assign]
            lambda _hwnd: (_ for _ in ()).throw(OSError("target identity unavailable"))
        )
    elif failure_stage == "gui_thread_info":
        api.focus_window = (  # type: ignore[method-assign]
            lambda _thread_id: (_ for _ in ()).throw(OSError("GUI state unavailable"))
        )
    else:
        api.window_thread_process_id = (  # type: ignore[method-assign]
            lambda hwnd: (
                (_ for _ in ()).throw(OSError("focus identity unavailable"))
                if hwnd == api.focus
                else original_identity(hwnd)
            )
        )
    host = WindowsHotkeyHost(
        output=io.StringIO(),
        control_input=None,
        api=api,
        parent_pid=654,
    )

    expected: dict[str, object] = {
        "target_hwnd": 909,
        "foreground_granted": True,
    }
    if failure_stage != "target_identity":
        expected.update(
            {
                "target_thread_id": 7001,
                "target_process_id": 8001,
            }
        )
    assert host._activation_context() == expected


def test_custom_combo_hotkey_uses_register_hotkey() -> None:
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
    assert len(api.registered_hotkeys) == 1
    _, hotkey_id, modifiers, virtual_key = api.registered_hotkeys[0]
    assert hotkey_id == 1
    assert modifiers & 0x0002  # MOD_CONTROL
    assert modifiers & 0x0004  # MOD_SHIFT
    assert modifiers & 0x4000  # MOD_NOREPEAT
    assert virtual_key == ord("V")


def test_register_hotkey_remains_stable_across_250_activations() -> None:
    output = io.StringIO()
    api = _FakeWindowsApi()

    def repeated_hotkey_loop() -> int:
        assert api.wndproc is not None
        for _ in range(250):
            api.wndproc(101, WM_HOTKEY, 1, 0)
        api.wndproc(101, WM_TIMER, HEARTBEAT_TIMER_ID, 0)
        return 0

    api.message_loop = repeated_hotkey_loop  # type: ignore[method-assign]
    host = WindowsHotkeyHost(
        output=output,
        control_input=None,
        api=api,
        clock=lambda: 7.0,
        session_id="repeated-hotkey-test",
        parent_pid=654,
    )

    assert host.run() == 0

    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [message["type"] for message in messages] == [
        "ready",
        *(["hotkey"] * 250),
        "heartbeat",
    ]
    assert [message["event_id"] for message in messages] == list(range(1, 253))
    assert len(api.foreground_handoffs) == 250
    assert api.unregistered_hotkeys == [(101, 1)]
    assert api.closed


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


@pytest.mark.parametrize(
    "legacy_hotkey",
    ("double:ctrl", "double:shift", "double:alt", "double:meta"),
)
def test_double_modifier_hotkeys_are_rejected_as_invalid(legacy_hotkey: str) -> None:
    output = io.StringIO()
    api = _FakeWindowsApi()
    host = WindowsHotkeyHost(
        hotkey=legacy_hotkey,
        output=output,
        control_input=None,
        api=api,
        session_id="legacy-hotkey-test",
    )

    assert host.run() == 5
    message = json.loads(output.getvalue())
    assert message["code"] == "invalid_hotkey"
    assert message["fatal"] is True
    assert legacy_hotkey in message["message"]
    assert api.wndproc is None
    assert api.registered_hotkeys == []
