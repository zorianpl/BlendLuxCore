"""
Standalone proof-of-concept for "persistent data" animation rendering
(reusing exported geometry/materials across frames instead of a full
re-export per frame). See feature/persistent-data-animation branch.

Usage (run headless, from the machine that has the GPU / the real scene open):

    blender --background /path/to/scene.blend --python debug-helpers/persistent_data_gpu_test.py -- \
        --device OCL --samples 6 --frames 1,2,7,2,7,2,7,1 --res 320 180

Or open the .blend in the GUI, then run this file from Blender's Text Editor
(in that case sys.argv parsing below is skipped, edit the CONFIG block instead).

What it does:
  1. Full export on the first listed frame (Exporter.create_session, like a
     normal final render).
  2. For every following frame: advance bpy frame, force a viewport-style
     diff (Exporter.get_changes(..., force_diff=True)) instead of a full
     re-export, apply it via BeginSceneEdit/EndSceneEdit on the SAME live
     RenderSession -- the session is never stopped/recreated between frames
     anymore (see v8 postmortem below), only Film.Clear()'d.
  3. Logs, per step: elapsed time, luminance (sanity check the image is
     actually different / correctly restored when frames repeat), VRAM used
     (via the addon's own utils.statistics.get_vram_usage(), same call the
     UI stats panel uses) and process RSS (system RAM).

v8 postmortem (why this no longer recreates the session):
  v8 did `session.Stop(); session = pyluxcore.RenderSession(renderconfig);
  session.Start()` after every diff frame. Confirmed by user testing: this
  gave NO speedup at all on the real production scene -- every frame timed
  the same as a plain "Render Animation". The v8 log proves why: even the
  frame 2->3 step, a pure CAMERA move with no geometry/material change,
  still printed a full "Compile Geometry / Compile 157 Textures / Compile
  ImageMaps (~350ms) / Building Optix accelerator / Starting N render
  threads" sequence -- identical cost to the very first full export. That
  sequence is triggered by constructing pyluxcore.RenderSession(renderconfig)
  itself, not by anything BeginSceneEdit()/EndSceneEdit() does (viewport
  render moves the camera through exactly the same BeginSceneEdit/
  EndSceneEdit path continuously, with no such recompile). So: recreating
  the session was the entire cost, and this version stops doing it.

  Why v8 recreated the session in the first place: session.HasDone() -- and
  LuxCore's built-in batch.haltspp halt condition -- was found to latch
  permanently true after the first frame's halt was reached, and
  Film.Clear() alone did not un-stick it (see PERSISTENT_DATA_ANIMATION_
  NOTES.md). This version sidesteps that mystery instead of solving it:
  the scene's built-in halt condition is disabled entirely (SAMPLES is only
  used by this script now, not by scene.luxcore.halt), and each frame's
  "enough samples" check is done in Python against session.GetStats()'s
  cumulative stats.renderengine.pass counter (record the pass count right
  after EndSceneEdit()+Film.Clear(), wait for it to advance by SAMPLES).
  This works regardless of whether that counter resets to 0 on a scene edit
  or keeps climbing forever, and never touches HasDone()/session
  recreation, so it can't reproduce the v8 stuck-session failure mode
  either.

  UNVERIFIED: nobody has run this version yet (no GPU on the dev machine --
  see reminder in chat). If the saved frames come out visually wrong
  (ghosting/blending between frames) or the loop hangs printing "!! timeout
  waiting for pass", that means one of the two assumptions above (Film.Clear
  actually clearing what feeds the saved image; BeginSceneEdit/EndSceneEdit
  being cheap for a camera-only diff) doesn't hold and this needs another
  look together with the log.

This is diagnostic code, not the final feature -- engine/final.py does not
call any of this yet.

Depsgraph mode (2026-09-23): earlier versions of this script called
`bpy.context.evaluated_depsgraph_get()` directly, once per frame. That
depsgraph is evaluated in VIEWPORT mode -- confirmed locally (no GPU
needed for this one) with a throwaway Subsurf-modifier test: `mode ==
"VIEWPORT"` and the evaluated mesh matched the modifier's *viewport*
level even in plain `--background`, even right after manually calling
`bpy.ops.render.render()` once beforehand (that render's own RENDER-mode
depsgraph is a separate, temporary object, not the one
`evaluated_depsgraph_get()` returns afterwards). For a user whose viewport
is set up with low-poly proxies / hidden collections for interactive
speed, that meant this script was silently rendering the *viewport*
version of the scene, not the render version -- geometry only, materials/
world/etc. are unaffected since those aren't viewport/render-dual like
modifiers and hide_viewport/hide_render are.

Fixed by no longer calling `evaluated_depsgraph_get()` at all: the frame
loop now drives an actual (temporary, throwaway) `bpy.types.RenderEngine`
subclass via `bpy.ops.render.render()` per frame, and reads the depsgraph
Blender hands to its `render(self, depsgraph)` callback -- confirmed
locally this is `mode == "RENDER"` and reflects each modifier's *render*
settings, per-frame, exactly like a normal "Render Animation" does. This
also means the real feature (once it lives in engine/final.py, which is
already invoked this same way by Blender) will not have this problem
either -- it was specific to this script's simplified, non-RenderEngine
invocation method, not to the export code itself.
"""
import sys
import time

