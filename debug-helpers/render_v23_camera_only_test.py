"""
v23 test: persistent-data animation rendering that deliberately does NOT
look at depsgraph.updates / Change detection / ObjectCache2.update() at
all. Every object/material/light/world/instance is exported ONCE on the
first frame (first_run()) and never touched again -- the ONLY thing
re-parsed on the live session every frame is the camera
(Exporter.update_camera_only(), export/__init__.py).

Why: ObjectCache2.update()'s per-instance diffing has no way to
recognize an instance that already belongs to an existing, unchanged
hybrid-batched group (only the group's single "base" object is tracked
in exported_objects) -- every OTHER member instance looks brand new to
it, every time it runs, regardless of whether anything moved. And it
runs whenever ANY object changes ANYWHERE in the scene (Change.OBJECT is
global, not per-group). Confirmed by testing: on the real production
scene this makes update() SLOWER than a full first_run(), not faster.
This test sidesteps the whole problem: after frame 1, nothing except the
camera is ever re-exported, so that code path is never even reached.

TRADE-OFF, accepted deliberately for this test: any object/material/
light/visibility animation OTHER than the camera will NOT be reflected
after frame 1 -- the scene is frozen exactly as it was first_run() on
the first listed frame. Only use this for a sequence where the camera is
genuinely the only thing that needs to move.

Same session-lifecycle rules as debug-helpers/persistent_data_gpu_test.py
(read that script's module docstring for the full "v8 postmortem" and
depsgraph-mode background): ONE live RenderSession, never stopped/
recreated between frames, driven by a throwaway RenderEngine subclass so
Blender hands us a genuine RENDER-mode depsgraph each frame, built-in
halt condition disabled (samples enforced in Python against
session.GetStats() instead).

Usage:

    blender --background /path/to/scene.blend --python render_v23_camera_only_test.py -- \\
        --device OCL --samples 6 --frames 1-150 --res 320 180

Or open the .blend in the GUI and run this file from the Text Editor /
Scripting tab (edit the CONFIG block below instead of using --args).
"""
import sys
import time

import bpy


# ---- CONFIG (edit here if running from Blender's Text Editor) ----
DEVICE = "OCL"          # "CPU" or "OCL" (OCL = GPU, OpenCL or CUDA per addon prefs)
SAMPLES = 6              # samples per frame; enforced in Python, see module docstring
FRAMES = [1, 2, 3, 4, 5]
RES_X = 320
RES_Y = 180
OUTPUT_DIR = r"C:\temp\v23_camera_only_test"
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
# Same reason as persistent_data_gpu_test.py: built-in halt latches
# HasDone()==True permanently after the first frame on a reused session.
h.enable = False

scene.luxcore.config.device = DEVICE

import pyluxcore


def _find_module(suffix, required_attr):
    for name, mod in list(sys.modules.items()):
        if name.endswith(suffix) and hasattr(mod, required_attr):
            return mod
    return None


export_mod = _find_module("export", "Exporter")
if export_mod is None or not hasattr(export_mod, "Exporter") or not hasattr(
    export_mod.Exporter, "update_camera_only"
):
    raise RuntimeError(
        "Could not find export.Exporter.update_camera_only() -- make sure "
        "the v23 addon build is installed and enabled (not an older "
        "BlendLuxCore build without this method), and that it has run at "
        "least once this session (e.g. briefly switch viewport shading to "
        "LuxCore) so its modules are imported."
    )
utils_view_layer_mod = _find_module("utils.view_layer", "State")
statistics_mod = _find_module("utils.statistics", "get_vram_usage")

Exporter = export_mod.Exporter
utils_view_layer = utils_view_layer_mod
get_vram_usage = statistics_mod.get_vram_usage

view_layer = bpy.context.view_layer
utils_view_layer.State.active_view_layer = view_layer.name


def rss_mb():
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
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


def current_pass(session):
    session.UpdateStats()
    return session.GetStats().Get("stats.renderengine.pass").GetInt()


def run_until_pass(session, target_pass, max_seconds=60):
    start = time.time()
    pass_now = current_pass(session)
    while pass_now < target_pass:
        if time.time() - start > max_seconds:
            print(f"  !! timeout waiting for pass {target_pass} (stuck at {pass_now})")
            break
        time.sleep(0.02)
        pass_now = current_pass(session)
    return pass_now


print(f"\n=== v23 camera-only test: device={DEVICE} samples={SAMPLES} "
      f"res={RES_X}x{RES_Y} frames={FRAMES} ===\n")

exporter = None
session = None
results = []


def run_frame(engine, depsgraph):
    global exporter, session

    idx = len(results)
    f = depsgraph.scene_eval.frame_current

    t0 = time.time()
    if session is None:
        exporter = export_mod.Exporter()
        exporter.persistent_data_animation = True
        session = exporter.create_session(depsgraph, context=None, engine=engine, view_layer=view_layer)
        session.Start()
        mode_str = "FULL_EXPORT"
    else:
        # THE point of this test: no Change detection, no depsgraph
        # inspection, no ObjectCache2.update() -- only the camera.
        session = exporter.update_camera_only(depsgraph, session)
        session.GetFilm().Clear()
        mode_str = "CAMERA_ONLY"
    export_time = time.time() - t0

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
          f"RAM={mem:8.1f}MB VRAM={vram_used}/{vram_max}MB  mode={mode_str}")
    results.append({
        "idx": idx, "frame": f, "edit_time": export_time, "render_time": render_time,
        "lum": lum, "vram": vram_used, "ram": mem, "mode": mode_str,
    })


class _V23CameraOnlyTestEngine(bpy.types.RenderEngine):
    bl_idname = "V23_CAMERA_ONLY_TEST_ENGINE"
    bl_label = "V23 Camera-Only Test (internal, not for real use)"
    bl_use_preview = False

    def render(self, depsgraph):
        for f in FRAMES:
            scene.frame_set(f)
            depsgraph.update()
            run_frame(self, depsgraph)


original_engine = scene.render.engine
bpy.utils.register_class(_V23CameraOnlyTestEngine)
scene.render.engine = _V23CameraOnlyTestEngine.bl_idname

try:
    bpy.ops.render.render(write_still=False)
finally:
    scene.render.engine = original_engine
    bpy.utils.unregister_class(_V23CameraOnlyTestEngine)
    if session is not None:
        session.Stop()

print("\n=== SUMMARY ===")
first = results[0]
last = results[-1]
cam_only_frames = [r for r in results[1:] if r["mode"] == "CAMERA_ONLY"]
print(f"edit time:   first (FULL_EXPORT)={first['edit_time']:.3f}s  last (CAMERA_ONLY)={last['edit_time']:.3f}s")
if cam_only_frames:
    avg = sum(r["edit_time"] for r in cam_only_frames) / len(cam_only_frames)
    print(f"edit time on CAMERA_ONLY frames: avg={avg:.3f}s "
          f"min={min(r['edit_time'] for r in cam_only_frames):.3f}s "
          f"max={max(r['edit_time'] for r in cam_only_frames):.3f}s")
print(f"RAM:   first={first['ram']:.1f}MB  last={last['ram']:.1f}MB  delta={last['ram']-first['ram']:.1f}MB")
print(f"VRAM:  first={first['vram']}MB  last={last['vram']}MB  delta={last['vram']-first['vram']}MB")
print("\nCheck the saved frames in", OUTPUT_DIR, "-- since nothing but the")
print("camera updates, any object/visibility animation in the scene will")
print("NOT show up after frame 1. That is expected for this test.")
