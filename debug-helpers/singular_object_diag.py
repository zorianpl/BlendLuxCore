"""
Standalone diagnostic, companion to instance_transform_diag.py, for the
origin/scale-doubling bug (see PERSISTENT_DATA_ANIMATION_NOTES.md, "ROOT
CAUSE FOUND AND FIXED" section) -- but for the objects actually affected:
plain, singular (non-GN-instanced) meshes with un-applied location/scale,
like the user's minimal repro ('Plane', 'Cube', 'Cube.001').

Unlike instance_transform_diag.py, this does NOT look at
depsgraph.object_instances (these objects are_instance=False, they don't
show up there in an interesting way) -- it looks at two things instead,
per listed frame, for every object whose name is in OBJECT_NAMES:

  1. Its own matrix_world (translation/scale) straight from
     depsgraph.objects -- this should be perfectly constant across all
     frames for a static object; if THIS changes, something is moving the
     object itself (unlikely, but worth ruling out).
  2. Whether it shows up in depsgraph.updates this frame with
     is_updated_geometry=True -- this is the exact trigger condition for
     ObjectCache2.update()'s "Geometry updates" loop in
     export/caches/object_cache.py, which is where the root-cause fix was
     made. If it's still True on some frame after the fix and the bug
     still reproduces, the fix didn't address the actual trigger and the
     investigation needs to go back to that loop; if it's never True, the
     bug is coming from somewhere else entirely (not this loop), and the
     new `[diag] first bake` / `[diag] !!! RE-BAKE` prints added to
     export/mesh_converter.py (need pyluxcore, use
     persistent_data_gpu_test.py to get those) are the next thing to
     check.

Does NOT touch pyluxcore/LuxCore -- safe and fast even on the heavy
production scene.

Usage:

    blender --background /path/to/scene.blend --python \
        debug-helpers/singular_object_diag.py -- --frames 4,5,6 --names Plane,Cube,Cube.001

Or edit FRAMES / OBJECT_NAMES below and run from Blender's Text Editor.
"""
import sys

import bpy


# ---- CONFIG (edit here if running from Blender's Text Editor) ----
FRAMES = [4, 5, 6]
OBJECT_NAMES = ["Plane", "Cube", "Cube.001"]
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
        elif arg == "--names" and i + 1 < len(argv):
            OBJECT_NAMES = argv[i + 1].split(",")


scene = bpy.context.scene
if not scene.camera:
    print("!! Scene has no camera, adding a temporary one so bpy.ops.render.render() works")
    bpy.ops.object.camera_add()
    scene.camera = bpy.context.active_object
scene.render.resolution_x = 4
scene.render.resolution_y = 4

per_frame = {}  # step -> (frame, {name: (loc, scale, is_updated_geometry)})


class _SingularObjectDiagEngine(bpy.types.RenderEngine):
    bl_idname = "SINGULAR_OBJECT_DIAG_ENGINE"
    bl_label = "Singular Object Diagnostic (internal, not for real use)"
    bl_use_preview = False

    def render(self, depsgraph):
        for f in FRAMES:
            scene.frame_set(f)
            depsgraph.update()

            updated_geometry_names = set()
            if depsgraph.id_type_updated("OBJECT"):
                for dg_update in depsgraph.updates:
                    if dg_update.is_updated_geometry and isinstance(dg_update.id, bpy.types.Object):
                        if dg_update.id.name in OBJECT_NAMES:
                            updated_geometry_names.add(dg_update.id.name)

            rows = {}
            for name in OBJECT_NAMES:
                obj = depsgraph.objects.get(name) or bpy.data.objects.get(name)
                if obj is None:
                    rows[name] = None
                    continue
                eval_obj = obj.evaluated_get(depsgraph)
                loc = tuple(round(x, 5) for x in eval_obj.matrix_world.translation)
                scale = tuple(round(x, 5) for x in eval_obj.matrix_world.to_scale())
                rows[name] = (loc, scale, name in updated_geometry_names)

            per_frame[len(per_frame)] = (f, rows)


original_engine = scene.render.engine
bpy.utils.register_class(_SingularObjectDiagEngine)
scene.render.engine = _SingularObjectDiagEngine.bl_idname
try:
    bpy.ops.render.render(write_still=False)
finally:
    scene.render.engine = original_engine
    bpy.utils.unregister_class(_SingularObjectDiagEngine)


print(f"\n=== singular_object_diag: frames={FRAMES} names={OBJECT_NAMES} ===\n")

for step, (f, rows) in per_frame.items():
    print(f"step={step} frame={f}")
    for name, data in rows.items():
        if data is None:
            print(f"    {name!r}: NOT FOUND in this scene")
            continue
        loc, scale, is_updated_geometry = data
        flag = "  <-- is_updated_geometry=True this frame" if is_updated_geometry else ""
        print(f"    {name!r}: loc={loc} scale={scale}{flag}")

print("\n=== checking each object's own matrix_world for unexpected movement ===")
first_seen = {}
moved = 0
for step, (f, rows) in per_frame.items():
    for name, data in rows.items():
        if data is None:
            continue
        loc, scale, _ = data
        if name in first_seen:
            prev_loc, prev_scale = first_seen[name]
            if loc != prev_loc or scale != prev_scale:
                moved += 1
                print(f"  !! {name!r} changed: was loc={prev_loc} scale={prev_scale}, "
                      f"now (step={step} frame={f}) loc={loc} scale={scale}")
        else:
            first_seen[name] = (loc, scale)
if moved == 0:
    print("  none -- every listed object's own matrix_world is constant across all "
          "tested frames, as expected for a static object. If the rendered image still "
          "shows scale/origin doubling, it is not because Blender moved/rescaled the "
          "object -- the corruption happens purely inside the exporter/LuxCore side.")