import bpy


# ---- CONFIG (edit here if running from Blender's Text Editor) ----
DEVICE = "OCL"          # "CPU" or "OCL" (OCL = GPU, OpenCL or CUDA per addon prefs)
SAMPLES = 6              # samples per frame; enforced in Python, see module docstring
FRAMES = [1, 2, 7, 2, 7, 2, 7, 1]
RES_X = 320
RES_Y = 180
OUTPUT_DIR = ""  # e.g. r"C:\temp\persistent_data_test" -- "" disables saving images
DUMP_TRANSFORMS = True   # print every exported object's translation/scale each frame
DUMP_TRANSFORMS_FILTER = ""  # only print objects whose key contains this (case-insensitive); "" = all
# --------------------------------------------------------------------

if "--" in sys.argv:
    argv = sys.argv[sys.argv.index("--") + 1:]
    for i, arg in enumerate(argv):
        if arg == "--device" and i + 1 < len(argv):
            DEVICE = argv[i + 1]
        elif arg == "--samples" and i + 1 < len(argv):
            SAMPLES = int(argv[i + 1])
        elif arg == "--frames" and i + 1 < len(argv):
            frames_arg = argv[i + 1]
            if "-" in frames_arg and "," not in frames_arg:
                start, end = frames_arg.split("-")
                FRAMES = list(range(int(start), int(end) + 1))
            else:
                FRAMES = [int(x) for x in frames_arg.split(",")]
        elif arg == "--res" and i + 2 < len(argv):
            RES_X, RES_Y = int(argv[i + 1]), int(argv[i + 2])
        elif arg == "--no-dump-transforms":
            DUMP_TRANSFORMS = False
        elif arg == "--dump-filter" and i + 1 < len(argv):
            DUMP_TRANSFORMS_FILTER = argv[i + 1]


scene = bpy.context.scene
for _ in range(200):
    try:
        scene.render.engine = "LUXCORE"
        break
    except TypeError:
        time.sleep(0.05)
else:
    raise RuntimeError("LUXCORE render engine never became available - is the addon enabled?")
scene.render.resolution_x = RES_X
scene.render.resolution_y = RES_Y
scene.render.resolution_percentage = 100

h = scene.luxcore.halt
# Built-in halt is intentionally OFF: LuxCore's own batch.haltspp halt
# condition was found to latch HasDone()==True permanently after the first
# frame, surviving Film.Clear() on a reused session (see module docstring).
# Per-frame "enough samples" is instead enforced in Python below, against
# session.GetStats()'s stats.renderengine.pass counter, so this never
# touches HasDone()/haltspp at all.
h.enable = False

scene.luxcore.config.device = DEVICE

import pyluxcore
from bl_ext.user_default.blendluxcore import export
from bl_ext.user_default.blendluxcore.export import Change
from bl_ext.user_default.blendluxcore.utils import view_layer as utils_view_layer
from bl_ext.user_default.blendluxcore.utils.statistics import get_vram_usage

view_layer = bpy.context.view_layer
utils_view_layer.State.active_view_layer = view_layer.name


def rss_mb():
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass  # Not on Linux

    try:
        import psutil
        return psutil.Process().memory_info().rss / (1024 * 1024)
    except ImportError:
        pass

    try:
        import ctypes
        import ctypes.wintypes as wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return counters.WorkingSetSize / (1024 * 1024)
    except Exception:
        pass

    return -1


