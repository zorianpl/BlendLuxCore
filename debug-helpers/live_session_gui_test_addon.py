"""
Live-session-across-separate-render()-calls test, GUI-driven.

THE QUESTION THIS ANSWERS: is keeping ONE live pyluxcore.RenderSession
alive across SEPARATE, native Blender render() invocations (i.e. real
"Render Animation" / Ctrl+F12, where Blender calls render() once per
frame itself) a hard LuxCore limitation, or was the earlier "v8"
crash (`session.BeginSceneEdit(): RuntimeError: invalid argument`,
see debug-helpers/persistent_data_gpu_test.py's module docstring) an
implementation bug -- e.g. caching the session on a RenderEngine
INSTANCE attribute, which may not survive across frames if Blender
creates a new Python RenderEngine instance per call. This test uses a
MODULE-LEVEL cache instead (the pattern already proven to work for
export/caches/persistent_scene_v1.py), and drives everything through
Blender's REAL native animation-render operator -- no custom internal
frame loop.

If this works: complete solution -- native GUI, real persistent-data
speedup (BeginSceneEdit/EndSceneEdit, camera-only, same as v23),
automatic compositor/denoise/save for free (same reasoning as
persistent_scene_v1: as long as we never do anything unusual across the
render() call boundary itself, Blender's own postprocess machinery
doesn't need to know or care what happened internally).

If this crashes the same way: confirms it's a genuine LuxCore-side
limitation, not fixable from the addon's Python code, and v23's
"everything in one continuous render() call" workaround is the correct,
necessary approach going forward.

SCOPE: camera-only refresh (same trade-off as v23 -- only the camera
re-parses on the live session after frame 1; everything else stays
frozen as first_run() exported it). This test is about proving/
disproving the session-lifecycle question first; extending it to also
cover flagged objects (like the abandoned v24 attempt) is a separate
step, only worth doing if this succeeds.

SAFETY: this is a small, standalone, disposable add-on, entirely
separate from the main BlendLuxCore-family install. It only IMPORTS
Exporter/FrameBufferFinal/utils.render from whichever BlendLuxCore-
family build is currently enabled (dynamic sys.modules lookup, same
pattern as the other debug-helpers scripts) -- it does not modify or
depend on any specific version/zip of that build, and registering it
does not touch the real "LUXCORE" engine at all.

Usage:
  1. Make sure a BlendLuxCore-family build (v23 or later, needs
     Exporter.update_camera_only()) is installed and enabled, and has
     run at least once this session (e.g. briefly switch viewport
     shading to LuxCore) so its modules are importable.
  2. Run this file ONCE from the Scripting tab. It registers the engine
     and two cleanup handlers -- nothing renders yet.
  3. From here on, everything is normal Blender GUI: Render Properties >
     Render Engine > "LuxCore Live Session Test (GUI)". Set your frame
     range/output as usual. Press Ctrl+F12 (Render Animation).
  4. Watch the System Console. Frame 1 prints "FULL export". Frame 2
     prints either "SUCCESS" (session reused, note the tiny edit time)
     or "FAILED" with the exact exception -- that failure, if it
     happens, is the answer to the open question above.
  5. To undo: run live_session_gui_test_unregister() from the Scripting
     tab (defined at the bottom of this file), or just restart Blender.
"""
import sys
import time

import bpy
from bpy.app.handlers import persistent


def _find_module(suffix, required_attr):
    for name, mod in list(sys.modules.items()):
        if name.endswith(suffix) and hasattr(mod, required_attr):
            return mod
    return None


export_mod = _find_module("export", "Exporter")
if export_mod is None or not hasattr(export_mod.Exporter, "update_camera_only"):
    raise RuntimeError(
        "Could not find export.Exporter.update_camera_only() -- make sure "
        "a v23-or-later BlendLuxCore-family build is installed, enabled, "
        "and has run at least once this session (e.g. briefly switch "
        "viewport shading to LuxCore) so its modules are imported."
    )
draw_final_mod = _find_module("draw.final", "FrameBufferFinal")
utils_render_mod = _find_module("utils.render", "update_stats")
utils_view_layer_mod = _find_module("utils.view_layer", "State")
engine_base_mod = _find_module("engine.base", "LuxCoreRenderEngine")
if engine_base_mod is None:
    raise RuntimeError("Could not find engine.base.LuxCoreRenderEngine.")
