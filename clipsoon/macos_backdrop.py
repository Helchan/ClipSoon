"""Native macOS visual-effect backing for transparent Qt top-level windows.

The controller deliberately owns only the AppKit view that sits *behind* the
Qt view.  Qt remains responsible for the window flags, input handling, and
all foreground painting.  Keeping that boundary small lets callers use the
same liquid-glass theme on older macOS releases without a screen-capture
permission or an undocumented private API.
"""

from __future__ import annotations

import ctypes
import logging
import math
import sys
from collections.abc import Callable
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)
_UNDER_WINDOW_MATERIAL = "under_window"
_POPOVER_MATERIAL = "popover"
_VALID_MATERIAL_ROLES = frozenset({_UNDER_WINDOW_MATERIAL, _POPOVER_MATERIAL})


@dataclass(frozen=True)
class MacosBackdropResult:
    """Outcome of a best-effort ``NSVisualEffectView`` operation."""

    applied: bool
    reason: str


@dataclass(frozen=True)
class _MacosModules:
    """The narrow PyObjC surface used by :class:`MacosBackdropController`."""

    appkit: object
    objc: object


@dataclass(frozen=True)
class _AppliedEffect:
    """A retained effect plus its semantic AppKit material role."""

    effect: object
    material_role: str


def _load_macos_modules() -> _MacosModules:
    """Import AppKit only after a Darwin caller actually requests an effect."""

    import AppKit
    import objc

    return _MacosModules(appkit=AppKit, objc=objc)