def dump_scene_transforms(exporter):
    """Print every currently-exported object's translation/scale, straight
    from the same ExportedObject.transform (a plain Blender Matrix) that
    ExportedObject.get_props() sends to LuxCore as the object's
    scene.objects.<key>.transformation SDL property -- so this is exactly
    what LuxCore has, not a re-derivation of it. obj_transform=None means
    the object's transform is baked into its mesh's vertices instead (see
    _convert_mesh_obj/mesh_converter.convert), not a separate property --
    printed as "(baked into mesh)".
    """
    cache = exporter.object_cache2
    keys = sorted(cache.exported_objects.keys())
    if DUMP_TRANSFORMS_FILTER:
        keys = [k for k in keys if DUMP_TRANSFORMS_FILTER.lower() in k.lower()]
    print(f"  [diag] scene transform dump ({len(keys)} objects"
          f"{' matching filter' if DUMP_TRANSFORMS_FILTER else ''}):")
    for key in keys:
        exported_obj = cache.exported_objects[key]
        n_parts = len(exported_obj.parts)
        if exported_obj.transform is None:
            print(f"    key={key} parts={n_parts} transform=(baked into mesh)")
        else:
            t = exported_obj.transform
            loc = tuple(round(x, 5) for x in t.translation)
            scale = tuple(round(x, 5) for x in t.to_scale())
            print(f"    key={key} parts={n_parts} loc={loc} scale={scale}")


def current_pass(session):
    session.UpdateStats()
    return session.GetStats().Get("stats.renderengine.pass").GetInt()


def run_until_pass(session, target_pass, max_seconds=60):
    """Poll session.GetStats() until stats.renderengine.pass reaches
    target_pass. Deliberately does not touch session.HasDone() / the
    built-in halt condition (disabled entirely, see CONFIG) -- this counter
    is cumulative-or-reset, we don't need to know or care which, we just
    wait for it to advance by SAMPLES from wherever it started this frame.
    """
    start = time.time()
    pass_now = current_pass(session)
    while pass_now < target_pass:
        if time.time() - start > max_seconds:
            print(f"  !! timeout waiting for pass {target_pass} (stuck at {pass_now})")
            break
        time.sleep(0.02)
        pass_now = current_pass(session)
    return pass_now


print(f"\n=== persistent_data_gpu_test: device={DEVICE} samples={SAMPLES} "
      f"res={RES_X}x{RES_Y} frames={FRAMES} ===\n")

exporter = None
session = None
renderconfig = None
results = []


def run_frame(engine, depsgraph):
    # depsgraph is whatever Blender handed to our RenderEngine.render()
    # callback for the current frame -- confirmed locally to be mode ==
    # "RENDER" (see module docstring), unlike bpy.context.evaluated_
    # depsgraph_get() which this used to call directly.
    global exporter, session, renderconfig

    idx = len(results)
    f = depsgraph.scene_eval.frame_current

    t0 = time.time()
    if session is None:
        exporter = export.Exporter()
        # See Exporter.persistent_data_animation docstring (export/__init__.py):
        # forces use_instancing=True for every object so nothing ever takes
        # the baked-transform-into-mesh path, which was confirmed corrupted
        # by a live BeginSceneEdit/EndSceneEdit session (2026-09-23).
        exporter.persistent_data_animation = True
        session = exporter.create_session(depsgraph, context=None, engine=engine, view_layer=view_layer)
        renderconfig = session.GetRenderConfig()
        session.Start()
        changes_str = "FULL_EXPORT"
    else:
        changes = exporter.get_changes(depsgraph, context=None, force_diff=True)
        changes_str = Change.to_string(changes)
        # Same live session, edited in place -- never stopped/recreated.
        # See module docstring (v8 postmortem) for why this replaces the
        # previous Stop()+RenderSession(renderconfig)+Start() dance.
        session = exporter.update(depsgraph, None, session, changes)
        session.GetFilm().Clear()
        # Diagnostic: is the image actually empty right after Clear(), or does
        # it already show leftover radiance from the previous frame? If this
        # is close to the previous frame's final lumY, Clear() (or whatever
        # SaveOutput/GetFilmY reads) isn't resetting what we think it resets.
        print(f"  [diag] lumY immediately after Film.Clear(): {session.GetFilm().GetFilmY():.5f}")
    export_time = time.time() - t0

    if DUMP_TRANSFORMS:
        dump_scene_transforms(exporter)

    t1 = time.time()
    pass_before = current_pass(session)
    target = pass_before + SAMPLES
    passes = run_until_pass(session, target)
    render_time = time.time() - t1

    if OUTPUT_DIR:
        import os
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        out_path = os.path.join(OUTPUT_DIR, f"step{idx:03d}_frame{f:04d}.png")
        session.GetFilm().SaveOutput(
            out_path, pyluxcore.FilmOutputType.RGB_IMAGEPIPELINE, pyluxcore.Properties()
        )
        print(f"  saved: {out_path}")

    lum = session.GetFilm().GetFilmY()
    stats = session.GetStats()
    vram_used, vram_max = get_vram_usage(stats)
    mem = rss_mb()
    n_exported = len(exporter.object_cache2.exported_objects)

    print(f"step={idx:<3} frame={f:<4} edit={export_time:6.3f}s render={render_time:6.3f}s "
          f"pass={pass_before}->{passes:<4} lumY={lum:.5f} objs={n_exported:<5} "
          f"RAM={mem:8.1f}MB VRAM={vram_used}/{vram_max}MB  changes={changes_str}")
    results.append({
        "idx": idx, "frame": f, "edit_time": export_time, "render_time": render_time,
        "lum": lum, "vram": vram_used, "ram": mem, "changes": changes_str,
    })


