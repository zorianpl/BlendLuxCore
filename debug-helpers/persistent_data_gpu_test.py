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
     re-export, apply it via BeginSceneEdit/EndSceneEdit, then start a FRESH
     RenderSession from the SAME (unchanged) RenderConfig -- this avoids the
     known memory leak in _update_config()/force_session_restart() (see
     GitHub issue #577), because we never re-Parse the RenderConfig itself,
     only the geometry inside it.
  3. Logs, per step: elapsed time, luminance (sanity check the image is
     actually different / correctly restored when frames repeat), VRAM used
     (via the addon's own utils.statistics.get_vram_usage(), same call the
     UI stats panel uses) and process RSS (system RAM).

This is diagnostic code, not the final feature -- engine/final.py does not
call any of this yet.
"""
import sys
import time

import bpy


# ---- CONFIG (edit here if running from Blender's Text Editor) ----
DEVICE = "OCL"          # "CPU" or "OCL" (OCL = GPU, OpenCL or CUDA per addon prefs)
SAMPLES = 6
FRAMES = [1, 2, 7, 2, 7, 2, 7, 1]
RES_X = 320
RES_Y = 180
# --------------------------------------------------------------------

if "--" in sys.argv:
    argv = sys.argv[sys.argv.index("--") + 1:]
    for i, arg in enumerate(argv):
        if arg == "--device" and i + 1 < len(argv):
            DEVICE = argv[i + 1]
        elif arg == "--samples" and i + 1 < len(argv):
            SAMPLES = int(argv[i + 1])
        elif arg == "--frames" and i + 1 < len(argv):
            FRAMES = [int(x) for x in argv[i + 1].split(",")]
        elif arg == "--res" and i + 2 < len(argv):
            RES_X, RES_Y = int(argv[i + 1]), int(argv[i + 2])


scene = bpy.context.scene
for _ in range(200):
    if hasattr(scene, "luxcore"):
        break
    time.sleep(0.05)

scene.render.engine = "LUXCORE"
scene.render.resolution_x = RES_X
scene.render.resolution_y = RES_Y
scene.render.resolution_percentage = 100

h = scene.luxcore.halt
h.enable = True
h.use_time = False
h.use_samples = True
h.samples = SAMPLES

scene.luxcore.config.device = DEVICE

import pyluxcore
from bl_ext.user_default.BlendLuxCore import export
from bl_ext.user_default.BlendLuxCore.export import Change
from bl_ext.user_default.BlendLuxCore.utils import view_layer as utils_view_layer
from bl_ext.user_default.BlendLuxCore.utils.statistics import get_vram_usage

view_layer = bpy.context.view_layer
utils_view_layer.State.active_view_layer = view_layer.name


def rss_mb():
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    return -1


def run_to_halt(session, max_seconds=30):
    start = time.time()
    while not session.HasDone():
        session.UpdateStats()
        if time.time() - start > max_seconds:
            print("  !! timeout waiting for halt condition")
            break
        time.sleep(0.02)
    session.UpdateStats()


print(f"\n=== persistent_data_gpu_test: device={DEVICE} samples={SAMPLES} "
      f"res={RES_X}x{RES_Y} frames={FRAMES} ===\n")

exporter = None
session = None
renderconfig = None
results = []

for idx, f in enumerate(FRAMES):
    scene.frame_set(f)
    depsgraph = bpy.context.evaluated_depsgraph_get()

    t0 = time.time()
    if session is None:
        exporter = export.Exporter()
        session = exporter.create_session(depsgraph, context=None, engine=None, view_layer=view_layer)
        renderconfig = session.GetRenderConfig()
        session.Start()
        changes_str = "FULL_EXPORT"
    else:
        changes = exporter.get_changes(depsgraph, context=None, force_diff=True)
        changes_str = Change.to_string(changes)
        session = exporter.update(depsgraph, None, session, changes)
        session.Stop()
        session = pyluxcore.RenderSession(renderconfig)
        session.Start()
    export_time = time.time() - t0

    run_to_halt(session)

    lum = session.GetFilm().GetFilmY()
    stats = session.GetStats()
    passes = stats.Get("stats.renderengine.pass").GetInt()
    vram_used, vram_max = get_vram_usage(stats)
    mem = rss_mb()
    n_exported = len(exporter.object_cache2.exported_objects)

    print(f"step={idx:<3} frame={f:<4} t={export_time:6.3f}s pass={passes:<4} "
          f"lumY={lum:.5f} objs={n_exported:<5} RAM={mem:8.1f}MB "
          f"VRAM={vram_used}/{vram_max}MB  changes={changes_str}")
    results.append((idx, f, export_time, lum, vram_used, mem))

session.Stop()

print("\n=== SUMMARY ===")
first = results[0]
last = results[-1]
print(f"time:  first={first[2]:.3f}s  last={last[2]:.3f}s")
print(f"RAM:   first={first[5]:.1f}MB  last={last[5]:.1f}MB  delta={last[5]-first[5]:.1f}MB")
print(f"VRAM:  first={first[4]}MB  last={last[4]}MB  delta={last[4]-first[4]}MB")
