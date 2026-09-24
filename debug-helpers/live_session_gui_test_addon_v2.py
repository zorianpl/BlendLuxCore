"""
Live-session-across-separate-render()-calls test, GUI-driven -- v2.

Same base test as live_session_gui_test_addon.py (v1): keeping ONE live
pyluxcore.RenderSession alive across SEPARATE, native Blender render()
invocations (real "Render Animation" / Ctrl+F12), driven entirely through
Blender's real animation-render operator, module-level cache instead of
a RenderEngine instance attribute. See v1's docstring for the full
background on why (session-lifecycle question, camera-only scope, etc.)
-- unchanged here, copy-pasted structure on purpose so this stays a
close, easy-to-diff mirror of v1 rather than a rewrite.

ONLY DIFFERENCE FROM v1: the Denoiser panel (ui/render/denoiser.py) hard-
checks `context.scene.render.engine == "LUXCORE"` in its own poll(), so
it never shows up for this test engine (bl_idname
"LUXCORE_LIVE_SESSION_TEST") -- you could not tick "enabled" or click
"Refresh" from the UI at all with v1. This version monkeypatches that
panel's poll() (and its BCD-advanced sub-panel) to also accept this test
engine, restored on unregister -- same register-time-patch-and-revert
shape already used elsewhere in this codebase (ui/world.py, ui/
particle.py toggle COMPAT_ENGINES the same way for a different reason).
Nothing about the real "LUXCORE" engine changes.

The refresh button just sets LuxCoreDenoiser.refresh = True (a class-
level flag, operators/render.py); the render loop's existing periodic
draw call already polls that flag every ~0.5s, so no other code needed
to change for a manual refresh to reach the live/reused session -- only
the button needed to become visible/clickable. Added one diagnostic
print, right before that periodic draw call, that fires only when a
refresh is pending: which frame/pass/mode it's about to apply to, so you
can compare that directly against what shows up in the Render Result /
fallback PNG.

WARNING: clicking "Refresh" while frame 2+ is rendering (mode
REUSED_LIVE_SESSION) exercises session.Pause()/Parse()/
AsyncExecuteImagePipeline()/Resume() (draw/final.py's
_refresh_denoiser()) on a session that went through BeginSceneEdit/
EndSceneEdit on a PREVIOUS, separate render() call -- a combination v1
never actually exercised, since the button was never reachable. This is
exactly the untested combination you're trying to check, so a pyluxcore-
side crash here (not just a Python exception) is a real possibility,
same family as the earlier "BeginSceneEdit(): invalid argument" issue --
if Blender itself crashes/closes when you click Refresh on frame 2+,
that IS the answer (a genuine LuxCore-side limitation), not a bug in
this script to chase further.

2026-09-24: a real EXCEPTION_ACCESS_VIOLATION was hit WITHOUT clicking
Refresh at all -- just moving the mouse over the GUI / selecting this
engine in the Render Engine dropdown. Root cause, not yet confirmed by
a repro but very likely: LUXCORE_LIVE_SESSION_TEST inherits
LuxCoreRenderEngine.view_update()/view_draw() (engine/base.py) as-is --
that's the REAL viewport-rendering code path (its own independent
pyluxcore session, nothing to do with this file's module-level _cache),
which Blender calls automatically whenever a 3D viewport is in
"Rendered" shading mode with this engine selected, regardless of
Ctrl+F12. bl_use_preview=False (below) does NOT block this -- it only
blocks material-preview thumbnails. This was never exercised/tested for
this engine and is a much simpler, much more likely crash trigger than
the denoiser-refresh path above. Fixed by overriding view_update()/
view_draw() as no-ops (see below) -- if it crashes again with these
overrides in place, the viewport path is ruled out and the denoiser-
refresh path (or something else) becomes the leading suspect again.

Usage: identical to v1 -- run once from the Scripting tab, then Render
Properties > Render Engine > "LuxCore Live Session Test (GUI, camera-
only)". Render Properties > Denoiser is now visible for this engine too
-- tick "enabled" BEFORE starting the render (read once, at frame 1's
full export), set frame range, Ctrl+F12, click "Refresh" whenever you
like once rendering, watch the System Console.
To undo: run live_session_gui_test_v2_unregister() from the Scripting
tab, or just restart Blender.
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
ui_denoiser_mod = _find_module("ui.render.denoiser", "LUXCORE_RENDER_PT_denoiser")
properties_denoiser_mod = _find_module("properties.denoiser", "LuxCoreDenoiser")
if properties_denoiser_mod is None:
    raise RuntimeError("Could not find properties.denoiser.LuxCoreDenoiser.")

Exporter = export_mod.Exporter
FrameBufferFinal = draw_final_mod.FrameBufferFinal
utils_render = utils_render_mod
utils_view_layer = utils_view_layer_mod
LuxCoreRenderEngine = engine_base_mod.LuxCoreRenderEngine
_add_passes = engine_final_mod._add_passes
LuxCoreDenoiser = properties_denoiser_mod.LuxCoreDenoiser


# Fallback verification save, independent of the Render Result window
# issue (see the note above the SaveOutput() call below). Set to "" to
# disable.
FALLBACK_SAVE_DIR = r"C:\temp\live_session_gui_test_v2"

# Fallback sample count if scene.luxcore.halt has no usable condition
# configured (see _frame_halt_target() below).
SAMPLES = 6

# {(scene_ptr, view_layer_name): {"exporter": Exporter, "session": RenderSession}}
_cache = {}

_DENOISER_PATCH_MARKER = "_v2_orig_poll"


def _key(scene, view_layer_name):
    return (scene.original.as_pointer(), view_layer_name)


def _frame_halt_target(scene):
    h = scene.luxcore.halt
    target_samples = h.samples if h.use_samples else None
    target_seconds = h.time if h.use_time else None

    if target_samples is None and target_seconds is None:
        if h.use_noise_thresh:
            print(
                "[live-session-test-v2] scene.luxcore.halt only has Noise "
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


def _patch_denoiser_panels():
    """Make ui/render/denoiser.py's panels show up for this test engine
    too, so the Denoiser "enabled" checkbox and "Refresh" button are
    reachable from the normal Render Properties UI. Idempotent -- stores
    the original poll() as an attribute ON THE CLASS ITSELF (not a
    module-level dict, which would reset on every "Run Script" while the
    live, already-patched class persists in bpy.types across reruns) so
    re-running this script without restarting Blender does not stack
    multiple wrapper layers. Reversible -- see _unpatch_denoiser_panels().
    """
    if ui_denoiser_mod is None:
        print(
            "[live-session-test-v2] could not find ui.render.denoiser -- "
            "the Denoiser panel will stay hidden for this test engine"
        )
        return

    for cls in (
        getattr(ui_denoiser_mod, "LUXCORE_RENDER_PT_denoiser", None),
        getattr(ui_denoiser_mod, "LUXCORE_RENDER_PT_denoiser_bcd_advanced", None),
    ):
        if cls is None or hasattr(cls, _DENOISER_PATCH_MARKER):
            continue
        orig_func = cls.__dict__["poll"].__func__  # raw function(cls, context)
        setattr(cls, _DENOISER_PATCH_MARKER, orig_func)

        def poll(kls, context, _orig=orig_func):
            return (
                _orig(kls, context)
                or context.scene.render.engine == "LUXCORE_LIVE_SESSION_TEST"
            )

        cls.poll = classmethod(poll)
        print(f"[live-session-test-v2] patched {cls.__name__}.poll()")


def _unpatch_denoiser_panels():
    if ui_denoiser_mod is None:
        return
    for cls in (
        getattr(ui_denoiser_mod, "LUXCORE_RENDER_PT_denoiser", None),
        getattr(ui_denoiser_mod, "LUXCORE_RENDER_PT_denoiser_bcd_advanced", None),
    ):
        orig_func = getattr(cls, _DENOISER_PATCH_MARKER, None) if cls else None
        if orig_func is None:
            continue
        cls.poll = classmethod(orig_func)
        delattr(cls, _DENOISER_PATCH_MARKER)


class LUXCORE_LIVE_SESSION_TEST(LuxCoreRenderEngine):
    """
    Inherits from the REAL LuxCoreRenderEngine (engine/base.py) instead
    of bare bpy.types.RenderEngine -- see v1's class docstring for why.
    Only render() is overridden.
    """
    bl_idname = "LUXCORE_LIVE_SESSION_TEST"
    bl_label = "LuxCore Live Session Test (GUI, camera-only)"
    bl_use_preview = False  # this test's render() does not handle is_preview

    def view_update(self, context, depsgraph):
        # bl_use_preview=False only blocks material-preview thumbnails --
        # it does NOT stop the 3D viewport's "Rendered" shading mode from
        # calling this. LuxCoreRenderEngine.view_update()/view_draw()
        # (inherited from engine/base.py) would otherwise run the REAL
        # viewport-rendering code path (its own pyluxcore session,
        # completely separate from and unsynchronized with this file's
        # module-level _cache) just from moving the mouse over a viewport
        # in Rendered shading, or from switching to this engine while a
        # viewport is already in that mode -- never exercised/tested for
        # this engine and a likely cause of a native crash. Override both
        # as safe no-ops instead of inheriting the real implementation.
        print(
            "[live-session-test-v2] view_update() called -- refusing to "
            "run real viewport rendering for this test engine. Set this "
            "viewport's shading to Solid/Material Preview, not Rendered; "
            "use Ctrl+F12 (Render Animation) instead."
        )

    def view_draw(self, context, depsgraph):
        pass

    def render(self, depsgraph):
        frame_t0 = time.time()
        scene_eval = depsgraph.scene_eval
        view_layer_eval = depsgraph.view_layer_eval
        layer = scene_eval.original.view_layers.get(view_layer_eval.name)

        _add_passes(self, layer, scene_eval)

        dummy_result = self.begin_result(0, 0, 1, 1, layer=layer.name)
        self.end_result(dummy_result, cancel=True, do_merge_results=False)

        utils_view_layer.State.active_view_layer = layer.name

        key = _key(scene_eval, view_layer_eval.name)
        entry = _cache.get(key)

        export_t0 = time.time()
        if entry is None:
            self.reset()
            print(f"[live-session-test-v2] frame={scene_eval.frame_current}: "
                  f"no cached session -- FULL export")
            exporter = Exporter()
            exporter.persistent_data_animation = True
            session = exporter.create_session(
                depsgraph, context=None, engine=self, view_layer=view_layer_eval,
            )
            if session is None:
                print("[live-session-test-v2] export cancelled by user")
                return
            session.Start()
            _cache[key] = {
                "exporter": exporter,
                "session": session,
                "aov_imagepipelines": dict(self.aov_imagepipelines),
            }
            mode = "FULL_EXPORT"
        else:
            exporter = entry["exporter"]
            session = entry["session"]
            print(f"[live-session-test-v2] frame={scene_eval.frame_current}: "
                  f"reusing session from a PREVIOUS, SEPARATE render() call "
                  f"-- attempting BeginSceneEdit...")
            try:
                session = exporter.update_camera_only(depsgraph, session)
                session.GetFilm().Clear()
                _cache[key]["session"] = session
                mode = "REUSED_LIVE_SESSION"
                print("[live-session-test-v2] SUCCESS -- BeginSceneEdit worked "
                      "across separate render() calls")
            except Exception as error:
                import traceback
                print(f"[live-session-test-v2] FAILED: {type(error).__name__}: {error}")
                traceback.print_exc()
                print("[live-session-test-v2] this is the answer to the open "
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
            self.aov_imagepipelines = dict(entry.get("aov_imagepipelines", {}))

        self.exporter = exporter
        self.session = session
        print(f"[live-session-test-v2] has_denoiser()={self.has_denoiser()} "
              f"aov_imagepipelines={self.aov_imagepipelines} "
              f"scene.luxcore.denoiser.enabled={scene_eval.luxcore.denoiser.enabled}")

        def _safe_draw(render_stopped):
            try:
                film = session.GetFilm()
                film.AsyncExecuteImagePipeline(0)
                wait_start = time.time()
                while not film.HasDoneAsyncExecuteImagePipeline():
                    if time.time() - wait_start > 10:
                        break
                    time.sleep(0.05)
            except Exception as error:
                print(f"[live-session-test-v2] AsyncExecuteImagePipeline(0) "
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
                if LuxCoreDenoiser.refresh:
                    # Only new print vs. v1 -- see module docstring.
                    print(
                        "[live-session-test-v2] denoiser refresh requested "
                        f"-- about to apply it to frame={scene_eval.frame_current} "
                        f"pass={pass_now} mode={mode}"
                    )
                try:
                    _safe_draw(render_stopped=False)
                except Exception as error:
                    print(f"[live-session-test-v2] progressive draw() failed "
                          f"(non-fatal): {type(error).__name__}: {error}")
                last_draw = now
            samples_done = target_pass is not None and pass_now >= target_pass
            time_done = target_seconds is not None and (now - render_t0) >= target_seconds
            if samples_done or time_done:
                break
            time.sleep(1 / 10)
        render_time = time.time() - render_t0

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
                print(f"[live-session-test-v2] fallback save: {out_path}")
            except Exception as error:
                print(f"[live-session-test-v2] fallback save FAILED: "
                      f"{type(error).__name__}: {error}")

        try:
            _safe_draw(render_stopped=True)
        except Exception as error:
            print(f"[live-session-test-v2] final draw() failed (non-fatal): "
                  f"{type(error).__name__}: {error}")

        # Deliberately do NOT call session.Stop() here -- cleanup happens
        # in the render_complete/render_cancel handlers below.

        print(f"[live-session-test-v2] frame={scene_eval.frame_current:<4} "
              f"export={export_time:6.3f}s render={render_time:6.3f}s "
              f"total={time.time() - frame_t0:6.3f}s mode={mode}")

        self.session = None


@persistent
def _on_render_complete(scene):
    print("[live-session-test-v2] render complete -- stopping cached session(s)")
    _stop_all()


@persistent
def _on_render_cancel(scene):
    print("[live-session-test-v2] render cancelled -- stopping cached session(s)")
    _stop_all()


def live_session_gui_test_v2_register():
    if hasattr(bpy.types, "LUXCORE_LIVE_SESSION_TEST"):
        bpy.utils.unregister_class(bpy.types.LUXCORE_LIVE_SESSION_TEST)
    bpy.utils.register_class(LUXCORE_LIVE_SESSION_TEST)
    if _on_render_complete not in bpy.app.handlers.render_complete:
        bpy.app.handlers.render_complete.append(_on_render_complete)
    if _on_render_cancel not in bpy.app.handlers.render_cancel:
        bpy.app.handlers.render_cancel.append(_on_render_cancel)

    _patch_denoiser_panels()

    # Same as v1: scene.luxcore.halt's built-in engine-side halt latches
    # HasDone() permanently True on a session reused across frames --
    # force just the "enable" flag off, use_samples/use_time still
    # respected via _frame_halt_target().
    h = bpy.context.scene.luxcore.halt
    if h.enable:
        print(
            "[live-session-test-v2] disabling scene.luxcore.halt.enable "
            "(was on) -- its use_samples/use_time values are still "
            "respected, only the engine-side HasDone() mechanism is "
            "bypassed (Python-side pass/time counting instead); "
            "re-enable the flag manually if you switch back to the real "
            "LUXCORE engine"
        )
        h.enable = False

    print(
        "\n=== live-session GUI test v2 registered ===\n"
        "Render Properties > Render Engine > "
        "'LuxCore Live Session Test (GUI, camera-only)'\n"
        "Render Properties > Denoiser is now also visible for this "
        "engine -- tick 'enabled' before rendering, click 'Refresh' any "
        "time during Ctrl+F12 to test it against the live/reused "
        "session (see the WARNING in this file's module docstring).\n"
        "Set your frame range/output, then Ctrl+F12 (Render Animation).\n"
        "Watch the System Console for FULL_EXPORT / REUSED_LIVE_SESSION / "
        "FAILED per frame.\n"
    )


def live_session_gui_test_v2_unregister():
    _stop_all()
    _unpatch_denoiser_panels()
    if _on_render_complete in bpy.app.handlers.render_complete:
        bpy.app.handlers.render_complete.remove(_on_render_complete)
    if _on_render_cancel in bpy.app.handlers.render_cancel:
        bpy.app.handlers.render_cancel.remove(_on_render_cancel)
    if hasattr(bpy.types, "LUXCORE_LIVE_SESSION_TEST"):
        bpy.utils.unregister_class(bpy.types.LUXCORE_LIVE_SESSION_TEST)
    print("=== live-session GUI test v2 unregistered ===")


live_session_gui_test_v2_register()
