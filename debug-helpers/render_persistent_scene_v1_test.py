"""
Standalone test for "persistent scene reuse" v1
(export/caches/persistent_scene_v1.py, new addon package
BlendLuxCore_PersistentSceneReuse_v1).

Different mechanism than debug-helpers/persistent_data_gpu_test.py: that
script keeps ONE live pyluxcore.RenderSession alive across frames via
BeginSceneEdit/EndSceneEdit inside a single continuous render() call --
proven unsafe across SEPARATE render() invocations (see its "v8
postmortem"). This test does the opposite: it reuses only the exported
Scene + Exporter caches (object_cache2, material_cache, ...); a brand
new RenderConfig + RenderSession is built every single frame, exactly
like an ordinary render. That means each render() call looks completely
normal to Blender -- so this test drives Blender's REAL native
"Render Animation" operator (bpy.ops.render.render(animation=True)),
which calls render() once per frame itself, with a proper RENDER-mode
depsgraph each time (unlike bpy.context.evaluated_depsgraph_get(), see
persistent_data_gpu_test.py's docstring for why that matters).

What this test verifies:
  1. Frames after the first are faster (export_time printed per frame --
     compare frame 1 vs later frames; the win comes from skipping
     first_run() when possible, not from the render/sampling itself).
  2. Blender's own compositor + denoise + save-to-disk + advance-to-
     next-frame keeps working completely automatically, because we never
     touch that machinery -- we only call begin_result()/end_result()
     the normal way, once per (real, separate) render() call, via
     FrameBufferFinal, exactly like the production engine.

KNOWN LIMITATIONS (see persistent_scene_v1.py docstring for details):
  - No camera/world/motion-blur *signature* checking before reuse -- if
    you toggle DoF, remove the env light, or change motion-blur settings
    mid-animation, the reused scene can end up with stale properties
    (Parse() cannot delete properties). Fine for a static-config
    animation test.
  - Does NOT fix the separate, already-diagnosed instance-redefine-sweep
    slowdown for large visibility-toggling instance groups (see
    PERSISTENT_DATA_ANIMATION_NOTES.md) -- frames where such a group
    toggles will still be slow, same as today.

Usage: open the real scene in Blender's GUI (not --background, so you
can watch the render window and see file output happen normally). Set
your frame range (scene.frame_start/frame_end) and output path as usual
in Output Properties. Make sure the PersistentSceneReuse_v1 addon is
enabled. Then run this file from the Scripting tab.
"""
import sys
import time

import bpy


# ---- CONFIG (edit here if you want to override the scene's own range/output) ----
FRAME_START = None  # None = use scene.frame_start
FRAME_END = None    # None = use scene.frame_end
OUTPUT_DIR = ""      # "" = use scene.render.filepath as already set
# -----------------------------------------------------------------------------

scene = bpy.context.scene
if FRAME_START is not None:
    scene.frame_start = FRAME_START
if FRAME_END is not None:
    scene.frame_end = FRAME_END
if OUTPUT_DIR:
    scene.render.filepath = OUTPUT_DIR


def _find_module(suffix, required_attr):
    for name, mod in list(sys.modules.items()):
        if name.endswith(suffix) and hasattr(mod, required_attr):
            return mod
    return None


export_mod = _find_module("export", "Exporter")
if export_mod is None:
    raise RuntimeError(
        "Could not find the loaded BlendLuxCore-family export module -- "
        "make sure the addon is enabled and has run at least once this "
        "session (e.g. briefly switch the viewport to LuxCore shading) so "
        "its modules are imported."
    )

persistent_scene_v1 = _find_module("caches.persistent_scene_v1", "get_session")
if persistent_scene_v1 is None:
    raise RuntimeError(
        "Could not find export.caches.persistent_scene_v1 -- is the "
        "PersistentSceneReuse_v1 addon build installed (not a different "
        "BlendLuxCore build)?"
    )

