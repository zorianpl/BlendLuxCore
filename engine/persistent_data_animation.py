"""
Persistent Data (Animation): keeps ONE live pyluxcore.RenderSession alive
across SEPARATE render() calls (one per animation frame), re-parsing
only the camera plus any obj.luxcore.always_reexport-flagged
objects/instancing-parents each frame (Exporter.update_flagged_only(),
a superset of update_camera_only() -- see its docstring in export/
__init__.py) instead of re-exporting/re-uploading the whole scene.
Gated by the scene.luxcore.config.use_persistent_data_animation
checkbox (off by default) -- see engine/final.py's _render_layer(),
which dispatches here instead of the normal path only when that
checkbox is on AND engine.is_animation is True.

See PERSISTENT_DATA_ANIMATION_NOTES.md for the full history and the
standalone test scripts this was proven in first
(live_session_gui_test_addon.py and friends).

KNOWN LIMITATIONS (opt-in only, normal rendering is entirely unaffected):
- Only the camera and objects/instancing-parents with
  obj.luxcore.always_reexport enabled ("Always Re-check (Persistent
  Data)" in Object Properties > LuxCore) can move/change between
  frames. Everything else is exported once on frame 1 and then frozen.
- update_flagged_only() is untested for Geometry-Nodes-driven instance-
  COUNT changes on a flagged group (that combination hit a different,
  unrelated bug -- batch obj_key mismatch on delete -- when this method
  was first written, see the notes) -- fine for a plain moving/rotating/
  scaling mesh, not yet verified for a flagged GN duplicator whose
  instance count varies frame to frame.
- No live mid-frame scene editing: the normal
  engine.exporter.get_changes()/update_session() polling
  engine/final.py's _render_layer() does every loop iteration is
  skipped here on purpose, because it goes through ObjectCache2.
  update(), which is slower than a full first_run() on a heavily
  batched scene (confirmed, see the notes) -- exactly what this feature
  exists to avoid.
"""
from time import time, sleep

_needs_reload = "bpy" in locals()

from .. import export
from ..draw.final import FrameBufferFinal
from ..utils import render as utils_render
from ..utils.errorlog import LuxCoreErrorLog
from ..properties.denoiser import LuxCoreDenoiser
from ..properties.display import LuxCoreDisplaySettings

if _needs_reload:
    import importlib
    importlib.reload(export)
    importlib.reload(utils_render)


# {(scene_ptr, view_layer_name): {"exporter": Exporter, "session":
# RenderSession, "aov_imagepipelines": {...}}}.
# Module-level (not an engine attribute) because Blender constructs a
# new Python RenderEngine instance for each separate render() call
# during an animation render -- this is what actually needs to survive
# across frames.
_cache = {}


def _key(scene, view_layer_name):
    return (scene.original.as_pointer(), view_layer_name)


def stop_sessions():
    """
    Stop and drop any cached Persistent Data (Animation) sessions.
    Called from the render_complete/render_cancel handlers (handlers/
    render_complete.py) once a WHOLE animation render finishes or is
    cancelled -- never per frame, since keeping the session alive across
    frames is the entire point.
    """
    for entry in _cache.values():
        try:
            entry["session"].Stop()
        except Exception:
            pass
    _cache.clear()


def _stop_requested(engine):
    return engine.test_break() or LuxCoreDisplaySettings.stop_requested


def _stat_refresh_interval(start, scene):
    width, height = utils_render.calc_filmsize(scene)
    is_big_image = width * height > 2000 * 2000
    minimum = 4 if is_big_image else 1
    maximum = 16

    minutes = (time() - start) / 60
    if minutes < 4:
        return max(2**minutes, minimum)
    else:
        return maximum


def _halt_target(scene):
    """
    Read the user's Halt Conditions (samples/time) without relying on
    LuxCore's own engine-side halt mechanism -- that mechanism latches
    session.HasDone() permanently True once reached, which would
    prevent ever resuming sampling on a session reused for a later
    frame. Falls back to a small, safe sample count if neither is
    configured (should not normally happen -- engine/final.py's
    _check_halt_conditions() already requires one for an animation
    render).
    """
    h = scene.luxcore.halt
    target_samples = h.samples if h.use_samples else None
    target_seconds = h.time if h.use_time else None
    if target_samples is None and target_seconds is None:
        target_samples = 16
    return target_samples, target_seconds


