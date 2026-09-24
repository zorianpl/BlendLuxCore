"""
Diagnostic companion to persistent_data_gpu_test.py.

Does NOT touch the exporter/persistent-data mechanism at all -- just dumps,
for every frame in FRAMES, exactly what depsgraph.object_instances contains
for objects matching NAME_FILTER. Use this to check whether "missing"
instances are actually a Blender/Geometry-Nodes evaluation issue (they never
show up here) or an export-side issue (they show up here correctly, but the
render is still wrong).

Usage: same as persistent_data_gpu_test.py (edit CONFIG below, or run with
--python from the command line; --frames/--name-filter are also accepted as
CLI args after "--").
"""
import sys
import time

import bpy


# ---- CONFIG (edit here if running from Blender's Text Editor) ----
FRAMES = [1, 2, 3, 10, 11]
NAME_FILTER = ""  # e.g. "Tree" -- "" means "show everything"
# --------------------------------------------------------------------

if "--" in sys.argv:
    argv = sys.argv[sys.argv.index("--") + 1:]
    for i, arg in enumerate(argv):
        if arg == "--frames" and i + 1 < len(argv):
            frames_arg = argv[i + 1]
            if "-" in frames_arg and "," not in frames_arg:
                start, end = frames_arg.split("-")
                FRAMES = list(range(int(start), int(end) + 1))
            else:
                FRAMES = [int(x) for x in frames_arg.split(",")]
        elif arg == "--name-filter" and i + 1 < len(argv):
            NAME_FILTER = argv[i + 1]


scene = bpy.context.scene
for _ in range(200):
    if hasattr(scene, "luxcore"):
        break
    time.sleep(0.05)

from bl_ext.user_default.BlendLuxCore import utils as blc_utils

print(f"\n=== persistent_data_gpu_test_2: frames={FRAMES} name_filter={NAME_FILTER!r} ===\n")

for f in FRAMES:
    scene.frame_set(f)
    depsgraph = bpy.context.evaluated_depsgraph_get()

    print(f"--- FRAME {f} ---")
    count = 0
    for dg_obj_instance in depsgraph.object_instances:
        obj = dg_obj_instance.object
        parent = dg_obj_instance.parent

        if NAME_FILTER and NAME_FILTER not in obj.name and (
            not parent or NAME_FILTER not in parent.name
        ):
            continue

        count += 1
        try:
            key = blc_utils.make_key_from_instance(dg_obj_instance)
        except Exception as e:
            key = f"<error: {e}>"

        pos = dg_obj_instance.matrix_world.translation
        try:
            is_obj_visible = blc_utils.is_obj_visible(obj)
        except Exception as e:
            is_obj_visible = f"<error: {e}>"

        print(
            f"  obj={obj.name!r:<30} parent={(parent.name if parent else None)!r:<25} "
            f"is_instance={dg_obj_instance.is_instance} show_self={dg_obj_instance.show_self} "
            f"persistent_id={list(dg_obj_instance.persistent_id)} "
            f"pos=({pos.x:.3f}, {pos.y:.3f}, {pos.z:.3f}) "
            f"exclude_from_render={obj.luxcore.exclude_from_render} "
            f"is_obj_visible={is_obj_visible} "
            f"key={key}"
        )

    print(f"  TOTAL matching instances: {count}\n")

print("=== done ===")