class MacosBackdropController:
    """Place one ``NSVisualEffectView`` below a native Qt ``NSView``.

    ``native_view_id`` is the integer form of a visible top-level QWidget's
    ``winId()``.  On macOS Qt exposes an ``NSView *`` as that WId.  The
    controller intentionally does not make an NSWindow opaque/transparent,
    alter Qt attributes, or use screen capture; the owning Qt surface must
    already have been prepared for transparency by its UI layer.
    """

    def __init__(
        self,
        module_loader: Callable[[], _MacosModules] | None = None,
        *,
        platform: str | None = None,
    ) -> None:
        self._module_loader = module_loader or _load_macos_modules
        self._platform = sys.platform if platform is None else platform
        self._modules: _MacosModules | None = None
        # AppKit retains a subview while attached, but this reference makes
        # explicit removal deterministic and protects it during hierarchy
        # transitions initiated by Qt.
        self._effects: dict[int, _AppliedEffect] = {}

    def apply(
        self,
        native_view_id: int,
        *,
        corner_radius: float = 16.0,
        content_inset: float = 0.0,
        material_role: str = _UNDER_WINDOW_MATERIAL,
    ) -> MacosBackdropResult:
        """Install or reuse a behind-window visual effect for one Qt NSView.

        ``content_inset`` is a symmetric inset in AppKit points.  Its default
        keeps the effect flush with the Qt root; a caller can pass ``14`` to
        align the effect with a 14-point inner card while the flexible width
        and height autoresizing mask preserves that gutter during resize.

        A Qt top-level's ``QNSView`` can move into its final AppKit host one
        event turn after ``show()``.  Reusing an effect is therefore safe only
        after verifying it remains the sibling immediately below that current
        Qt view; otherwise this method removes and reinserts it.
        """

        if native_view_id <= 0:
            return MacosBackdropResult(False, "invalid-native-view")
        if self._platform != "darwin":
            return MacosBackdropResult(False, "unsupported")
        if not self._valid_content_inset(content_inset):
            return MacosBackdropResult(False, "invalid-content-inset")
        if material_role not in _VALID_MATERIAL_ROLES:
            return MacosBackdropResult(False, "invalid-material-role")
        try:
            modules = self._modules or self._module_loader()
            self._modules = modules
            root_view = modules.objc.objc_object(c_void_p=ctypes.c_void_p(native_view_id))
            window = root_view.window()
            if window is None:
                return MacosBackdropResult(False, "no-window")
            content_view = window.contentView()
            if content_view is None:
                return MacosBackdropResult(False, "no-content-view")

            target = self._insertion_target(root_view)
            if target is None:
                # For a Qt top-level the root QNSView is the NSWindow content
                # view.  Adding an effect as its child would cover Qt's own
                # backing store, so wait for the native sibling host instead.
                return MacosBackdropResult(False, "defer-no-sibling-host")
            host, frame, relative_to = target
            if content_inset:
                frame = modules.appkit.NSInsetRect(frame, content_inset, content_inset)

            existing = self._effects.get(native_view_id)
            if existing is not None:
                if (
                    existing.material_role == material_role
                    and self._effect_is_current_sibling(existing.effect, host, root_view, window)
                ):
                    return MacosBackdropResult(True, "already-applied")
                existing.effect.removeFromSuperview()
                self._effects.pop(native_view_id, None)

            effect = modules.appkit.NSVisualEffectView.alloc().initWithFrame_(frame)
            effect.setAutoresizingMask_(
                modules.appkit.NSViewWidthSizable | modules.appkit.NSViewHeightSizable
            )
            effect.setMaterial_(self._material_for_role(modules.appkit, material_role))
            effect.setBlendingMode_(modules.appkit.NSVisualEffectBlendingModeBehindWindow)
            # ClipSoon is a floating Qt Tool/NSPanel. Such a panel can remain
            # visible without becoming the key window, where the follow-window
            # state weakens the material enough to look like an opaque tint.
            # Pinning the effect active keeps the documented behind-window
            # frost stable while Qt continues to own focus and interaction.
            effect.setState_(modules.appkit.NSVisualEffectStateActive)
            # ``alphaValue`` is not a blur-strength control. Reducing it
            # blends sharp, unfiltered desktop pixels back into the material.
            # Pin the native view to full opacity and use the Qt veil above it
            # only for the final, already-frosted contrast balance.
            set_alpha = getattr(effect, "setAlphaValue_", None)
            if callable(set_alpha):
                set_alpha(1.0)
            self._round_effect(effect, corner_radius)
            host.addSubview_positioned_relativeTo_(
                effect,
                modules.appkit.NSWindowBelow,
                relative_to,
            )
            self._effects[native_view_id] = _AppliedEffect(effect, material_role)
            return MacosBackdropResult(True, "applied")
        except Exception:
            # A WId can be recreated while Qt reparents a window.  Treat the
            # native material as optional rather than risking a UI crash.
            LOGGER.debug("Could not apply the macOS visual effect backdrop", exc_info=True)
            return MacosBackdropResult(False, "api-unavailable")

    def remove(self, native_view_id: int) -> MacosBackdropResult:
        """Remove the effect for a Qt NSView without changing Qt window flags."""

        if native_view_id <= 0:
            return MacosBackdropResult(False, "invalid-native-view")
        if self._platform != "darwin":
            return MacosBackdropResult(False, "unsupported")
        applied = self._effects.get(native_view_id)
        if applied is None:
            return MacosBackdropResult(False, "not-applied")
        try:
            applied.effect.removeFromSuperview()
            self._effects.pop(native_view_id, None)
            return MacosBackdropResult(True, "removed")
        except Exception:
            LOGGER.debug("Could not remove the macOS visual effect backdrop", exc_info=True)
            return MacosBackdropResult(False, "remove-failed")

    @staticmethod
    def _insertion_target(root_view: object) -> tuple[object, object, object] | None:
        """Return the AppKit sibling host below a Qt top-level ``QNSView``."""

        superview = root_view.superview()
        if superview is not None:
            return superview, root_view.frame(), root_view
        return None

    @staticmethod
    def _effect_is_current_sibling(
        effect: object,
        host: object,
        root_view: object,
        window: object,
    ) -> bool:
        """Check that a cached effect survived Qt/AppKit hierarchy changes."""

        try:
            if effect.superview() != host or effect.window() != window:
                return False
            subviews = list(host.subviews())
            return subviews.index(effect) < subviews.index(root_view)
        except (AttributeError, ValueError):
            return False

    @staticmethod
    def _material_for_role(appkit: object, material_role: str) -> object:
        """Map ClipSoon surface semantics to documented AppKit materials."""

        if material_role == _POPOVER_MATERIAL:
            return appkit.NSVisualEffectMaterialPopover
        return appkit.NSVisualEffectMaterialUnderWindowBackground

    @staticmethod
    def _valid_content_inset(value: float) -> bool:
        """Reject invalid values before passing them into Core Graphics geometry."""

        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0

    @staticmethod
    def _round_effect(effect: object, corner_radius: float) -> None:
        """Clip the native material to the same rounded shell used by Qt."""

        if corner_radius <= 0:
            return
        effect.setWantsLayer_(True)
        layer = effect.layer()
        if layer is not None:
            layer.setCornerRadius_(corner_radius)
            layer.setMasksToBounds_(True)