def _create_session(engine, depsgraph, statistics, view_layer, key):
    """
    Full export + session creation, shared by the first frame and by
    the fallback-after-failed-reuse path. Temporarily disables
    scene.luxcore.halt.enable for the duration of THIS export only
    (restored immediately after, regardless of outcome) so LuxCore's
    own engine-side halt condition never gets baked into the
    RenderConfig -- see _halt_target()'s docstring for why. Returns
    True if a session was created and cached, False if the user
    cancelled the export.
    """
    engine.reset()
    engine.exporter = export.Exporter(statistics)
    engine.exporter.persistent_data_animation = True

    scene = depsgraph.scene_eval
    halt = scene.luxcore.halt
    halt_enable_backup = halt.enable
    halt.enable = False
    try:
        engine.session = engine.exporter.create_session(
            depsgraph, engine=engine, view_layer=view_layer
        )
    finally:
        halt.enable = halt_enable_backup

    if engine.session is None:
        print("[Engine/PersistentDataAnimation] Export cancelled by user.")
        return False

    start = time()
    engine.session.Start()
    statistics.session_init_time.value = time() - start

    # engine.exporter.flagged_reexport_keys is already correctly
    # populated at this point -- first_run() (called inside
    # create_session() above) records each flagged group's key itself,
    # at the exact moment it creates it (see ObjectCache2.first_run(),
    # object_cache.py, and Exporter.update_flagged_only()'s docstring
    # for why a separate, later re-derivation was tried first and
    # confirmed unreliable).

    _cache[key] = {
        "exporter": engine.exporter,
        "session": engine.session,
        "aov_imagepipelines": dict(engine.aov_imagepipelines),
    }
    return True


def render_layer(engine, depsgraph, statistics, view_layer):
    """Entry point called from engine/final.py's _render_layer()."""
    scene = depsgraph.scene_eval
    key = _key(scene, view_layer.name)
    cached = _cache.get(key)
    mode = None
    reuse_failed = False

    if cached is not None:
        engine.exporter = cached["exporter"]
        engine.aov_imagepipelines = dict(cached["aov_imagepipelines"])
        try:
            engine.session = engine.exporter.update_flagged_only(
                depsgraph, cached["session"], view_layer
            )
            engine.session.GetFilm().Clear()
            cached["session"] = engine.session
            statistics.session_init_time.value = 0
            mode = "REUSED_LIVE_SESSION"
        except Exception as error:
            import traceback
            traceback.print_exc()
            LuxCoreErrorLog.add_warning(
                "Persistent Data (Animation): failed to reuse the live "
                f"session ({error}) -- falling back to a full export "
                "for this frame."
            )
            try:
                cached["session"].Stop()
            except Exception:
                pass
            del _cache[key]
            reuse_failed = True

    if mode is None:
        if not _create_session(engine, depsgraph, statistics, view_layer, key):
            return
        mode = "FULL_EXPORT_AFTER_FAILED_REUSE" if reuse_failed else "FULL_EXPORT"

    engine.framebuffer = FrameBufferFinal(scene)
    session_config = engine.session.GetRenderConfig()

    target_samples, target_seconds = _halt_target(scene)

    def _current_stats():
        # UpdateStats() is the call that actually refreshes the numbers
        # (session.GetStats() alone just returns a handle to whatever
        # was last computed) -- called every loop tick (~10/s), same
        # cadence already proven fine by live_session_gui_test_addon.py,
        # since halt timing here is entirely Python-side (no
        # engine-side halt condition is baked in for this mode).
        engine.session.UpdateStats()
        return engine.session.GetStats()

    pass_before = _current_stats().Get("stats.renderengine.pass").GetInt()
    target_pass = pass_before + target_samples if target_samples is not None else None
    render_t0 = time()
    last_draw = 0.0
    last_stat_refresh = 0.0
    fast_refresh_duration = 1 if engine.is_animation else 5

    while True:
        if _stop_requested(engine):
            break
        stats = _current_stats()
        pass_now = stats.Get("stats.renderengine.pass").GetInt()
        now = time()
        manual_refresh_requested = LuxCoreDisplaySettings.refresh or LuxCoreDenoiser.refresh
        fast_refresh = now - render_t0 < fast_refresh_duration

        # Throttle only the UI text/progress-bar formatting (cheap, but
        # no need to redo it 10x/s) -- this is what actually populates
        # engine.exporter.stats, i.e. the Statistics panel.
        if fast_refresh or manual_refresh_requested or (now - last_stat_refresh) > _stat_refresh_interval(render_t0, scene):
            utils_render.update_status_msg(
                stats, engine, depsgraph.scene, session_config, time_until_film_refresh=0
            )
            last_stat_refresh = now

        if now - last_draw > fast_refresh_duration or manual_refresh_requested:
            engine.framebuffer.draw(engine, engine.session, depsgraph.scene, render_stopped=False)
            last_draw = now
        samples_done = target_pass is not None and pass_now >= target_pass
        time_done = target_seconds is not None and (now - render_t0) >= target_seconds
        if samples_done or time_done:
            break
        sleep(1 / 10)

    utils_render.update_status_msg(
        _current_stats(), engine, depsgraph.scene, session_config, time_until_film_refresh=0
    )
    engine.framebuffer.draw(engine, engine.session, depsgraph.scene, render_stopped=True)

    print(f"[Engine/PersistentDataAnimation] frame={scene.frame_current} "
          f"mode={mode} total={time() - render_t0:.3f}s")

    # Deliberately do NOT Stop()/del engine.session here -- that is the
    # whole point. The session stays alive in _cache until the whole
    # animation finishes or is cancelled (see stop_sessions()). Only
    # clear the reference on THIS (possibly about-to-be-destroyed)
    # engine instance, same reasoning as engine/base.py's __del__.
    engine.session = None
