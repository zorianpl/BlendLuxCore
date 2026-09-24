# Persistent Data (Animation)

An experimental, opt-in feature that speeds up animation renders by
keeping a single LuxCore render session alive across all frames,
instead of re-exporting and re-uploading the whole scene to the render
device on every frame.

## Enabling it

Render Properties > LuxCore Tools > **Persistent Data (Animation)**
(checkbox in the panel header). Off by default; has no effect on
single-frame (F12) renders.

To keep a specific object (or a Geometry Nodes / Collection Instance
parent that generates instances) moving or changing after the first
frame, enable **Always Re-check (Persistent Data)** on it, in Object
Properties > LuxCore.

## What actually happens

- Frame 1 renders exactly like a normal final render: a full export,
  a fresh `RenderSession`.
- Every frame after that reuses the *same* session. Only the camera,
  and any object/instancing-parent flagged "Always Re-check", are
  re-evaluated and re-uploaded. Everything else stays exactly as it
  was exported on frame 1.

This avoids the expensive part of starting a render from scratch (BVH
build, texture/geometry upload to the GPU device) on every frame, which
dominates the per-frame cost of a normal render far more than the
Python-side scene export does.

## Why nothing is inferred from Blender's dependency graph

An earlier version of this feature tried to detect per-frame changes
automatically, the same way viewport rendering already does (via
Blender's depsgraph: `depsgraph.updates` / `is_updated_geometry`).
That approach was tried and dropped -- it isn't reliable enough here:

- It can report a change **too broadly**. A Geometry Nodes system
  producing new instances anywhere in the scene can spuriously flag
  unrelated, untouched objects as "geometry updated" on that same
  frame. This was confirmed directly: a completely static object had
  its scale doubled purely because some *other*, unrelated GN system
  happened to produce new instances on the same frame.
- It can just as easily **miss** a real change that does need picking
  up, with no error or warning -- silently wrong output is worse than
  no automation at all.

So instead, Persistent Data (Animation) follows the same model used by
other renderers built around a persistent render session (e.g. Octane):
every object is exported once and then treated as completely static,
full stop. Nothing is guessed from change-tracking metadata. The only
way anything moves or changes after frame 1 is if you say so
explicitly, per object, with the "Always Re-check" flag. Everything not
flagged stays on the fast, already-proven batched export path; only the
explicitly-flagged subset pays any per-frame cost at all.

## Known limitations

- Only the camera and explicitly-flagged objects can change after
  frame 1. Everything else -- object, material, light, world and
  visibility changes -- is frozen as exported on frame 1.
- No live mid-frame scene editing (e.g. dragging a slider while a
  frame is rendering) for the reused-session frames.
- A flagged object's mesh, if also instanced by some *other*,
  unflagged source (a second Geometry Nodes group, or a plain
  Collection Instance) sharing the same mesh, is handled correctly but
  costs one extra batch per distinct instancing source -- see
  `PERSISTENT_DATA_ANIMATION_NOTES.md` for why.

## Further reading

`debug-helpers/PERSISTENT_DATA_ANIMATION_NOTES.md` has the full,
chronological investigation this feature came out of -- every bug
found and fixed along the way, with root causes and the evidence that
led to each one. Useful if something in this area breaks again and you
need the reasoning behind a specific piece of code, but not required
reading to just use or maintain the feature day to day.