engine_final_mod = _find_module("engine.final", "render")
if engine_final_mod is None or not hasattr(engine_final_mod, "_add_passes"):
    raise RuntimeError("Could not find engine.final._add_passes.")

Exporter = export_mod.Exporter
FrameBufferFinal = draw_final_mod.FrameBufferFinal
utils_render = utils_render_mod
utils_view_layer = utils_view_layer_mod
LuxCoreRenderEngine = engine_base_mod.LuxCoreRenderEngine
_add_passes = engine_final_mod._add_passes


# Fallback verification save, independent of the Render Result window
# issue (see the note above the SaveOutput() call below). Set to "" to
# disable.
FALLBACK_SAVE_DIR = r"C:\temp\live_session_gui_test"

# Fallback sample count if scene.luxcore.halt has no usable condition
# configured (see _frame_halt_target() below) -- scene.luxcore.halt is
# force-disabled for this test (its own use_samples/samples ARE still
# read, just not its enable flag, see _frame_halt_target()), because the
# built-in halt mechanism itself is what latches HasDone() permanently
# True on a session reused across frames.
SAMPLES = 6

# {(scene_ptr, view_layer_name): {"exporter": Exporter, "session": RenderSession}}
_cache = {}


def _key(scene, view_layer_name):
    return (scene.original.as_pointer(), view_layer_name)


def _frame_halt_target(scene):
    """
    Read the user's actual Render Properties > LuxCore > Halt Conditions
    config and turn it into (target_samples, target_seconds) -- either
    may be None. scene.luxcore.halt.enable itself is ignored/forced off
    (see SAMPLES comment above); use_samples/use_time/samples/seconds are
    still read and respected. use_noise_thresh is NOT supported here
    (would need reading noise stats, not just the pass counter) -- if
    that's the ONLY condition configured, falls back to SAMPLES with a
    one-time warning.
    """
    h = scene.luxcore.halt
    target_samples = h.samples if h.use_samples else None
    target_seconds = h.time if h.use_time else None

    if target_samples is None and target_seconds is None:
        if h.use_noise_thresh:
            print(
                "[live-session-test] scene.luxcore.halt only has Noise "
                "Threshold configured -- not supported by this test's "
                f"Python-side halt check, falling back to SAMPLES={SAMPLES}"
            )
        target_samples = SAMPLES

    return target_samples, target_seconds


def _current_pass(session):
    session.UpdateStats()
    return session.GetStats().Get("stats.renderengine.pass").GetInt()


def _stop_all():
    for entry in _cache.values():
        try:
            entry["session"].Stop()
        except Exception:
            pass
    _cache.clear()


