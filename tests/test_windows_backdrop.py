from __future__ import annotations

import ctypes

from clipsoon.windows_backdrop import (
    _DWMSBT_NONE,
    _DWMSBT_TRANSIENTWINDOW,
    _DWMWA_SYSTEMBACKDROP_TYPE,
    NativeDwmApi,
    WindowsBackdropController,
    _normalized_hresult,
)


class FakeDwmApi:
    def __init__(
        self,
        *,
        set_result: int = 0,
        get_result: int = 0,
        backdrop_value: int = _DWMSBT_TRANSIENTWINDOW,
    ) -> None:
        self.set_result = set_result
        self.get_result = get_result
        self.backdrop_value = backdrop_value
        self.set_calls: list[tuple[int, int]] = []
        self.get_calls: list[int] = []

    def set_system_backdrop(self, hwnd: int, backdrop: int) -> int:
        self.set_calls.append((hwnd, backdrop))
        return self.set_result

    def get_system_backdrop(self, hwnd: int) -> tuple[int, int | None]:
        self.get_calls.append(hwnd)
        return self.get_result, self.backdrop_value if self.get_result >= 0 else None


def test_native_dwm_api_binds_and_passes_the_system_backdrop_enum() -> None:
    calls: list[tuple[str, int, int, int]] = []

    class Function:
        argtypes = None
        restype = None

        def __init__(self, name: str) -> None:
            self.name = name

        def __call__(self, hwnd, attribute, pointer, size):
            calls.append(
                (
                    self.name,
                    int(hwnd.value),
                    int(attribute.value),
                    int(size.value),
                )
            )
            if self.name == "DwmGetWindowAttribute":
                ctypes.cast(pointer, ctypes.POINTER(ctypes.c_int32)).contents.value = _DWMSBT_TRANSIENTWINDOW
            return 0

    class Library:
        DwmSetWindowAttribute = Function("DwmSetWindowAttribute")
        DwmGetWindowAttribute = Function("DwmGetWindowAttribute")

    api = NativeDwmApi(lambda: Library())

    assert api.set_system_backdrop(123, _DWMSBT_TRANSIENTWINDOW) == 0
    assert api.get_system_backdrop(123) == (0, _DWMSBT_TRANSIENTWINDOW)
    assert calls == [
        ("DwmSetWindowAttribute", 123, _DWMWA_SYSTEMBACKDROP_TYPE, 4),
        ("DwmGetWindowAttribute", 123, _DWMWA_SYSTEMBACKDROP_TYPE, 4),
    ]


def test_controller_uses_only_supported_win11_builds_and_verifies_dwm_state() -> None:
    api = FakeDwmApi()
    controller = WindowsBackdropController(
        api,
        platform="win32",
        build_number=lambda: 22_621,
    )

    result = controller.apply_transient(123)

    assert result.applied
    assert api.set_calls == [(123, _DWMSBT_TRANSIENTWINDOW)]
    assert api.get_calls == [123]


def test_controller_does_not_load_or_call_dwm_on_windows_10() -> None:
    api = FakeDwmApi()
    controller = WindowsBackdropController(
        api,
        platform="win32",
        build_number=lambda: 19_045,
    )

    result = controller.apply_transient(123)

    assert not result.applied
    assert result.reason == "unsupported"
    assert api.set_calls == []
    assert api.get_calls == []


def test_controller_falls_back_when_the_dwm_request_or_readback_fails() -> None:
    rejected = FakeDwmApi(set_result=0x80070057)
    rejected_controller = WindowsBackdropController(
        rejected,
        platform="win32",
        build_number=lambda: 22_621,
    )

    rejected_result = rejected_controller.apply_transient(123)

    assert not rejected_result.applied
    assert rejected_result.reason == "set-failed"
    assert rejected.get_calls == []

    mismatch = FakeDwmApi(backdrop_value=_DWMSBT_NONE)
    mismatch_controller = WindowsBackdropController(
        mismatch,
        platform="win32",
        build_number=lambda: 22_621,
    )

    mismatch_result = mismatch_controller.apply_transient(456)

    assert not mismatch_result.applied
    assert mismatch_result.reason == "verification-failed"
    assert mismatch.set_calls == [
        (456, _DWMSBT_TRANSIENTWINDOW),
        (456, _DWMSBT_NONE),
    ]


def test_controller_clears_with_none_not_auto() -> None:
    api = FakeDwmApi()
    controller = WindowsBackdropController(
        api,
        platform="win32",
        build_number=lambda: 22_621,
    )

    result = controller.clear(789)

    assert result.applied
    assert result.reason == "cleared"
    assert api.set_calls == [(789, _DWMSBT_NONE)]


def test_hresult_normalization_handles_unsigned_win32_failure_values() -> None:
    assert _normalized_hresult(0x80070057) < 0