class _PersistentDataTestEngine(bpy.types.RenderEngine):
    # Throwaway engine, registered only for the lifetime of this script, so
    # bpy.ops.render.render() hands run_frame() a genuine RENDER-mode
    # depsgraph (see module docstring) instead of us fetching one via
    # bpy.context.evaluated_depsgraph_get(), which is VIEWPORT-mode. Does
    # not touch pyluxcore itself -- run_frame() does all of that with the
    # real Exporter/RenderSession, exactly like the standalone-script
    # version did.
    #
    # Drives the WHOLE frame sequence from inside this ONE render() call
    # (scene.frame_set() + depsgraph.update() per frame, confirmed locally
    # to keep depsgraph.mode == "RENDER" and correctly re-evaluate each
    # frame) instead of calling bpy.ops.render.render() once per frame.
    # The per-frame-operator-call version crashed on a later frame with
    # `session.BeginSceneEdit(): RuntimeError: invalid argument` -- most
    # likely because it kept one pyluxcore RenderSession alive *across*
    # several separate render operator invocations, which is not how a
    # RenderEngine is normally used (engine/final.py's real render() also
    # creates/tears down its session within a single continuous call).
    # Doing everything inside one call sidesteps that risk entirely.
    bl_idname = "PERSISTENT_DATA_TEST_ENGINE"
    bl_label = "Persistent Data Test (internal, not for real use)"
    bl_use_preview = False

    def render(self, depsgraph):
        for f in FRAMES:
            scene.frame_set(f)
            depsgraph.update()
            run_frame(self, depsgraph)


original_engine = scene.render.engine
bpy.utils.register_class(_PersistentDataTestEngine)
scene.render.engine = _PersistentDataTestEngine.bl_idname

try:
    bpy.ops.render.render(write_still=False)
finally:
    scene.render.engine = original_engine
    bpy.utils.unregister_class(_PersistentDataTestEngine)
    if session is not None:
        session.Stop()

print("\n=== SUMMARY ===")
first = results[0]
last = results[-1]
diff_frames = [r for r in results[1:] if r["changes"] != "FULL_EXPORT"]
print(f"edit time:   first={first['edit_time']:.3f}s  last={last['edit_time']:.3f}s")
if diff_frames:
    avg_diff_edit = sum(r["edit_time"] for r in diff_frames) / len(diff_frames)
    print(f"edit time on diff frames (not FULL_EXPORT): avg={avg_diff_edit:.3f}s "
          f"min={min(r['edit_time'] for r in diff_frames):.3f}s "
          f"max={max(r['edit_time'] for r in diff_frames):.3f}s")
    print("  -> if this is still close to the first frame's full-export time, the "
          "session-reuse fix did NOT help and BeginSceneEdit/EndSceneEdit itself "
          "is the expensive part, not session recreation.")
print(f"RAM:   first={first['ram']:.1f}MB  last={last['ram']:.1f}MB  delta={last['ram']-first['ram']:.1f}MB")
print(f"VRAM:  first={first['vram']}MB  last={last['vram']}MB  delta={last['vram']-first['vram']}MB")