class LUXCORE_LIVE_SESSION_TEST(LuxCoreRenderEngine):
    """
    Inherits from the REAL LuxCoreRenderEngine (engine/base.py) instead
    of bare bpy.types.RenderEngine -- FrameBufferFinal.draw() assumes a
    full LuxCoreRenderEngine (engine.exporter, engine.has_denoiser(),
    engine.DENOISED_OUTPUT_NAME, engine.aov_imagepipelines, ...), and
    patching each missing attribute one at a time (as this file did
    earlier today) is a losing game -- inheriting gets all of it at
    once, correctly, for free. Only render() is overridden; everything
    else (reset(), has_denoiser(), update_render_passes(), __del__, ...)
    is the exact same code the real "LUXCORE" engine uses.
    """
    bl_idname = "LUXCORE_LIVE_SESSION_TEST"
    bl_label = "LuxCore Live Session Test (GUI, camera-only)"
    bl_use_preview = False  # this test's render() does not handle is_preview

    def render(self, depsgraph):
        # NOT called unconditionally here -- reset() wipes
        # self.aov_imagepipelines back to {} (among other things), and
        # that dict is only ever (re)populated during a FULL export
        # (config.convert()/aovs.convert(), inside create_session()) --
        # update_camera_only() never touches it. Calling reset() on every
        # frame, including reused ones, silently broke
        # self.has_denoiser() (and AOV passes) on every frame after the
        # first. Called instead right before each FULL_EXPORT branch
        # below, matching how it should only run when we're actually
        # about to (re)export.
        frame_t0 = time.time()
        scene_eval = depsgraph.scene_eval
        view_layer_eval = depsgraph.view_layer_eval
        layer = scene_eval.original.view_layers.get(view_layer_eval.name)

        # Registers custom passes (DENOISED, AOVs, light groups) on the
        # result BEFORE begin_result() builds it -- without this,
        # render_layer.passes["DENOISED"] doesn't exist at all and
        # _import_aov() fails, independent of has_denoiser()/
        # aov_imagepipelines being correct. engine/final.py's real
        # render() calls this every frame too, not just once.
        _add_passes(self, layer, scene_eval)

        dummy_result = self.begin_result(0, 0, 1, 1, layer=layer.name)
        self.end_result(dummy_result, cancel=True, do_merge_results=False)

        # FrameBufferFinal.draw() reads this to know which layer name to
        # deliver the result under -- without it (missing here before)
        # it stays at whatever stale/unset value it had, draw() hits a
        # KeyError, falls back to layer="" and the result never lands in
        # the right place. Matches engine/final.py's real render().
        utils_view_layer.State.active_view_layer = layer.name

        key = _key(scene_eval, view_layer_eval.name)
        entry = _cache.get(key)

        export_t0 = time.time()
        if entry is None:
            self.reset()
            print(f"[live-session-test] frame={scene_eval.frame_current}: "
                  f"no cached session -- FULL export")
            exporter = Exporter()
            exporter.persistent_data_animation = True
            session = exporter.create_session(
                depsgraph, context=None, engine=self, view_layer=view_layer_eval,
            )
            if session is None:
                print("[live-session-test] export cancelled by user")
                return
            session.Start()
            # self.aov_imagepipelines (and self.exporter below) are
            # INSTANCE attributes -- LuxCoreRenderEngine.__init__() itself
            # calls self.reset() (which sets self.aov_imagepipelines = {}),
            # so if Blender constructs a FRESH Python engine instance for
            # the next frame's separate render() call (confirmed likely,
            # same reason self.exporter needs re-setting every frame
            # below), this dict is wiped again before we ever see it --
            # completely independent of whether *we* call reset(). Cache
            # it at module level too, alongside exporter/session, and
            # restore it every frame (see the REUSED_LIVE_SESSION branch)
            # instead of assuming it survives on its own.
            _cache[key] = {
                "exporter": exporter,
                "session": session,
                "aov_imagepipelines": dict(self.aov_imagepipelines),
            }
            mode = "FULL_EXPORT"
        else:
            exporter = entry["exporter"]
            session = entry["session"]
            print(f"[live-session-test] frame={scene_eval.frame_current}: "
                  f"reusing session from a PREVIOUS, SEPARATE render() call "
                  f"-- attempting BeginSceneEdit...")
            try:
                session = exporter.update_camera_only(depsgraph, session)
                session.GetFilm().Clear()
                _cache[key]["session"] = session
                mode = "REUSED_LIVE_SESSION"
                print("[live-session-test] SUCCESS -- BeginSceneEdit worked "
                      "across separate render() calls")
            except Exception as error:
                import traceback
                print(f"[live-session-test] FAILED: {type(error).__name__}: {error}")
                traceback.print_exc()
                print("[live-session-test] this is the answer to the open "
                      "question -- falling back to a fresh session this frame")
                try:
                    session.Stop()
                except Exception:
                    pass
                self.reset()
                exporter = Exporter()
                exporter.persistent_data_animation = True
                session = exporter.create_session(
                    depsgraph, context=None, engine=self, view_layer=view_layer_eval,
                )
                if session is None:
                    del _cache[key]
                    return
                session.Start()
                _cache[key] = {
                    "exporter": exporter,
                    "session": session,
                    "aov_imagepipelines": dict(self.aov_imagepipelines),
                }
                mode = "FULL_EXPORT_AFTER_FAILED_REUSE"
        export_time = time.time() - export_t0

        if mode == "REUSED_LIVE_SESSION":
            # See the FULL_EXPORT branch's comment: restore the AOV/
            # denoiser imagepipeline registration cached there, since a
            # fresh engine instance's own __init__/reset() wiped this
            # instance's copy before render() was even called.
            self.aov_imagepipelines = dict(entry.get("aov_imagepipelines", {}))

        # FrameBufferFinal.draw() reads engine.exporter (AOV/lightgroup
        # import, engine.exporter.lightgroup_cache) -- the real engine
        # sets this on itself in _render_layer(); this script kept the
        # exporter in the module-level _cache instead, so draw() never
        # found it here. Same "engine object doesn't fully match what
        # draw() assumes" pattern as active_view_layer earlier.
        self.exporter = exporter
        # _refresh_denoiser() (draw/final.py) reads engine.session, not
        # the `session` local/parameter -- but LuxCoreRenderEngine.__del__
        # calls self.session.Stop() whenever THIS Python engine instance
        # is garbage collected, which would kill our module-cached, still
        # -needed-next-frame session if it stayed set across separate
        # render() calls. Set only for the duration of this render() call,
        # explicitly cleared again at the end (see below) before we
        # return, so a __del__ between frames (if Blender ever collects
        # this instance) finds nothing to stop.
        self.session = session
        print(f"[live-session-test] has_denoiser()={self.has_denoiser()} "
              f"aov_imagepipelines={self.aov_imagepipelines} "
              f"scene.luxcore.denoiser.enabled={scene_eval.luxcore.denoiser.enabled}")

        # NOT session.HasDone() -- confirmed (2026-09-23) to latch True
        # permanently on a session reused across frames, same as the "v8"
        # issue documented in persistent_data_gpu_test.py: surfaced here
        # as the loop exiting before a single sample was computed since
        # Film.Clear(), then draw() raising "Film Output not available"
        # because the imagepipeline buffer had nothing in it yet. Count
        # passes in Python instead, same fix as v23/v24.
        def _safe_draw(render_stopped):
            # "Film Output not available: 3" (2026-09-23): unlike the
            # normal engine/final.py loop, GetOutputFloat(RGB_IMAGEPIPELINE)
            # here finds nothing to read -- SaveOutput() below succeeds
            # for the exact same output type, so the underlying radiance
            # data is fine, it's specifically the precomputed imagepipeline
            # buffer GetOutputFloat() reads that's stale/empty after
            # BeginSceneEdit/EndSceneEdit + Film.Clear() on a reused
            # session. Force it to (re)compute first, same call the
            # denoiser path already uses for its own imagepipeline index
            # (_refresh_denoiser(), index != 0) -- here for the main
            # combined pass, index 0.
            try:
                film = session.GetFilm()
                film.AsyncExecuteImagePipeline(0)
                wait_start = time.time()
                while not film.HasDoneAsyncExecuteImagePipeline():
                    if time.time() - wait_start > 10:
                        break
                    time.sleep(0.05)
            except Exception as error:
                print(f"[live-session-test] AsyncExecuteImagePipeline(0) "
                      f"failed (non-fatal): {type(error).__name__}: {error}")
            framebuffer.draw(self, session, depsgraph.scene, render_stopped=render_stopped)

        target_samples, target_seconds = _frame_halt_target(scene_eval)

        framebuffer = FrameBufferFinal(scene_eval)
        render_t0 = time.time()
        pass_before = _current_pass(session)
        target_pass = pass_before + target_samples if target_samples is not None else None
        last_draw = 0.0
        while True:
            if self.test_break():
                break
            pass_now = _current_pass(session)
            now = time.time()
            if now - last_draw > 0.5:
                # Progressive live preview -- best-effort only, see note
                # below. Never let a display failure abort the frame.
                try:
                    _safe_draw(render_stopped=False)
                except Exception as error:
                    print(f"[live-session-test] progressive draw() failed "
                          f"(non-fatal): {type(error).__name__}: {error}")
                last_draw = now
            samples_done = target_pass is not None and pass_now >= target_pass
            time_done = target_seconds is not None and (now - render_t0) >= target_seconds
            if samples_done or time_done:
                break
            time.sleep(1 / 10)
        render_time = time.time() - render_t0

        # Fallback verification FIRST, before the (currently unreliable,
        # see draw() try/except above and below) Render Result display --
        # this way it still happens even if draw() throws. Same direct
        # film save already proven reliable in v23/v24. Purely for you to
        # visually confirm each frame; unrelated to the session-reuse
        # question itself (already answered: SUCCESS).
        if FALLBACK_SAVE_DIR:
            try:
                import os
                import pyluxcore
                os.makedirs(FALLBACK_SAVE_DIR, exist_ok=True)
                out_path = os.path.join(
                    FALLBACK_SAVE_DIR, f"frame{scene_eval.frame_current:04d}.png"
                )
                session.GetFilm().SaveOutput(
                    out_path, pyluxcore.FilmOutputType.RGB_IMAGEPIPELINE, pyluxcore.Properties()
                )
                print(f"[live-session-test] fallback save: {out_path}")
            except Exception as error:
                print(f"[live-session-test] fallback save FAILED: "
                      f"{type(error).__name__}: {error}")

        # Final Render Result display attempt -- best-effort, same reason
        # as the progressive one above.
        try:
            _safe_draw(render_stopped=True)
        except Exception as error:
            print(f"[live-session-test] final draw() failed (non-fatal): "
                  f"{type(error).__name__}: {error}")

        # Deliberately do NOT call session.Stop() here -- that is the whole
        # point of this test. Cleanup happens in the render_complete/
        # render_cancel handlers below, once the WHOLE animation is done.

        print(f"[live-session-test] frame={scene_eval.frame_current:<4} "
              f"export={export_time:6.3f}s render={render_time:6.3f}s "
              f"total={time.time() - frame_t0:6.3f}s mode={mode}")

        # Clear again (see the comment where this was set above) so this
        # engine instance's __del__ has nothing to Stop() if Blender
        # garbage-collects it before the next frame's render() call --
        # the module-level _cache, not this attribute, owns the session's
        # lifecycle across frames.
        self.session = None


