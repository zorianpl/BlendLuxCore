_needs_reload = "bpy" in locals()

import bpy
from bpy.app.handlers import persistent
from .. import engine

if _needs_reload:
    import importlib
    importlib.reload(engine)


@persistent
def handler(scene):
    """
    Stops and drops any Persistent Data (Animation) sessions still
    cached in engine.persistent_data_animation once a WHOLE render
    (single frame or animation) finishes or is cancelled. A no-op
    unless the Persistent Data (Animation) checkbox was actually used,
    since the cache is empty otherwise. See engine/
    persistent_data_animation.py's stop_sessions() docstring.
    """
    engine.persistent_data_animation.stop_sessions()
