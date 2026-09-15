# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""UAL Material Blend — Object proximity + Sets Geometry Nodes.

Not part of import_routing / material_builder. Reference methods:
``MaterialShader Mix Reference/Blendit/`` and ``mk_surface_blend/``
(patterns only, no paste).

``register`` / ``unregister`` import bpy-backed modules lazily so
``material_blend.constants`` stays importable in pure tests.
"""

from __future__ import annotations


def register() -> None:
    from . import ops as blend_ops
    from . import sets_state
    from . import state

    state.register()
    sets_state.register()
    blend_ops.register()


def unregister() -> None:
    from . import ops as blend_ops
    from . import sets_state
    from . import state

    blend_ops.unregister()
    sets_state.unregister()
    state.unregister()