@persistent
def _on_render_complete(scene):
    print("[live-session-test] render complete -- stopping cached session(s)")
    _stop_all()


@persistent
def _on_render_cancel(scene):
    print("[live-session-test] render cancelled -- stopping cached session(s)")
    _stop_all()


def live_session_gui_test_register():
    if hasattr(bpy.types, "LUXCORE_LIVE_SESSION_TEST"):
        bpy.utils.unregister_class(bpy.types.LUXCORE_LIVE_SESSION_TEST)
    bpy.utils.register_class(LUXCORE_LIVE_SESSION_TEST)
    if _on_render_complete not in bpy.app.handlers.render_complete:
        bpy.app.handlers.render_complete.append(_on_render_complete)
    if _on_render_cancel not in bpy.app.handlers.render_cancel:
        bpy.app.handlers.render_cancel.append(_on_render_cancel)

    # scene.luxcore.halt's built-in ENGINE-SIDE halt mechanism (the
    # "enable" flag, which bakes haltspp/halttime into the RenderConfig)
    # is what latches HasDone() permanently True on a session reused
    # across frames -- force just that flag off. Its use_samples/samples
    # and use_time/time values are still read every frame by
    # _frame_halt_target() and enforced in Python instead (see the
    # sampling loop in render()), so your configured "how many samples /
    # how long per frame" is respected -- only the broken engine-side
    # HasDone() latch is bypassed. This DOES modify scene data (not just
    # this test's in-memory state) -- if you go back to the real LUXCORE
    # engine afterwards, check Render Properties > LuxCore > Halt
    # Conditions and re-enable it there.
    h = bpy.context.scene.luxcore.halt
    if h.enable:
        print(
            "[live-session-test] disabling scene.luxcore.halt.enable "
            "(was on) -- its use_samples/use_time values are still "
            "respected, only the engine-side HasDone() mechanism is "
            "bypassed (Python-side pass/time counting instead); "
            "re-enable the flag manually if you switch back to the real "
            "LUXCORE engine"
        )
        h.enable = False

    print(
        "\n=== live-session GUI test registered ===\n"
        "Render Properties > Render Engine > "
        "'LuxCore Live Session Test (GUI, camera-only)'\n"
        "Set your frame range/output, then Ctrl+F12 (Render Animation).\n"
        "Watch the System Console for FULL_EXPORT / REUSED_LIVE_SESSION / "
        "FAILED per frame.\n"
    )


def live_session_gui_test_unregister():
    _stop_all()
    if _on_render_complete in bpy.app.handlers.render_complete:
        bpy.app.handlers.render_complete.remove(_on_render_complete)
    if _on_render_cancel in bpy.app.handlers.render_cancel:
        bpy.app.handlers.render_cancel.remove(_on_render_cancel)
    if hasattr(bpy.types, "LUXCORE_LIVE_SESSION_TEST"):
        bpy.utils.unregister_class(bpy.types.LUXCORE_LIVE_SESSION_TEST)
    print("=== live-session GUI test unregistered ===")


live_session_gui_test_register()
