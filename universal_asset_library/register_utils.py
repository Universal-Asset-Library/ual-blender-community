# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Idempotent bpy class register helpers (safe disable/enable + F8 reload).

Blender docs: PropertyGroups must be registered before use; ``register_class``
raises ``ValueError`` if the RNA identifier (class ``__name__``) is already
taken. After a module reload, Python has *new* class objects while RNA may still
hold the *old* type — ``getattr(bpy.types, name)`` is unreliable; use
``BaseType.bl_rna_get_subclass_py(name)`` to fetch the live registered class
(see ``bpy.types.PropertyGroup`` API).
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Type


def _log(msg: str) -> None:
    try:
        print("UAL register:", msg)
    except Exception:
        pass


def _rna_bases_for(cls: Type) -> List[Type]:
    """Return Blender base types to query for a live registered subclass."""
    import bpy

    bases: List[Type] = []
    for base in (
        bpy.types.PropertyGroup,
        bpy.types.AddonPreferences,
        bpy.types.Operator,
        bpy.types.Panel,
        bpy.types.UIList,
        bpy.types.Menu,
        bpy.types.Header,
    ):
        try:
            if issubclass(cls, base):
                bases.append(base)
        except TypeError:
            pass
    # Always try PropertyGroup / Operator / Panel as fallbacks for odd MRO
    for base in (bpy.types.PropertyGroup, bpy.types.Operator, bpy.types.Panel, bpy.types.UIList):
        if base not in bases:
            bases.append(base)
    return bases


def live_registered_class(name: str, cls: Optional[Type] = None) -> Optional[Type]:
    """Return the Python class currently registered under *name*, or None."""
    import bpy

    if not name:
        return None

    # Preferred: RNA subclass lookup (survives module reload).
    if cls is not None:
        for base in _rna_bases_for(cls):
            getter = getattr(base, "bl_rna_get_subclass_py", None)
            if getter is None:
                continue
            try:
                found = getter(name, None)
            except TypeError:
                try:
                    found = getter(name)
                except Exception:
                    found = None
            except Exception:
                found = None
            if found is not None:
                return found

    for base_name in (
        "PropertyGroup",
        "AddonPreferences",
        "Operator",
        "Panel",
        "UIList",
        "Menu",
    ):
        base = getattr(bpy.types, base_name, None)
        if base is None:
            continue
        getter = getattr(base, "bl_rna_get_subclass_py", None)
        if getter is None:
            continue
        try:
            found = getter(name, None)
        except TypeError:
            try:
                found = getter(name)
            except Exception:
                found = None
        except Exception:
            found = None
        if found is not None:
            return found

    # Fallback: bpy.types attribute (may be the reloaded, unregistered class).
    try:
        attr = getattr(bpy.types, name, None)
    except Exception:
        attr = None
    if attr is not None and getattr(attr, "is_registered", False):
        return attr
    return None


def _is_ual_owned(cls: Type) -> bool:
    """True if *cls* belongs to this add-on (safe to unregister on reload)."""
    name = getattr(cls, "__name__", "") or ""
    if name.startswith("UAL_"):
        return True
    mod = getattr(cls, "__module__", "") or ""
    if "universal_asset_library" in mod:
        return True
    bl_idname = getattr(cls, "bl_idname", None)
    if isinstance(bl_idname, str):
        if bl_idname.startswith("ual.") or bl_idname == "universal_asset_library":
            return True
    return False


def unregister_class_safe(cls: Type) -> bool:
    """Unregister *cls* and/or any live UAL RNA type with the same ``__name__``."""
    import bpy

    name = getattr(cls, "__name__", "") or ""
    removed = False
    candidates: List[Type] = []

    live = live_registered_class(name, cls)
    if live is not None:
        if live is cls or _is_ual_owned(live):
            candidates.append(live)
        else:
            _log(
                "skip unregister of non-UAL live type {} ({})".format(
                    name, getattr(live, "__module__", "?")
                )
            )
    if getattr(cls, "is_registered", False):
        candidates.append(cls)
    candidates.append(cls)

    seen = set()
    for candidate in candidates:
        key = id(candidate)
        if key in seen:
            continue
        seen.add(key)
        try:
            if not getattr(candidate, "is_registered", True):
                # Still try — some builds omit is_registered
                pass
            bpy.utils.unregister_class(candidate)
            removed = True
        except Exception as exc:
            _log("unregister {} failed: {}".format(getattr(candidate, "__name__", candidate), exc))
    return removed


def register_class_safe(cls: Type) -> bool:
    """Register *cls*, replacing any live type of the same name. Never raises ValueError."""
    import bpy

    name = getattr(cls, "__name__", "") or ""
    unregister_class_safe(cls)
    # Second pass: RNA lookup again after dependents may have been cleared by caller
    live = live_registered_class(name, cls)
    if live is not None and (live is cls or _is_ual_owned(live)):
        try:
            bpy.utils.unregister_class(live)
        except Exception as exc:
            _log("second unregister {} failed: {}".format(name, exc))
    elif live is not None:
        _log("skip second unregister of non-UAL live type {}".format(name))

    try:
        bpy.utils.register_class(cls)
        return True
    except ValueError as exc:
        # Last attempt: unregister live again then register once more.
        live = live_registered_class(name, cls)
        if live is not None and (live is cls or _is_ual_owned(live)):
            try:
                bpy.utils.unregister_class(live)
                bpy.utils.register_class(cls)
                return True
            except Exception as exc2:
                _log("replace {} failed: {} / {}".format(name, exc, exc2))
        # NEVER keep a half-registered PropertyGroup (missing props → panel crashes).
        # Purge the RNA name so the next bump / retry can succeed cleanly.
        try:
            live = live_registered_class(name, cls)
            if live is not None and (live is cls or _is_ual_owned(live)):
                bpy.utils.unregister_class(live)
                _log("purged broken {}".format(name))
        except Exception as exc3:
            _log("purge broken {} failed: {}".format(name, exc3))
        _log("register failed {}: {}".format(name, exc))
        return False


def register_classes(classes: Sequence[Type]) -> None:
    """Unregister dependents first, then register in dependency order."""
    ordered = tuple(classes)
    # Pass 1: tear down reverse (AddonPreferences / UIProps before PropertyGroups)
    for cls in reversed(ordered):
        unregister_class_safe(cls)
    # Pass 2: register forward; never abort the whole addon on one sticky type
    for cls in ordered:
        register_class_safe(cls)


def unregister_classes(classes: Iterable[Type]) -> None:
    for cls in reversed(tuple(classes)):
        unregister_class_safe(cls)


def purge_named_types(names: Sequence[str]) -> None:
    """Best-effort unregister of known RNA identifiers (startup recovery)."""
    for name in names:
        live = live_registered_class(name, None)
        if live is None:
            continue
        try:
            import bpy

            bpy.utils.unregister_class(live)
        except Exception as exc:
            _log("purge {} failed: {}".format(name, exc))
