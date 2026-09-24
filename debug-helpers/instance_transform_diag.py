"""
Standalone diagnostic for the "origin/scale gets corrupted when a GN
instance reappears" bug (see PERSISTENT_DATA_ANIMATION_NOTES.md, "origin/
scale corruption on GN reappearance"). Does NOT touch pyluxcore/LuxCore at
all -- only reads Blender's own depsgraph.object_instances, so it's safe
and fast to run even on the heavy production scene: no render, no BVH
build, no texture upload, just depsgraph evaluation.

What it does: steps through the given frames (forcing a genuine RENDER-mode
depsgraph the same way persistent_data_gpu_test.py does, see that file's
docstring for why bpy.context.evaluated_depsgraph_get() is NOT used), and
for every dupli instance records its key (same key function BlendLuxCore's
exporter uses, utils.make_key_from_instance, reimplemented here so this
script has zero addon dependency), translation and scale. Then reports,
per key that appears in more than one of the listed frames but is ABSENT
in at least one frame in between (i.e. it disappeared and reappeared):
whether its translation/scale on reappearance matches what it was the
last time it was seen. A mismatch is the bug; a match means Blender's own
depsgraph is fine and the corruption happens inside the exporter instead.

Usage:

    blender --background /path/to/scene.blend --python \
        debug-helpers/instance_transform_diag.py -- --frames 2,3,2,3

Or edit FRAMES below and run from Blender's Text Editor.
"""
import sys

import bpy


# ---- CONFIG (edit here if running from Blender's Text Editor) ----
FRAMES = [2, 3, 2, 3]
# Only print per-instance rows for keys whose name contains this (case-
# insensitive), to keep output manageable in a scene with many objects.
# "" prints everything.
NAME_FILTER = ""
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
        elif arg == "--filter" and i + 1 < len(argv):
            NAME_FILTER = argv[i + 1]


def persistent_id_to_str(pid):
    return "_".join(str(x) for x in pid)


def make_key(datablock):
    return str(datablock.original.as_pointer())


def make_key_from_instance(dgi):
    # Mirrors utils.make_key_from_instance() in the addon exactly.
    if dgi.is_instance:
        key = make_key(dgi.object)
        key += "_" + make_key(dgi.parent)
        key += persistent_id_to_str(dgi.persistent_id)
    else:
        key = make_key(dgi.object.original)
    return key


scene = bpy.context.scene
if not scene.camera:
    print("!! Scene has no camera, adding a temporary one so bpy.ops.render.render() works")
    bpy.ops.object.camera_add()
    scene.camera = bpy.context.active_object
scene.render.resolution_x = 4
scene.render.resolution_y = 4

per_frame = {}  # frame -> {key: (obj_name, parent_name, loc, scale)}


class _InstanceDiagEngine(bpy.types.RenderEngine):
    # See module docstring / persistent_data_gpu_test.py for why this
    # exists instead of bpy.context.evaluated_depsgraph_get().
    bl_idname = "INSTANCE_TRANSFORM_DIAG_ENGINE"
    bl_label = "Instance Transform Diagnostic (internal, not for real use)"
    bl_use_preview = False

    def render(self, depsgraph):
        for f in FRAMES:
            scene.frame_set(f)
            depsgraph.update()
            rows = {}
            for dgi in depsgraph.object_instances:
                if not dgi.is_instance:
                    continue
                if not dgi.show_self:
                    continue
                key = make_key_from_instance(dgi)
                loc = tuple(round(x, 5) for x in dgi.matrix_world.translation)
                scale = tuple(round(x, 5) for x in dgi.matrix_world.to_scale())
                parent_name = dgi.parent.name if dgi.parent else None
                rows[key] = (dgi.object.name, parent_name, loc, scale)
            # Frames can repeat in FRAMES (e.g. [2, 3, 2, 3]) to check the
            # same transition twice -- key each snapshot by its position,
            # not just the frame number.
            per_frame[len(per_frame)] = (f, rows)


original_engine = scene.render.engine
bpy.utils.register_class(_InstanceDiagEngine)
scene.render.engine = _InstanceDiagEngine.bl_idname
try:
    bpy.ops.render.render(write_still=False)
finally:
    scene.render.engine = original_engine
    bpy.utils.unregister_class(_InstanceDiagEngine)


print(f"\n=== instance_transform_diag: frames={FRAMES} ===\n")

for step, (f, rows) in per_frame.items():
    shown = {
        k: v for k, v in rows.items()
        if not NAME_FILTER or NAME_FILTER.lower() in v[0].lower()
    }
    print(f"step={step} frame={f} total_instances={len(rows)} shown={len(shown)}")
    for key, (name, parent_name, loc, scale) in shown.items():
        print(f"    key={key} obj={name!r} parent={parent_name!r} loc={loc} scale={scale}")

print("\n=== checking for reappearing keys with a mismatched transform ===")
last_seen = {}  # key -> (step, loc, scale)
mismatches = 0
for step, (f, rows) in per_frame.items():
    for key, (name, parent_name, loc, scale) in rows.items():
        if key in last_seen:
            prev_step, prev_loc, prev_scale = last_seen[key]
            if prev_step != step - 1 and (loc != prev_loc or scale != prev_scale):
                # It was absent for at least one step in between (a real
                # disappear+reappear, not just two consecutive frames),
                # AND its transform changed.
                mismatches += 1
                print(f"  !! MISMATCH key={key} obj={name!r}: "
                      f"step={prev_step} (frame={per_frame[prev_step][0]}) had loc={prev_loc} scale={prev_scale}, "
                      f"step={step} (frame={f}) has loc={loc} scale={scale}")
        last_seen[key] = (step, loc, scale)

if mismatches == 0:
    print("  none found -- Blender's own depsgraph.object_instances is consistent across "
          "the tested disappear/reappear transitions. If the bug is still visible in the "
          "rendered image, it is being introduced inside the exporter (export/caches/"
          "object_cache.py or exported_data.py), not by Blender itself.")
else:
    print(f"  {mismatches} mismatch(es) found -- Blender's own depsgraph.object_instances "
          "already disagrees with itself across the disappear/reappear transition. This "
          "points at the .blend's own Geometry Nodes setup (or a Blender bug), not at the "
          "exporter.")
