from __future__ import annotations

from clipsoon.macos_backdrop import MacosBackdropController, _MacosModules


class _FakeLayer:
    def __init__(self) -> None:
        self.corner_radius: float | None = None
        self.masks_to_bounds: bool | None = None

    def setCornerRadius_(self, value: float) -> None:
        self.corner_radius = value

    def setMasksToBounds_(self, value: bool) -> None:
        self.masks_to_bounds = value


class _FakeEffect:
    instances: list[_FakeEffect] = []

    def __init__(self) -> None:
        self.frame: object | None = None
        self.autoresize_mask: int | None = None
        self.material: int | None = None
        self.blending_mode: int | None = None
        self.state: int | None = None
        self.alpha_value: float | None = None
        self.wants_layer = False
        self._layer = _FakeLayer()
        self.removed = False
        self._superview: _FakeHost | None = None
        self._window: _FakeWindow | None = None

    @classmethod
    def alloc(cls) -> _FakeEffect:
        effect = cls()
        cls.instances.append(effect)
        return effect

    def initWithFrame_(self, frame: object) -> _FakeEffect:
        self.frame = frame
        return self

    def setFrame_(self, frame: object) -> None:
        self.frame = frame

    def setAutoresizingMask_(self, value: int) -> None:
        self.autoresize_mask = value

    def setMaterial_(self, value: int) -> None:
        self.material = value

    def setBlendingMode_(self, value: int) -> None:
        self.blending_mode = value

    def setState_(self, value: int) -> None:
        self.state = value

    def setAlphaValue_(self, value: float) -> None:
        self.alpha_value = value

    def setWantsLayer_(self, value: bool) -> None:
        self.wants_layer = value

    def layer(self) -> _FakeLayer:
        return self._layer

    def superview(self) -> _FakeHost | None:
        return self._superview

    def window(self) -> _FakeWindow | None:
        return self._window

    def removeFromSuperview(self) -> None:
        self.removed = True
        if self._superview is not None:
            self._superview.remove(self)


class _FakeHost:
    def __init__(self, bounds: object) -> None:
        self._bounds = bounds
        self.additions: list[tuple[object, int, object | None]] = []
        self._subviews: list[object] = []

    def bounds(self) -> object:
        return self._bounds

    def addSubview_positioned_relativeTo_(
        self,
        effect: object,
        position: int,
        relative_to: object | None,
    ) -> None:
        self.additions.append((effect, position, relative_to))
        if relative_to in self._subviews:
            self._subviews.insert(self._subviews.index(relative_to), effect)
        else:
            self._subviews.append(effect)
        if isinstance(effect, _FakeEffect):
            effect._superview = self
            effect._window = relative_to.window() if relative_to is not None else None

    def remove(self, view: object) -> None:
        if view in self._subviews:
            self._subviews.remove(view)

    def subviews(self) -> list[object]:
        return list(self._subviews)


class _FakeWindow:
    def __init__(self, content_view: _FakeHost | None) -> None:
        self._content_view = content_view

    def contentView(self) -> _FakeHost | None:
        return self._content_view


class _FakeRootView:
    def __init__(
        self,
        window: _FakeWindow | None,
        *,
        superview: _FakeHost | None,
        frame: object,
    ) -> None:
        self._window = window
        self._superview = superview
        self._frame = frame
        if superview is not None:
            superview._subviews.append(self)

    def window(self) -> _FakeWindow | None:
        return self._window

    def superview(self) -> _FakeHost | None:
        return self._superview

    def frame(self) -> object:
        return self._frame


class _FakeObjc:
    def __init__(self, root_view: _FakeRootView) -> None:
        self.root_view = root_view
        self.native_values: list[int] = []

    def objc_object(self, *, c_void_p) -> _FakeRootView:
        self.native_values.append(int(c_void_p.value))
        return self.root_view


class _FakeAppKit:
    NSVisualEffectView = _FakeEffect
    NSViewWidthSizable = 1
    NSViewHeightSizable = 2
    NSVisualEffectMaterialUnderWindowBackground = 21
    NSVisualEffectMaterialPopover = 6
    NSVisualEffectBlendingModeBehindWindow = 0
    NSVisualEffectStateActive = 1
    NSWindowBelow = -1

    @staticmethod
    def NSInsetRect(frame: tuple[float, float, float, float], horizontal: float, vertical: float):
        x, y, width, height = frame
        return (
            x + horizontal,
            y + vertical,
            width - horizontal * 2,
            height - vertical * 2,
        )


def _modules_for(root_view: _FakeRootView) -> tuple[_MacosModules, _FakeObjc]:
    objc = _FakeObjc(root_view)
    return _MacosModules(appkit=_FakeAppKit, objc=objc), objc


def test_controller_is_a_safe_noop_off_macos() -> None:
    loaded = False

    def loader() -> _MacosModules:
        nonlocal loaded
        loaded = True
        raise AssertionError("the loader must not run")

    controller = MacosBackdropController(loader, platform="win32")

    assert controller.apply(123).reason == "unsupported"
    assert controller.remove(123).reason == "unsupported"
    assert not loaded