draw_final_mod = _find_module("draw.final", "FrameBufferFinal")
if draw_final_mod is None:
    raise RuntimeError("Could not find draw.final.FrameBufferFinal.")

utils_render = _find_module("utils.render", "update_stats")
if utils_render is None:
    raise RuntimeError("Could not find utils.render.")

Exporter = export_mod.Exporter
FrameBufferFinal = draw_final_mod.FrameBufferFinal


class RENDER_PERSISTENT_SCENE_V1_TEST(bpy.types.RenderEngine):
    """
    Throwaway engine for this test only -- a simplified, parallel copy of
    engine/final.py's render loop with just the session-creation call
    swapped for persistent_scene_v1.get_session(). Does not register
    with or modify the real LUXCORE engine in any way.
    """
    bl_idname = "RENDER_PERSISTENT_SCENE_V1_TEST"
    bl_label = "Persistent Scene Reuse v1 Test (internal, not for real use)"
    bl_use_preview = False
    bl_use_postprocess = True
    bl_use_save_buffers = False

    def render(self, depsgraph):
        frame_t0 = time.time()
        scene_eval = depsgraph.scene_eval
        view_layer_eval = depsgraph.view_layer_eval
        layer = scene_eval.original.view_layers.get(view_layer_eval.name)

        # Same "check the layer is enabled" dance as engine/final.py
        dummy_result = self.begin_result(0, 0, 1, 1, layer=layer.name)
        self.end_result(dummy_result, cancel=True, do_merge_results=False)

        statistics = scene_eval.luxcore.statistics.get_active()

        export_t0 = time.time()
        exporter, session = persistent_scene_v1.get_session(
            Exporter, depsgraph, None, self, view_layer_eval, statistics,
        )
        export_time = time.time() - export_t0

        if session is None:
            print(f"[v1-test] frame={scene_eval.frame_current}: "
                  f"export cancelled by user")
            return

        framebuffer = FrameBufferFinal(scene_eval)
        session.Start()
        session_config = session.GetRenderConfig()

        render_t0 = time.time()
        while True:
            if self.test_break():
                break
            stats = utils_render.update_stats(session)
            if session.HasDone():
                break
            time.sleep(1 / 10)

        stats = utils_render.update_stats(session)
        framebuffer.draw(self, session, depsgraph.scene, render_stopped=True)
        session.Stop()
        render_time = time.time() - render_t0

        n_objects = len(exporter.object_cache2.exported_objects)
        print(
            f"[v1-test] frame={scene_eval.frame_current:<4} "
            f"export={export_time:6.3f}s render={render_time:6.3f}s "
            f"total={time.time() - frame_t0:6.3f}s objects={n_objects}"
        )


def _register_and_run():
    if hasattr(bpy.types, "RENDER_PERSISTENT_SCENE_V1_TEST"):
        bpy.utils.unregister_class(bpy.types.RENDER_PERSISTENT_SCENE_V1_TEST)
    bpy.utils.register_class(RENDER_PERSISTENT_SCENE_V1_TEST)

    original_engine = scene.render.engine
    scene.render.engine = "RENDER_PERSISTENT_SCENE_V1_TEST"
    try:
        print(
            f"\n=== persistent_scene_v1 test: frames "
            f"{scene.frame_start}-{scene.frame_end} -> "
            f"{scene.render.filepath!r} ===\n"
        )
        bpy.ops.render.render(animation=True)
    finally:
        scene.render.engine = original_engine
        bpy.utils.unregister_class(RENDER_PERSISTENT_SCENE_V1_TEST)

    print("\n=== done -- compare export= timings above (frame 1 = FULL, "
          "should say so in the [persistent_scene_v1] log lines mixed in; "
          "later frames should say REUSE and be much faster), and check "
          "that the output files at scene.render.filepath actually exist "
          "and went through your compositor/denoise setup normally ===\n")


_register_and_run()