def test_controller_inserts_and_reuses_a_rounded_sibling_effect() -> None:
    _FakeEffect.instances.clear()
    content = _FakeHost(bounds=(0, 0, 900, 610))
    root = _FakeRootView(
        _FakeWindow(content),
        superview=content,
        frame=(0, 0, 900, 610),
    )
    modules, objc = _modules_for(root)
    controller = MacosBackdropController(lambda: modules, platform="darwin")

    result = controller.apply(123, corner_radius=18.0)

    assert result.applied and result.reason == "applied"
    assert objc.native_values == [123]
    effect = _FakeEffect.instances[0]
    assert effect.frame == (0, 0, 900, 610)
    assert effect.autoresize_mask == 3
    assert effect.material == _FakeAppKit.NSVisualEffectMaterialUnderWindowBackground
    assert effect.blending_mode == _FakeAppKit.NSVisualEffectBlendingModeBehindWindow
    assert effect.state == _FakeAppKit.NSVisualEffectStateActive
    # A partial alpha leaks unblurred desktop content through the system
    # material. Pin the visual-effect view itself at AppKit's full opacity.
    assert effect.alpha_value == 1.0
    assert effect.wants_layer
    assert effect.layer().corner_radius == 18.0
    assert effect.layer().masks_to_bounds is True
    assert content.additions == [(effect, _FakeAppKit.NSWindowBelow, root)]

    assert controller.apply(123).reason == "already-applied"
    assert len(_FakeEffect.instances) == 1


def test_controller_can_symmetrically_inset_the_effect_to_match_an_inner_card() -> None:
    _FakeEffect.instances.clear()
    content = _FakeHost(bounds=(0, 0, 900, 610))
    root = _FakeRootView(
        _FakeWindow(content),
        superview=content,
        frame=(0, 0, 900, 610),
    )
    modules, _objc = _modules_for(root)
    controller = MacosBackdropController(lambda: modules, platform="darwin")

    result = controller.apply(246, content_inset=14.0)

    assert result.applied
    effect = _FakeEffect.instances[0]
    assert effect.frame == (14.0, 14.0, 872.0, 582.0)
    # The existing flexible dimensions retain the 14-point gutter after an
    # AppKit content-view resize instead of pinning the effect to one edge.
    assert effect.autoresize_mask == 3


def test_controller_refreshes_reused_effect_geometry_and_radius() -> None:
    _FakeEffect.instances.clear()
    content = _FakeHost(bounds=(0, 0, 900, 610))
    root = _FakeRootView(
        _FakeWindow(content),
        superview=content,
        frame=(0, 0, 900, 610),
    )
    modules, _objc = _modules_for(root)
    controller = MacosBackdropController(lambda: modules, platform="darwin")

    assert controller.apply(246, corner_radius=18.0, content_inset=14.0).reason == "applied"
    effect = _FakeEffect.instances[0]
    root._frame = (0, 0, 920, 630)

    result = controller.apply(246, corner_radius=20.0, content_inset=14.0)

    assert result.applied and result.reason == "already-applied"
    assert len(_FakeEffect.instances) == 1
    assert effect.frame == (14.0, 14.0, 892.0, 602.0)
    assert effect.layer().corner_radius == 20.0
    assert not effect.removed


def test_controller_uses_the_popover_material_for_a_contextual_floating_surface() -> None:
    _FakeEffect.instances.clear()
    content = _FakeHost(bounds=(0, 0, 900, 610))
    root = _FakeRootView(
        _FakeWindow(content),
        superview=content,
        frame=(0, 0, 900, 610),
    )
    modules, _objc = _modules_for(root)
    controller = MacosBackdropController(lambda: modules, platform="darwin")

    result = controller.apply(246, material_role="popover")

    assert result.applied
    assert _FakeEffect.instances[0].material == _FakeAppKit.NSVisualEffectMaterialPopover


def test_controller_rejects_unknown_material_roles() -> None:
    controller = MacosBackdropController(platform="darwin")

    assert controller.apply(1, material_role="window").reason == "invalid-material-role"


def test_controller_rejects_negative_or_non_numeric_content_insets() -> None:
    controller = MacosBackdropController(platform="darwin")

    assert controller.apply(1, content_inset=-1).reason == "invalid-content-inset"
    assert controller.apply(1, content_inset=float("inf")).reason == "invalid-content-inset"
    assert controller.apply(1, content_inset=True).reason == "invalid-content-inset"


def test_controller_defers_until_the_qt_view_has_a_safe_sibling_host() -> None:
    _FakeEffect.instances.clear()
    content = _FakeHost(bounds=(0, 0, 420, 300))
    root = _FakeRootView(
        _FakeWindow(content),
        superview=None,
        frame=(99, 99, 1, 1),
    )
    modules, _objc = _modules_for(root)
    controller = MacosBackdropController(lambda: modules, platform="darwin")

    result = controller.apply(456, corner_radius=0)

    assert not result.applied
    assert result.reason == "defer-no-sibling-host"
    assert not _FakeEffect.instances
    assert not content.additions


def test_controller_reinserts_a_cached_effect_after_qt_changes_its_sibling_host() -> None:
    _FakeEffect.instances.clear()
    initial_host = _FakeHost(bounds=(0, 0, 900, 610))
    root = _FakeRootView(
        _FakeWindow(initial_host),
        superview=initial_host,
        frame=(0, 0, 900, 610),
    )
    modules, _objc = _modules_for(root)
    controller = MacosBackdropController(lambda: modules, platform="darwin")

    assert controller.apply(456).reason == "applied"
    initial_effect = _FakeEffect.instances[0]
    final_host = _FakeHost(bounds=(0, 0, 900, 610))
    initial_host.remove(root)
    root._superview = final_host
    final_host._subviews.append(root)

    result = controller.apply(456)

    assert result.applied and result.reason == "applied"
    assert initial_effect.removed
    assert len(_FakeEffect.instances) == 2
    current_effect = _FakeEffect.instances[1]
    assert final_host.additions == [(current_effect, _FakeAppKit.NSWindowBelow, root)]


def test_controller_degrades_cleanly_when_the_native_view_has_not_joined_a_window() -> None:
    root = _FakeRootView(None, superview=None, frame=(0, 0, 1, 1))
    modules, _objc = _modules_for(root)
    controller = MacosBackdropController(lambda: modules, platform="darwin")

    result = controller.apply(789)

    assert not result.applied
    assert result.reason == "no-window"
