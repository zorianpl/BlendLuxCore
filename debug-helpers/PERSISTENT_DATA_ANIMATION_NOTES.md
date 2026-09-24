# Persistent-data animation caching — status notes (2026-09-22)

Branch: `feature/persistent-data-animation`. Goal: avoid re-exporting geometry
and re-uploading textures to the render device on every frame of an
animation, when only object visibility/transforms change (e.g. Geometry
Nodes toggling which instances exist) and the underlying mesh/material data
does not.

Test harness: `debug-helpers/persistent_data_gpu_test.py` (also mirrored at
`F:\Luxcore_Addons\persistent_data_gpu_test.py`). Run against the real
production scene at
`F:\Luxcore_Addons\Test_Scene\Animation_Persisten_Data\Render_Scene_Windows.blend`.
It does a full export on the first listed frame, then for each following
frame calls `Exporter.get_changes(..., force_diff=True)` + `Exporter.update()`
to apply only the diff, then **recreates** a fresh `pyluxcore.RenderSession`
from the same (unchanged) `RenderConfig` rather than reusing the old session.

Build/packaging: zips are produced with
`blender --factory-startup --command extension build --source-dir <repo> --output-filepath <name>.zip`
(same as the CMake `extension` target) and dropped on the network share as
`BlendLuxCore_ConditionalBatching6_persistent-data_v<N>.zip`. No wheel
bundling needed — pyluxcore installs itself on first addon startup.

## Bug #1 (FIXED today, uncommitted): leftover GN instances after deletion

**Symptom:** toggling a Geometry Nodes setup from 4 instances to 0 only ever
removed 1 of them ("4/0" expected, "4/3" observed) — present since v2,
through v6/v7.

**Root cause (confirmed via a `scene.ToProperties()` dump of the live
LuxCore scene, see chat log):** `ObjectCache2.duplicate_instances()` calls
`luxcore_scene.DuplicateObject(src_name, dst_name, count, matrices,
object_ids)` where `dst_name = src_name + "dupli"`. The code (and the
comment above `ExportedObject.has_duplicates`) assumed this registers ONE
LuxCore object named `"<src>dupli"` holding all the extra instances. It does
not: LuxCore actually creates one object **per extra instance**, named
`"<src>dupli0"`, `"<src>dupli1"`, ... `"<src>dupli<count-1>>"` — confirmed
directly in the SDL properties dump (`scene.objects.<key>dupli0.*`,
`...dupli1.*`, `...dupli2.*` for a 4-instance group with 3 extra copies).

`ExportedObject.delete()` therefore called `DeleteObject("<src>dupli")` —
a name that never existed. `pyluxcore.Scene.DeleteObject()` has no
"unknown object" error path (checked via `strings` on the compiled
`pyluxcore.*.so`: no such message exists for `DeleteObject`, unlike
`UpdateObjectTransformation`/`UpdateObjectMaterial` which do have one) — it
silently no-ops on an unknown name. So the base object was correctly
deleted every time, but the whole batch of extra duplicated instances was
never touched, leaving N-1 behind forever. This matches "removes only the
first" exactly.

**Fix applied** (uncommitted — files modified but not yet `git commit`ed):
- `export/caches/exported_data.py`: `ExportedObject.has_duplicates` (bool)
  replaced with `duplicate_count` (int). `delete()` now loops
  `for i in range(self.duplicate_count): luxcore_scene.DeleteObject(part.lux_obj + "dupli" + str(i))`.
- `export/caches/object_cache.py`: `duplicate_instances()` now sets
  `duplis.exported_obj.duplicate_count = duplis.get_count()` instead of the
  bool flag. Removed the now-obsolete debug prints from the previous
  (unsuccessful) fix attempt (commits `72cff130`, `bd9bce91`).
- `debug-helpers/persistent_data_gpu_test.py`: unrelated fix, import casing
  — extension id in `blender_manifest.toml` is `blendluxcore` (lowercase),
  script imported `bl_ext.user_default.BlendLuxCore` (capitalized), which
  `ModuleNotFoundError`'d on a fresh Windows install (worked by accident on
  the Linux dev machine because its local extension folder happened to
  still be named `BlendLuxCore` from an older install).

Packaged as `BlendLuxCore_ConditionalBatching6_persistent-data_v8.zip` on
the network share. **Confirmed fixed by user testing**: ran frames 2-3 on
the actual production scene (`Render_Scene_Windows.blend`, the frames where
the GN setup changes instance count), duplicates were removed correctly and
everything rendered as expected. The only remaining problem after this fix
is the loss of the speedup v6 used to have — see the section below, that is
now the main open item, not correctness.

**TODO:** commit the fix (`export/caches/exported_data.py`,
`export/caches/object_cache.py`) — it's currently only in the working tree,
confirmed working, safe to commit next session.

## Open question: no speedup observed in v8 — is it just re-rendering everything?

Your suspicion, roughly: "v8 behaves like a normal Render Animation, full
recompute every frame." **Partially true, but not for the reason it looks
like.** Two independent costs are involved, and only one of them is
actually being cached:

1. **Python-side export** (mesh conversion, material parsing, `scene.Parse()`)
   — **is** being skipped on diff frames via `force_diff`, same mechanism
   viewport rendering already uses. This is real and working (RAM stayed
   flat at `delta=0.0MB` across repeated frames in testing — no leak from
   repeated re-conversion).
2. **LuxCore's own device upload** (BVH build + texture/geometry upload to
   the OpenCL device) — **is not** being cached. The harness deliberately
   does `session.Stop()` → `session = pyluxcore.RenderSession(renderconfig)`
   → `session.Start()` on every diff frame instead of editing a live
   session. Constructing a new `RenderSession` makes LuxCore redo the full
   device upload from scratch every time, regardless of whether the
   underlying `Scene` object actually changed.

Evidence this is what's happening: VRAM reported by
`get_vram_usage()` was **identical** on every frame in both test runs
(1736MB → 1736MB single model; 2367MB → 2367MB with a second model added) —
if the device-side data were persisted/cached, VRAM usage on later frames
should not require re-uploading the same amount. Frame time roughly tracked
total scene size (going from ~6.5s to ~11.1s per frame when a second model
was added, for **every** frame including diff frames) rather than staying
flat after the first frame, which is what you'd expect if only the first
frame paid the full cost.

**Why the harness recreates the session instead of editing it live**
(per the original PoC commit `00c607ea`'s own docstring): a live session's
`HasDone()` latches permanently `true` once a halt condition is hit, and
`Film.Clear()` alone does not un-stick it — so re-using one session across
simulated frames got stuck after the first. Recreating the session dodges
that, and also dodges a known memory leak in
`_update_config()`/`force_session_restart()` (GitHub issue #577). Checked
the compiled `pyluxcore.*.so` for an escape hatch (`IsObjectDefined`-style
reset, some way to un-latch `HasDone()` without recreating) — found
`RenderSession::Create(RenderConfig, RenderState, Film)`, an overload that
takes an existing `RenderState`/`Film`, not currently used anywhere in this
code and not yet investigated — possibly relevant to preserving device-side
state across a "recreate" instead of a plain `Create(RenderConfig)`. Worth
checking if that overload also skips the device upload step, since it's
the one part of the RenderSession lifecycle that looked promising and
hasn't been explored.

**Conclusion:** v8 is not literally "recompute everything" (the Python
export savings are real), but for a texture/geometry-heavy production scene
the device-upload cost dominates and completely swamps that saving, so the
net effect on wall-clock time is indistinguishable from a full re-export —
which is exactly the "suspicion" you had, just for a more specific reason
than "nothing is cached."

## Side investigation: why did v6 show 6s → 1.5s, if it had the same bug?

v6 also had the "4/3" dupli-deletion bug (confirmed by you — same "one
instance removed, 3 remain" symptom), so that's not what changed. Diffed
v6's `object_cache.py` against current: the only difference is a
`concurrent.futures.ThreadPoolExecutor(8 workers)` wrapped around
`duplicate_instances()`'s `DuplicateObject()` calls, guarded by one global
`scene_lock` (added specifically because unsynchronized concurrent
`DuplicateObject()` calls were **silently corrupting/dropping instances** —
a second, separate bug from the naming bug above). That code was never
committed to git (not found via `git log -S"ThreadPoolExecutor"`), so it
only ever existed in the working tree that built v6.zip.

This lock effectively serializes the actual `DuplicateObject()` C++ calls
anyway — the threads mostly just queue up behind `scene_lock`. There was
also a dead/unused `conversion_tasks = []` list in `first_run()` suggesting
a planned (but never wired up) parallel mesh-conversion path;
`_convert_obj()` was still called synchronously. **This threading code is
very unlikely to be the actual source of a 4x speedup** given how little of
it ran in parallel.

**Best guess:** the 6s → 1.5s measurement was on a lighter test scene (the
harness's own `CONFIG` block defaults to `NAME_FILTER = "Plane"`, suggesting
a small built-in test scene, not the tree-heavy production scene used in
today's v8 test) where the device-upload cost is small enough that skipping
the Python-side export dominates the savings and is clearly visible —
whereas today's heavy scene hides that same saving behind a large, constant
device-upload cost.

**Not yet verified** — need to re-run v8 (or current code) against
whatever scene/params originally produced the v6 "6s → 1.5s" result, for a
real apples-to-apples comparison. If v8 reproduces ~1.5s on that lighter
scene, it confirms the export-side saving was real all along and the
"no speedup" today is purely a heavy-scene device-upload artifact, not a
regression from removing the (mostly inert) threading code.

## Next steps

1. Commit the fix (`exported_data.py`, `object_cache.py`) — correctness is
   confirmed by user testing (frames 2-3, production scene, GN instance
   count change), nothing else blocking this.
2. **Main open item:** get the speedup back without reintroducing the
   dupli-deletion bug. Re-run the original lighter scene/params that
   produced v6's "6s → 1.5s" against v8, to check whether the export-side
   saving alone reproduces that number (see section above) — this tells us
   whether the missing speedup is purely the heavy-scene device-upload cost
   (expected, see section above) or something else regressed too.
3. If device-upload caching is wanted for heavy scenes too, investigate
   `RenderSession::Create(RenderConfig, RenderState, Film)` as a possible
   way to avoid a full re-upload on "recreate" — currently unused, found
   only via `strings` on the compiled `.so`, not tested.
4. Decide whether the `ThreadPoolExecutor`-based parallel conversion idea
   from v6 is worth resurrecting properly for the CPU-bound mesh/material
   conversion step in `first_run()` (independent of the GPU-upload
   question) — the v6 attempt never actually wired it up.

## 2026-09-23: attempt at getting the speedup back (untested — new Claude session, no GPU on dev machine)

User confirmed v8 gives literally no speedup ("renderuje się tak jakbym
włączył render animation z blendera") and re-pasted the v7-era log showing
the pre-fix "4/3" dupli bug as context — that log predates the dupli-count
fix above, which is unaffected by anything in this section and does not
need to be revisited; it's confirmed correct and still just needs a commit
(see "Next steps" item 1 above, still outstanding — not committed this
session either, in case the change below needs revision after real
testing and both should go in together).

**Change made:** `debug-helpers/persistent_data_gpu_test.py` no longer
does `session.Stop(); session = pyluxcore.RenderSession(renderconfig);
session.Start()` after a diff frame. It now keeps ONE live session for
the whole run, edits it in place via the existing `exporter.update()` ->
`BeginSceneEdit()`/`EndSceneEdit()` path (same path viewport render uses
continuously), then only calls `session.GetFilm().Clear()`.

**Why recreation was believed necessary (and why that's sidestepped, not
solved):** `session.HasDone()` (and the built-in `batch.haltspp` halt
condition) was found to latch `True` permanently after the first frame,
surviving `Film.Clear()` on a reused session — this was the original
reason the harness recreated the session instead of reusing it live. The
exact mechanism was never root-caused (no LuxCore C++ source available
locally, only the compiled `pyluxcore.*.so` via `strings` — see wheel at
`.../BlendLuxCore/wheels/pyluxcore-2.11.2-*.whl`). Rather than debug that
latch, this version avoids it entirely: `scene.luxcore.halt.enable = False`
(no built-in halt condition reaches LuxCore at all, `batch.haltspp`/
`batch.halttime` stay `0`), and each frame's "enough samples" check is done
in Python by polling `session.GetStats().Get("stats.renderengine.pass")`
and waiting for it to advance by `SAMPLES` from wherever it stood at the
start of that frame. This is agnostic to whether that counter resets to 0
on a scene edit or keeps climbing — either way the wait-for-delta logic is
correct — and it never calls `HasDone()` or recreates anything, so it
can't reproduce the latch.

**Evidence recreation (not the diff itself) was the actual cost:** in the
v7/v8-era log the user pasted, the frame 2->3 step is `changes=CAMERA |
REQUIRES_SCENE_EDIT | VISIBILITY | WORLD` — camera move only, no
geometry/material change — yet it still printed the full `Compile Geometry
/ Compile 157 Textures / Compile ImageMaps (~350ms) / Building Optix
accelerator / Starting N render threads` sequence, identical in cost to
the very first full export. Viewport render moves the camera through the
exact same `BeginSceneEdit`/`EndSceneEdit` path continuously without ever
triggering that sequence, so it cannot be `EndSceneEdit()` itself causing
it — it has to be `pyluxcore.RenderSession(renderconfig)` being
reconstructed from scratch every frame, forcing a full device re-upload
regardless of how small the actual scene diff was.

**Status: UNVERIFIED.** Not run at all yet — no GPU on this dev machine,
next test has to happen on the Windows render machine after a new build,
same as always. Two assumptions need to survive that test:
1. `Film.Clear()` actually resets what `SaveOutput()`/`GetFilmY()` read —
   if not, saved frames will look like a ghosted blend of consecutive
   frames (watch the saved PNGs and the `lumY` values in the log for
   values that don't make sense for the given frame).
2. `BeginSceneEdit()`/`EndSceneEdit()` is actually cheap for this
   production scene's per-frame diffs — if the log still shows `Compile
   Geometry`/`Compile Textures`/`Compile ImageMaps`/`Building Optix
   accelerator` repeating on frames whose `changes=` line doesn't include
   `OBJECT` or `MATERIAL`, or if `edit=` in the new per-frame log line
   stays close to the first frame's time, the recompile is coming from
   inside `EndSceneEdit()` itself for this scene, not from session
   recreation, and this fix doesn't help — would need profiling inside
   `EndSceneEdit()`/`ObjectCache2.update()` next, not this session-lifecycle
   angle.

If this test also hangs printing `!! timeout waiting for pass`, that's the
`HasDone()`-latch bug's sibling: the render threads themselves got stuck
(not just the halt flag), and reusing the session live doesn't actually
work at all for this render engine/device combo — would need to fall back
to investigating `RenderSession::Create(RenderConfig, RenderState, Film)`
after all (see "Next steps" item 3 above), despite it being more likely
intended for resuming an interrupted single-frame render (checkpoint
save/resume) than for carrying device state across scene edits — that
overload was not usable to test this theory either, still unconfirmed.

**Next step after this test:** paste back the new log (in particular the
`edit=`/`render=` split per step and whether the LuxCore console still
prints a full Compile/Building-accelerator sequence on non-OBJECT/MATERIAL
frames), plus confirm the saved frames look correct. If confirmed working,
commit this alongside the still-pending dupli-count fix together.

## 2026-09-23 (continued): v9 test results -- real speedup confirmed, two new bugs found

User's first v9 test (frames 2-3) was invalidated: `always_reexport` was
still set on an object from the earlier workaround (see "Next steps" item
above -- this flag turned out to force a FULL scene export every frame in
this test harness, see `debug-helpers/persistent_data_gpu_test.py`'s
`force_full_export` check -- it's a blanket per-*run* override, not
per-object, despite the property's name/tooltip suggesting otherwise; it
is not read anywhere in `export/`, only in this harness). Confirmed via
`[persistent_data_gpu_test] always_reexport is set on at least one object`
in that log and `changes=FULL_EXPORT` on both steps with near-identical
~13s edit time and a full Compile/Building-accelerator sequence on both.

After disabling that flag (frames 6-7 test, `67_Plane.txt`): **real
speedup confirmed** -- `edit=7.885s` (FULL_EXPORT, frame 6) ->
`edit=1.165s` (diff, frame 7, `changes=CAMERA | OBJECT | REQUIRES_SCENE_EDIT
| VISIBILITY | WORLD`) with no repeated Compile Geometry/Textures/
ImageMaps/Building Optix accelerator sequence on the diff frame. The
live-session-reuse approach (never recreating `pyluxcore.RenderSession`,
editing in place via `BeginSceneEdit`/`EndSceneEdit`, driving per-frame
sample counts in Python against `stats.renderengine.pass` instead of the
built-in halt) works and gives a real, large speedup even across an
OBJECT-changing edit, not just camera-only.

### Bug A (FIXED by user, no code change needed): "Plane" object cut in half when GN instances reappeared

User's own diagnosis, plausible and apparently confirmed by testing:
the "Plane" object (the ground/backdrop mesh, `Mesh_Plane...` in the SDL
dump, a separate static object unrelated to the tree GN instancing) had a
non-zero `matrix_world` origin. When GN instances reappeared (duplicate
count changing), the Plane appeared to shift/get cut in half. Setting the
Plane's origin to (0,0,0) resolved it. Not yet root-caused in the code
(no LuxCore source available locally to confirm the mechanism -- plausible
guess: something in the batching/duplication math assumes/interacts badly
with a non-origin transform when the object count in a `DuplicateObject()`
batch changes between frames), but the workaround is simple and user-side,
no addon change needed unless it recurs on another off-origin object.

### Bug B (OPEN, actively being diagnosed): ghosting -- frame N+1's saved image shows frame N blended on top

After fixing Bug A, user reports: on the frame where GN instances
reappear, the saved image looks like two renders overlaid -- "jakby klatka
7 wczytuje poprzednią klatkę 6" (as if frame 7 loads frame 6 on top).
This is exactly the risk flagged in this file and in the harness's own
module docstring when the live-session-reuse change was made: "if the
saved frames come out visually wrong (ghosting/blending between frames)
... Film.Clear() actually clearing what feeds the saved image ... doesn't
hold."

**Supporting evidence this is a Film.Clear()-adjacent issue, not something
else:** in the `67_Plane.txt` log (a *different*, ghosting-free transition,
included here as the only hard data point so far), `stats.renderengine.pass`
went from 690 (end of frame 6) to 422 (immediately after
`session.GetFilm().Clear()` on frame 7) -- i.e. neither a clean reset to 0
nor a continuation from 690. This counter's behavior across a live-session
`Clear()` is clearly not simple/well understood yet.

**Diagnostic added (untested):** `debug-helpers/persistent_data_gpu_test.py`
now prints `session.GetFilm().GetFilmY()` immediately after
`session.GetFilm().Clear()`, before any new samples are requested for that
frame. This distinguishes the two live hypotheses in one test run:
- If that value is close to the *previous* frame's final `lumY`: `Clear()`
  (or whatever `SaveOutput()`/`GetFilmY()` actually reads) is not resetting
  the image content at all -- the old frame's radiance is still fully
  there right after Clear() returns, before any new sample has even run.
  This would point at `Film.Clear()` not being synchronous with the
  device-side "Film_Clear Kernel" (seen being compiled once at session
  start in the logs) -- i.e. Clear() returns before the GPU has actually
  executed the reset, and by the time SaveOutput() runs the reset may or
  may not have caught up, explaining an inconsistent/50-50-ish blend.
- If it's close to 0: Clear() itself works, and the leak happens somewhere
  later (in the sample-accumulation loop, in the hardware image pipeline
  step at `SaveOutput()`, or in `AsyncExecuteImagePipeline`/
  `HasDoneAsyncExecuteImagePipeline` -- another pyluxcore API surface,
  seen via `strings` on the compiled `.so`, not yet investigated -- that
  the addon does not currently call explicitly and that may need to be to
  guarantee the imagepipeline reflects only post-Clear samples).

**Next step:** re-run the exact transition where the ghosting was observed
(the frame before instances reappear, then the frame where they reappear
and the Plane origin is already at 0,0,0) with `OUTPUT_DIR` set, and paste
back the console log -- specifically the two new `[diag] lumY immediately
after Film.Clear()` lines and the saved PNGs. That single result should be
enough to tell which of the two hypotheses above is right and what to fix
next, without more blind guessing.

## 2026-09-23 (continued): Bug C -- `always_reexport` wired up for real, GN "Duplicate Collection" instances not disappearing

User found a third bug, confirmed to reproduce **only** in
`persistent_data_gpu_test.py` (not in a plain Render Animation, which
never uses the diff/update path at all -- every frame is independently a
full, ground-truth-correct export there): a GN setup using "Duplicate
Collection" + "Collection Instance" to place trees does not remove those
tree instances when GN's own logic says they should disappear. Manually
hiding the Collection Instance object *does* remove them correctly.

**Root cause, found by reading (not yet confirmed by a log from this exact
scenario):** hybrid batching. `ObjectCache2.first_run()` (called only once,
at `create_session()`/full export time) merges same-mesh dupli instances
into one `ExportedObject` + N-1 `DuplicateObject()` copies in LuxCore,
tracked under a single "base" key in `self.exported_objects`. Critically,
`ObjectCache2.update()` (the diff/persistent-data path) **never re-batches
and never touches `duplicate_count`** -- its second loop only does one of
two things per currently-existing `depsgraph.object_instances` entry: a
cheap transform/visibility update if its key is already in
`exported_objects`, or a full `_convert_obj()` if not. A batch's "extra"
(non-base) instances were never individually stored in `exported_objects`
in the first place (that's the whole point of batching), so there is no
per-update mechanism that reduces `duplicate_count` when GN's instance
count for an already-batched group shrinks (but doesn't go all the way to
zero -- see the "4/3" bug and its fix earlier in this file, which does
cover the down-to-zero case via `VisibilityCache.objects_to_remove`
correctly identifying the base key as gone). The `Duplicate Collection`
node's dupli enumeration/ordering may also just not interact well with
which instance ends up as "the base" -- not confirmed, no log from this
exact scenario yet.

**Fix (untested):** wired up the existing-but-previously-unused
`obj.luxcore.always_reexport` property (added earlier as a workaround for
the "4/3" bug, `properties/blender_object.py:42`, its tooltip already
described exactly this intended behavior even though nothing consumed it
in `export/`) for real, in `export/caches/object_cache.py`:
- New `_wants_individual_tracking(obj, dg_obj_instance)` helper: true if
  `obj.luxcore.always_reexport` or the instance's parent (the Collection
  Instance empty, for nested collection duplication) has it set.
- `first_run()`'s hybrid-batching condition now excludes any instance
  where this is true, routing it through the existing "singular object"
  branch instead (`_convert_obj()` called per-instance, each getting its
  own key in `exported_objects`) -- the same code path that already
  correctly handles the manual-hide case, per the user's own report.
- Mesh/material data is still cached independently by `mesh_key` in
  `self.exported_meshes` (confirmed by reading `_convert_mesh_obj`), so
  this does not mean re-exporting geometry per instance, only accepting
  the (smaller, per-instance-props-only) overhead of tracking each
  instance individually instead of as one batch -- matches
  `DESC_ALWAYS_REEXPORT`'s existing description exactly.
- Removed `debug-helpers/persistent_data_gpu_test.py`'s old blanket
  `force_full_export` check (any object with the flag forced a FULL
  export of the *entire scene* every frame) -- that behavior is now wrong
  given the flag has real per-object meaning; the harness now always uses
  the diff path from frame 2 onward, same as before this flag existed.

**How the user should use it:** enable "Always Re-check (Persistent Data)"
(Object Properties, LuxCore panel) on the Collection Instance object (or
directly on the instanced tree object) that's misbehaving. Everything
else keeps using persistent-data/hybrid-batching as before; only that
one GN group gets individually tracked and re-evaluated every diff frame.

**Not yet verified:** no log from this exact GN pattern was available to
confirm the root-cause read above before implementing the fix -- next
test should confirm both that the flagged group now disappears/reappears
correctly *and* that everything else still shows the speedup from the
live-session-reuse work above.

**CONFIRMED by user testing:** flagging the Collection Instance with
`always_reexport` fixed both the GN removal bug and the ghosting -- the
ghosting was apparently a symptom of the same corrupted-batch-state issue
(a stale/mismatched batch after an undetected instance-count change),
not a separate `Film.Clear()` problem. The `[diag] lumY immediately after
Film.Clear()` print is still in the harness but turned out not to be
needed for this; can be removed whenever, it's harmless either way.

## 2026-09-23 (continued): auto-detect Geometry Nodes instead of relying on the manual flag

User added a second Collection Instance object (different GN setup, same
"Duplicate Collection" pattern) and forgot to enable `always_reexport` on
it -- reproduced the exact same "instances never disappear" bug. Manually
flagging every GN object that needs this is fragile (as just demonstrated
on the very next one added). Fixed in `export/caches/object_cache.py`:

- New `_has_geometry_nodes_modifier(obj)` (`@lru_cache(maxsize=128)`,
  not cleared per-frame like `supports_live_transform()` -- an object
  gaining/losing a Nodes modifier mid-render isn't a normal case, a stale
  answer for one frame is harmless): checks `obj.modifiers` for any
  `type == "NODES"` modifier.
- `_wants_individual_tracking()` now also returns true if either the
  instance's own object or its instancing parent (the Collection Instance
  empty, or whatever object hosts the modifier that generated this dupli)
  has a Geometry Nodes modifier -- no manual flag needed for the common
  case anymore. The manual `always_reexport` flag still exists as an
  override for anything this detection doesn't catch (e.g. a non-GN
  visibility-driver setup).
- Updated `DESC_ALWAYS_REEXPORT` tooltip in `properties/blender_object.py`
  to reflect that GN objects get this automatically now.

**Trade-off, not yet a problem but worth knowing:** this opts *every*
Geometry-Nodes-instanced object out of hybrid batching by default, even
ones whose instance count never actually changes across the animation
(e.g. a static GN-scattered forest) -- those pay the individual-tracking
memory/performance cost for no correctness benefit, since batching would
have been safe for them. No way to opt back into batching for a
known-stable GN object currently exists (would need a second flag, e.g.
"force batching despite GN", if this turns out to matter for a heavy
scene). Not implemented since it hasn't come up yet -- revisit if a
production scene shows a real cost from this.

**Not yet verified** against the two-Collection-Instance scene that
surfaced this.

## 2026-09-23 (continued): the test harness was reading VIEWPORT-mode geometry, not RENDER-mode

User asked whether the persistent-data test script renders what's in the
viewport (their viewport is often heavily simplified -- low-poly proxies,
hidden collections, for interactive speed) or what a real Render Animation
would produce. **Confirmed locally, no GPU needed:** yes, it was reading
viewport-mode geometry.

Root cause: `debug-helpers/persistent_data_gpu_test.py` called
`bpy.context.evaluated_depsgraph_get()` directly, once per frame. Verified
with a throwaway `--background --factory-startup` script (a cube with a
Subsurf modifier set to `levels=0` (viewport) / `render_levels=3`
(render)): `depsgraph.mode` from `evaluated_depsgraph_get()` is
`"VIEWPORT"`, and the evaluated mesh has the *viewport* polycount (6, not
the render one) -- true even in plain `--background` with no window open,
and true even immediately after manually calling `bpy.ops.render.render()`
once (that operation's own RENDER-mode depsgraph is a separate, temporary
object, not something `evaluated_depsgraph_get()` returns afterwards --
`Depsgraph.mode` is read-only, there is no public API to request
RENDER-mode evaluation outside of Blender's own render invocation).

**Fix, confirmed locally (same throwaway script) to actually receive a
RENDER-mode depsgraph, matching each frame:** the script no longer calls
`evaluated_depsgraph_get()` at all. It registers a temporary, throwaway
`bpy.types.RenderEngine` subclass (`_PersistentDataTestEngine`, unrelated
to the real `LUXCORE` engine, doesn't touch pyluxcore itself) and drives
the frame loop via `scene.frame_set(f); bpy.ops.render.render(write_
still=False)` per frame -- Blender calls the engine's `render(self,
depsgraph)` once per that call, with a depsgraph confirmed to be `mode ==
"RENDER"` and correctly frame-specific. The actual export logic
(`run_frame()`, calling `Exporter.create_session()`/`get_changes()`/
`update()`) is unchanged, just called from inside that callback instead
of a bare loop.

**Why this doesn't affect the real feature:** `engine/final.py`'s
`render(engine, depsgraph)` is already invoked this same way by Blender's
normal "Render Animation" -- this bug was specific to the standalone
script's simplified, non-RenderEngine invocation method for fast
iteration, not to the export code itself. Confirmed nothing in
`export/caches/object_cache.py` or elsewhere reads `bpy.context` directly
for geometry (everything takes `depsgraph` as an explicit argument), so
this was a single, isolated bug in one file.

**Not yet re-verified against the real production scene** -- the local
throwaway-cube test confirms the mechanism (RenderEngine.render() gets
RENDER-mode, evaluated_depsgraph_get() doesn't), but the actual harness
change (register temp engine class, drive via bpy.ops.render.render())
has not been run against pyluxcore/the real scene yet.

## 2026-09-23 (continued): v12 crashed on a later frame, fixed by driving all frames from ONE render() call

User's v12 test: meshes now correctly loaded at render quality (confirms
the RENDER-mode fix worked -- previously this was silently using viewport
proxies). But two things:

1. Edit time on diff frames went from ~0.12s to ~4.5s compared to a run
   where the viewport had low-poly proxies active. **Expected, not a
   regression** -- 0.12s was diffing tiny proxy geometry; 4.5s is diffing
   the real production meshes, which is what should have been happening
   all along.
2. Crash partway through the frame list (`session.BeginSceneEdit():
   RuntimeError: invalid argument`), which then also broke the cleanup
   (`session.Stop()` in the `finally:` block threw the same error) --
   render did not finish.

**Root cause (fixed, not yet re-verified):** v12 drove the frame loop by
calling `bpy.ops.render.render(write_still=False)` once *per frame*, each
a separate operator invocation, while keeping one pyluxcore
`RenderSession` alive across all of them via module-level globals. That's
not how a `RenderEngine` is normally used -- every other caller
(`engine/final.py`'s real `render()`, included) creates and tears down
its session within a *single* continuous `render(self, depsgraph)` call.
Persisting a live session across several separate render-operator
invocations was never validated and is the most likely explanation for
`BeginSceneEdit()` suddenly rejecting the session with "invalid argument"
partway through.

**Fix, confirmed locally that the depsgraph mechanics work (not yet with
pyluxcore):** the frame loop now lives entirely *inside* the engine's
`render(self, depsgraph)` method -- `bpy.ops.render.render()` is called
exactly once, and each frame inside that one call does `scene.frame_set(f)`
followed by `depsgraph.update()`. Verified with the same throwaway-cube
script: `depsgraph.mode` stays `"RENDER"` and `depsgraph.scene_eval.
frame_current` / the evaluated mesh correctly advance per iteration this
way (calling `scene.frame_set()` alone, without `depsgraph.update()`,
does NOT re-evaluate the depsgraph -- the frame silently stays stuck at
whatever it was when `render()` started, a second gotcha found in the
same test). This also removes the repeated register/engine-switch
overhead the per-frame-operator version had.

**Not yet re-verified** -- next test should confirm both that the crash
is gone and that the render now completes through the whole `FRAMES`
list.

## 2026-09-23 (continued): the GN auto-detection (v11) was too broad, narrowed to the instancing parent only

After the RENDER-mode fix, edit time on diff frames was still much higher
than expected. User clarified: not a proxy-vs-full-mesh issue (already
using full meshes in both cases) -- one of their meshes uses a standard
"Is Viewport" GN setup (convex hull proxy in viewport, real mesh in
render). Its output does **not** vary across the animation (it's a static
viewport/render switch, not a per-frame change), and "that high-poly dupli
is in memory anyway, it's used elsewhere" -- i.e. it's a batched dupli via
the existing hybrid-batching mechanism, batching was working fine for it.

**Root cause:** v11's `_has_geometry_nodes_modifier(parent) or
_has_geometry_nodes_modifier(obj)` checked the instanced object's OWN
modifiers too, not just its instancing parent. A GN modifier used purely
for a self-contained viewport/render mesh swap (no duplication, no
instance-count logic at all) has nothing to do with the bug that
motivated auto-detection (GN-driven instance-count changes not being
picked up by `update()`), but was still pulling every dupli of that
object out of hybrid batching, forcing per-instance individual tracking
for no reason.

**Fix:** `_wants_individual_tracking()` now only checks
`_has_geometry_nodes_modifier(parent)` -- the instancing *source*, not
`obj`. This matches an existing convention already used elsewhere in this
file for holdout/visibility (`check_obj = dg_obj_instance.parent if
dg_obj_instance.is_instance else obj` -- for a true instance, `parent` is
the object whose settings are authoritative, `obj` is just "which mesh
data"). The manual `always_reexport` flag is unaffected -- still checked
on both `obj` and `parent`, since that's an explicit user choice, not a
heuristic.

**Not yet re-verified.**

## 2026-09-23 (continued): investigating origin/scale corruption on GN reappearance

User (after confirming the GN auto-detection narrowing fixed the slowdown):
a mesh with a non-zero origin gets *shifted* when its GN instance
reappears, and a mesh with non-1,1,1 scale gets *bigger or smaller* --
"as if the values get multiplied". Same family of symptom as the earlier
"Plane cut in half" bug (Bug A above), which was only ever worked around
(zeroing the Plane's origin), never actually root-caused in code -- this
is presumably that same underlying bug, now visible on an object the user
can't/doesn't want to just zero out.

**Traced through the code (export/caches/object_cache.py) without being
able to reproduce locally (no .blend, no pyluxcore here):**
- Mesh geometry for an instanced object is stored in LOCAL/object space
  (`_get_mesh_key` uses `obj.original.data` when `use_instancing`), shared
  across all instances of that mesh -- NOT baked with any particular
  instance's world transform.
- Each instance's transform is applied separately via the SDL
  `scene.objects.<key>.transformation` property
  (`ExportedObject.get_props()`, `exported_data.py`), set directly from
  `dg_obj_instance.matrix_world` (`ExportedObject.transform`) -- no
  compounding visible in this code.
- `ObjectCache2.update()`'s second loop (the one that handles a
  reappearing instance) always calls `_convert_obj()` fresh for any
  `obj_key` not already in `self.exported_objects` -- which is exactly
  the case for a reappearing instance (its key was popped out when it
  disappeared) -- using the CURRENT frame's real `dg_obj_instance.
  matrix_world`, same as at first export. On paper this should be
  correct regardless of origin/scale.
- `duplicate_instances()`/`DuplicateObject()` (the hybrid-batching path,
  where a "multiply" bug in LuxCore's own matrix handling would be a
  natural suspect) is **not** involved in reappearance at all --
  `update()` never re-batches (confirmed earlier, see the "Bug C" section
  above), every reappearing instance goes through the same individual
  `_convert_obj()` path whether or not it was originally part of a batch.

**One real, separate bug found and fixed along the way (not yet confirmed
to be THE cause, but wrong regardless):** `ObjectCache2.update()` had two
calls to `self._get_mesh_key(obj, use_instancing)` missing the
`is_viewport_render` argument, silently defaulting to `True` --
`_get_mesh_key()` embeds `is_viewport_render` in the resulting name/hash
(`utils.get_luxcore_name(source, is_viewport_render)`), so during a real
final-render diff update (`is_viewport_render` should be `False`) these
two call sites were computing a *viewport*-flavored mesh_key while
`_convert_mesh_obj()`'s own internal call (correctly passing the real
value) computes the *render*-flavored one -- a mismatch that could plausibly
cause the wrong geometry variant to be associated with an object for
objects whose viewport/render geometry actually differs (e.g. the "Is
Viewport" convex-hull case from the previous section). Fixed by passing
`is_viewport_render` through at both call sites.

**Not yet root-caused for the origin/scale-specifically symptom** -- no
local reproduction possible (needs a .blend + pyluxcore). Next test
should confirm whether the `is_viewport_render` fix affects this at all,
and if not, needs either a minimal repro scene or targeted print
instrumentation comparing `dg_obj_instance.matrix_world` at first
appearance vs at reappearance for the same real object.

## 2026-09-23 (continued): built a pyluxcore-free diagnostic, found a real anomaly, user confirmed it's a plain mesh with un-applied transform

Built `debug-helpers/instance_transform_diag.py`: reads only
`depsgraph.object_instances` (same RenderEngine trick as the main
harness, no pyluxcore/LuxCore involved at all -- safe and fast even on
the heavy production scene), records each instance's key/translation/
scale across a frame list, and flags any key that disappears for at
least one recorded step and comes back with a different transform. Also
supports `--frames 6-7` range syntax (fixed a crash, only comma lists
were supported at first).

Tested against the light `Render_Scene.blend` (not the heavy Windows one,
per instruction) and found a real, reproducible anomaly at frame 7: for
what a user would call "one tree", Blender's depsgraph yields **two**
`object_instances` entries, same location, both `show_self=True`:
- one via the Collection-Info/outer level, `scale=(1,1,1)`
- one via the nested per-object level, `scale=(0.434,0.434,0.434)`
  (matching that object's own local scale)

Confirmed stable (re-evaluating the same frame 3x via `depsgraph.update()`
gives identical key sets each time) -- not a per-evaluation race.

User ran the script against the real scene (frames 6-7) and confirmed:
in that window nothing actually disappears-then-reappears (frame 7 only
*adds* instances on top of frame 6's), so the mismatch checker had
nothing to catch there -- need a frame range that actually spans the
disappear+reappear transition to use this checker meaningfully.

**Key clarification from the user:** the object with the wrong origin/
scale on reappearance is a **plain, regular editable mesh with location/
scale not applied** ("Apply Transform" never run) -- not the nested-
Collection-Info double-entry thing above, not an empty. This points at
`ObjectCache2._convert_mesh_obj()`'s `use_instancing` branch instead: for
`use_instancing=False`, the object's own transform gets baked directly
into the cached mesh's vertex data (`mesh_converter.convert(...,
transform, ...)`, `obj_transform` stays `None`); for `use_instancing=True`
the mesh stays in local space and the transform is applied separately via
`ExportedObject.transform`/`obj_transform`. If the *same* `mesh_key` for
this object were ever cached once under `use_instancing=False` (baked
into vertices) and reused later under `use_instancing=True` (transform
applied *again*, on top of the already-baked one), the object's own
location/scale would effectively apply twice -- exactly matching "as if
multiplied". Not confirmed yet, `_get_mesh_key()` includes `use_instancing`
in the key precisely to prevent this kind of cross-contamination, but
worth checking directly with real data rather than guessing further.

**Added a diagnostic print in the real exporter** (not just the
standalone script), `_convert_mesh_obj()` in
`export/caches/object_cache.py`, printing on every call: object name,
`use_instancing`, `mesh_key`, `loaded_from_cache`, `is_instance`, the
instance's translation/scale, and whether `obj_transform` ended up set.
Next step: user runs `persistent_data_gpu_test.py` across the actual
disappear+reappear frame range for the affected mesh and greps the log
for its name -- comparing `use_instancing`/`mesh_key`/`loaded_from_cache`
between its first appearance and its reappearance should directly show
whether the double-bake hypothesis above is right.

## 2026-09-23 (continued): ROOT CAUSE FOUND AND FIXED -- `update()` used a hardcoded, viewport-only `use_instancing=True` for final render too

User's key clarification that cracked this: the affected object **does
not disappear at all** -- it stays visible the whole time. Its scale gets
multiplied by itself (confirmed experimentally: set scale to 2, it becomes
4) at the exact moment *some* Geometry Nodes system elsewhere in the scene
*first* produces new instances -- and only that once; later GN
appearances elsewhere don't repeat it. Also clarified: the affected
objects are plain editable meshes with un-applied location/scale (e.g.
`Cube`, `Cube.001`, `Plane` in the user's minimal repro), not empties or
nested Collection-Info instances.

**Root cause, confirmed by reading `ObjectCache2.update()`
(`export/caches/object_cache.py`):** at the top of `update()`,
`use_instancing = True` was hardcoded unconditionally, with the comment
"Always instance in viewport so we can move objects around" -- clearly
written assuming this method only matters for viewport, but it runs for
every persistent-data *final* render diff too. This one wrong value was
used in two places:

1. The "Geometry updates" loop (handles `dg_update.is_updated_geometry`):
   computed `mesh_key` with this wrong `use_instancing=True` for objects
   like `Cube` that actually need `use_instancing=False` (a plain object,
   not a GN/particle dupli, no displacement/proxy/motion-blur -- see
   `_compute_use_instancing()`, new name for the formula that was already
   correct inside `_convert_mesh_obj()`). This produced the *wrong*
   mesh_key (`"Cube_instance"` instead of `"Cube"`) and added *that* to
   `redefine_objs_with_these_mesh_keys`.
2. The second loop (decides cheap transform-only update vs full
   `_convert_obj()`) recomputes `mesh_key` the same wrong way and checks
   it against `redefine_objs_with_these_mesh_keys` from step 1 -- for
   `Cube`, this now spuriously matches, so instead of the cheap path it
   falls through to a full `_convert_obj()`.
3. Inside `_convert_obj()` -> `_convert_mesh_obj()`, `use_instancing` gets
   computed *correctly* this time (`False` for `Cube`) -- which calls
   `mesh_converter.convert()` again with `transform=dg_obj_instance.
   matrix_world`, baking the object's world transform into the **already-
   baked** `"Cube000"` LuxCore mesh shape a second time via
   `luxcore_scene.DefineMeshExt(name="Cube000", ..., transformation=...)`.
   Fresh local-space vertices are supplied each call (`object_eval.
   to_mesh()` never includes the object's own transform, confirmed by
   reading `mesh_converter.py`), so the doubling isn't from stale
   vertices -- it must be LuxCore's own `DefineMeshExt` compounding the
   given `transformation` with what a same-named shape already had,
   rather than replacing it outright (not confirmed against LuxCore's own
   source, unavailable locally -- but this exactly explains every
   observed detail: doubles instead of resetting, and happens only once,
   since steps 1-2 only mis-fire on whichever frame first triggers a
   broad enough `depsgraph.updates` sweep to flag `Cube`'s geometry as
   updated -- which for this scene only happened to coincide with the
   first GN system elsewhere producing new instances, not a real change
   to `Cube` itself).

**Fix, in `export/caches/object_cache.py` (untested):**
- Factored the correct formula out of `_convert_mesh_obj()` into a shared
  `_compute_use_instancing(exporter, dg_obj_instance, obj,
  is_viewport_render)`.
- The "Geometry updates" loop no longer guesses one `use_instancing`
  value: it now checks *both* `_get_mesh_key(obj, False, ...)` and
  `_get_mesh_key(obj, True, ...)`, and re-converts+redefines only whichever
  variant(s) are actually present in `self.exported_meshes`, each with the
  transform appropriate for that variant (`obj.matrix_world` baked in for
  the `False`/singular case, `None` for the `True`/instanced case). An
  object that's *only* ever been exported as `use_instancing=False` (like
  `Cube`) now never gets the `True` variant spuriously created or
  redefined.
- The second loop now calls `_compute_use_instancing()` per-instance
  (matching `_convert_mesh_obj()` exactly) instead of reusing the old
  hardcoded module-level value, which no longer exists.

**New diagnostic tool** (per user request): `persistent_data_gpu_test.py`
now has `DUMP_TRANSFORMS` (default on) -- prints every exported object's
`ExportedObject.transform` (the actual Blender `Matrix` that gets sent to
LuxCore as `scene.objects.<key>.transformation`, i.e. authoritative, not
re-derived) once per frame via a new `dump_scene_transforms()` helper.
`DUMP_TRANSFORMS_FILTER` / `--dump-filter <substr>` narrows it to keys
containing a substring (e.g. an object name). Note: only covers
individually-tracked objects (`exported_objects` keys) -- a batched
dupli's extra `DuplicateObject()` copies aren't individually tracked and
won't show up here, only the batch's "base" key will.

**Not yet verified** -- next test should confirm the scale-doubling is
gone for `Cube`/`Cube.001`/`Plane` and for the real production meshes,
and that the `[diag] scene transform dump` output looks sane across the
frame where it used to double.

## 2026-09-23 (continued): v17 fix did NOT resolve it -- added a direct re-bake detector

User confirmed the v17 fix (the `use_instancing` hardcode fix above) did
**not** stop the doubling. `dump_scene_transforms()`'s output for
`Plane`/`Cube`/`Cube.001` correctly shows `transform=(baked into mesh)`
(`obj_transform=None`, `use_instancing=False`, as expected) -- but that
tool only shows Python-side `ExportedObject.transform` bookkeeping, not
what's actually baked into the LuxCore-side mesh vertices, so it can't
see this bug even when things look "normal" from that angle. Either the
v17 fix didn't fully close the path that triggers a second bake, or the
actual mechanism is different from the hypothesis (LuxCore's
`DefineMeshExt` compounding a `transformation` on redefinition) -- still
unconfirmed either way, no LuxCore source available locally to verify.

**Added a direct detector at the actual bake site**, `export/
mesh_converter.py`: a module-level `_baked_transform_names` set tracks
every LuxCore shape name defined with a non-`None` `transformation` (the
`use_instancing=False` case). First bake of a name prints `[diag] first
bake of '<name>' with a transformation ... translation: (...)`; any
**second** bake of the *same* name prints a loud `[diag] !!! RE-BAKE of
'<name>' ...`. This is unambiguous regardless of which code path
triggers it or whether the compounding hypothesis is right -- if `!!!
RE-BAKE` shows up in the log for `Cube000`/`Plane000`/etc., that IS the
bug, caught at the exact moment it happens, with the translation baked
each time for comparison.

**Also added `debug-helpers/singular_object_diag.py`** (companion to
`instance_transform_diag.py`, same RenderEngine-trick, no pyluxcore
needed): for a list of named objects (default `Plane`, `Cube`,
`Cube.001`), prints per frame their own `matrix_world` (should be
perfectly constant for a static object -- confirmed locally on the light
scene's `Plane`/`Plane.001`) and whether they show up in `depsgraph.
updates` with `is_updated_geometry=True` that frame -- the exact trigger
condition for `ObjectCache2.update()`'s "Geometry updates" loop, i.e.
whether the v17 fix's premise (this loop firing for these objects) still
holds after the fix.

**Next step:** user runs `persistent_data_gpu_test.py` (has pyluxcore,
picks up the new `mesh_converter.py` re-bake detector) across the
frame range that reproduces the bug, greps for `RE-BAKE`/`first bake`
for the affected object names -- this is the most direct evidence
possible short of LuxCore's own source. `singular_object_diag.py` can be
run alongside (no pyluxcore, cheap) to cross-check whether `is_updated_
geometry` still fires for these objects post-fix.

## 2026-09-23 (continued): confirmed the bug is inside LuxCore's live BeginSceneEdit/EndSceneEdit, not in this addon's export logic -- worked around it

Two results in sequence nailed this down completely:

1. `singular_object_diag.py` on the real scene: `is_updated_geometry` is
   **never** `True` for `Plane`/`Cube`/`Cube.001` across the bug's frame
   range -- v17's "Geometry updates" fix was real but was never the
   trigger for *this* bug.
2. `mesh_converter.py`'s new re-bake detector: only ever **one** `[diag]
   first bake ...]` per object, **zero** `[diag] !!! RE-BAKE`. So the
   mesh is genuinely only ever defined once, with the correct transform,
   confirmed by direct evidence at the exact call site that sends data to
   LuxCore. The Python/SDL side is clean.
3. **User confirmed the bug does NOT reproduce with a plain "Render
   Animation"** (F12 → Animation, no `persistent_data_gpu_test.py`, a
   fresh `RenderSession` per frame, no `BeginSceneEdit`/`EndSceneEdit`
   live-editing at all).

Put together: the exact same Python export, producing the exact same
single correct `DefineMeshExt(transformation=...)` call, is fine under a
fresh-session-per-frame render and corrupted under a live session reused
across frames via `BeginSceneEdit`/`EndSceneEdit`. This is a LuxCore-side
issue with how it handles an object whose mesh vertices already have a
transform baked in (`use_instancing=False`) sitting untouched in a live
session while an *unrelated* scene edit happens elsewhere (e.g. GN
instances appearing) -- not something fixable from this addon's export
code, and no LuxCore C++ source available locally to fix at the source.
Objects that instead send their transform as a separate
`scene.objects.<key>.transformation` property (`use_instancing=True`,
e.g. every GN dupli, since `dg_obj_instance.is_instance` alone already
forces this) were never affected -- that code path is the one viewport
rendering has exercised live-editing on for years.

**Workaround, implemented in `export/__init__.py` +
`export/caches/object_cache.py`:** new `Exporter.persistent_data_animation`
flag (default `False`, so a normal final render is completely
unaffected). When set, `_compute_use_instancing()` forces
`use_instancing=True` for *every* object, including ones that would
otherwise get the baked-transform treatment -- so nothing ever takes the
code path confirmed to corrupt under a live session. Set by
`persistent_data_gpu_test.py` right after creating the `Exporter`, before
`create_session()`, so it applies from the very first (full-export)
frame onward -- consistent with every later diff frame, avoiding a
mismatch between how an object was first exported and how it would be
looked up later.

**Not yet verified** -- next test should confirm the scale/origin
corruption for `Plane`/`Cube`/`Cube.001` (and the real production meshes)
is gone with this flag set.

## 2026-09-23 (continued): GN auto-detection (Bug C fix) REVERTED -- severe regression on a heavy scene

User tested against their real, heavy production scene (millions of
instances, many GN systems) with the fixes above applied. Confirmed a
severe regression: previously ~80s/frame export, now still not finished
after several minutes on the very first (full-export) frame. Also hit an
unrelated crash partway through (`ImageMap::MakeTx error: ... Format
"tiff" does not support separate display windows... OpenEXR is a format
that allows overscan textures`, in `create_session()` ->
`luxcore_scene.Parse(scene_props)`) -- user says there's no tiff texture
in the scene and this was already dealt with elsewhere in an earlier
addon version (v2/v3, not today's persistent-data-vN builds); not
investigated, likely unrelated to anything from today, only surfaced
because the export finally got far enough to reach texture processing
after minutes of grinding.

**Root cause of the regression:** the GN auto-detection added for "Bug C"
earlier today (`_has_geometry_nodes_modifier(parent)` in
`_wants_individual_tracking()`, `export/caches/object_cache.py`) pulls
*every* GN-sourced instance group out of hybrid batching, not just the
ones whose instance count actually changes. For a scene with many GN
systems and most of them perfectly stable across the animation, this
turns one fast batched `DuplicateObject()` C++ call per group into one
Python `_convert_obj()` call *per instance* -- fine for a handful of
trees, catastrophic at millions of instances.

**Reverted.** `_wants_individual_tracking()` is back to manual-only:
`obj.luxcore.always_reexport` on the object or its instancing parent, no
automatic detection. `_has_geometry_nodes_modifier()` removed (no longer
used anywhere). `DESC_ALWAYS_REEXPORT` tooltip updated back to describe
manual-only usage, explicitly noting that leaving it off keeps a stable
group on the faster batched path.

**Where this leaves Bug C:** the underlying correctness issue (`update()`
never re-batching, so a GN group's instance-count change is only
correctly picked up via `VisibilityCache` when the group goes to exactly
zero) is unfixed again -- back to needing `always_reexport` set by hand on
whichever specific GN systems actually have a varying instance count. No
better general fix was found today; the "automatic and cheap" version
turned out to not exist for a scene this large. If this needs revisiting,
the right direction is probably *dynamic* detection (start every group
batched, and only fall back to individual tracking for a specific group
once its instance count is actually observed to change across frames)
rather than a static, modifier-presence-based guess -- not implemented,
meaningfully more complex than what was tried today.

## 2026-09-23 (continued): separate, likely pre-existing bug -- a texture "file" property ends up as "0"

With the batching regression fixed (v20), export on the heavy scene
dropped from 487s to 44.7s, but then crashes: LuxCore's SDL log shows
`Creating TX image for file:  0`, followed by OpenImageIO errors ("Image
'0' does not exist") and a `RuntimeError: ImageMap::MakeTx error: ...
Format 'tiff' does not support separate display windows ...`. User: no
tiff texture in the scene, and thinks this was already fixed in an
earlier BlendLuxCore version (not one of today's persistent-data-vN
builds) -- not otherwise investigated, likely unrelated to any of
today's other work, just newly reachable now that export finishes fast
enough to get this far.

The failing texture is `scene.textures.2264513808416Color` (a
pointer-based SDL name, not human-readable) -- it never prints
`Probe ImageMap:`/`Image map detected - Reading:` before the crash the
way every other successful `...Color` texture in the log does, meaning
its `.file` property itself is bad, not a downstream MakeTx quirk on a
valid path.

**Added a diagnostic**, `nodes/textures/imagemap.py`
(`LuxCoreNodeTexImagemap.sub_export()`): right after computing `filepath`
via `ImageExporter.export()`, checks if it's falsy or not a string, and
if so prints `[diag] !!! imagemap.py sub_export: bad filepath=... node=...
tree=... image=... source=...` -- this is the one place a `.file`
property gets set from an Imagemap texture node (checked, only two
places in the whole addon set a `"file"` SDL property at all; the other,
`nodes/textures/fresnel.py`, is for a manually-typed NK data file path,
unrelated -- not touched).

**Not yet verified** -- next test should show which node/material/image
this points at.

## 2026-09-23 (continued): "file: 0" / MakeTx crash -- ROOT CAUSE FOUND AND FIXED

Root cause was in `export/image.py`, `ImageExporter._save_to_temp_file()`,
introduced in the refactor that added per-tile (UDIM) packed-file support
somewhere between v6 (confirmed working) and v20 (confirmed broken;
identical to v4 and v6 for this file, ruling those two out as the
regression point):

1. On a **cache hit** (the same packed image's `key` -- derived from
   `packed.filepath`, the image's *original* path before packing --
   already exported once this session), the loop did `continue` without
   appending the cached temp path to `result`. Two datablocks with the
   same original `packed.filepath` (e.g. `Concrete002_2K_Color.jpg` and
   its Blender-renamed duplicate `...jpg.002`, both packed) collide on
   this key: the first succeeds and populates `temp_images[key]`, the
   second hits the cache branch and silently contributes nothing.
2. On an actual `packed.save()` failure (`RuntimeError`), the code
   printed a `[BLC] Warning: could not save image.` line and `continue`d
   instead of raising.
3. Either way, `_save_to_temp_file()` returned `None` instead of raising.
   `None` then became `scene.textures.<key>.file`'s value, which
   pyluxcore/LuxCore apparently coerces to the string `"0"` when read
   back -- explaining the exact `Creating TX image for file:  0` /
   `Image "0" does not exist` / `MakeTx ... tiff ... overscan` crash
   chain. The "tiff/overscan" error text was a red herring the whole
   time -- there is no tiff texture in the scene, `"0"` isn't a real
   filename at all.

**Fix** (`export/image.py`, both the cache-hit branch and the final
`return`): cache hits now `result.append(temp_image.name)` before
`continue`; if `result` ends up empty after the loop, raise `OSError`
instead of returning `None`. The `OSError` is caught by
`imagemap.py sub_export()`'s existing `except OSError` handler, which
falls back to the magenta `[1, 0, 1]` "missing image" color + a visible
`LuxCoreErrorLog` warning -- exactly the old (v6) failure mode, instead
of corrupting the scene properties and crashing deep inside LuxCore's
C++ MakeTx.

Two more fixes bundled in the same pass (found while chasing this):
- `export/cycles_node_reader.py`: ported upstream commit `6d4e5a38`
  ("Fix #1091: Prevent legacy Principled BSDF from rendering as mesh
  lights") -- unconditional `Emission Strength` read was forcing
  `emission=1` on every Cycles-imported material, notably ones appended/
  linked without LuxCore nodes (`is_asset_without_lux_mat` path in
  `export/material.py`). Now only sets `emission` when the socket is
  actually linked or has a nonzero default.
- `handlers/depsgraph_update_post.py`: cherry-picked commit `2674ae43`
  from branch `fix/linked-material-node-rename` (was not on this
  branch) -- guards the node-tree-rename-on-material-rename logic
  against linked (`mat.library`/`node_tree.library`) materials, which
  are read-only and raised a console-spamming `AttributeError` on every
  depsgraph update whenever a linked material was present. Cosmetic
  (console only), did not affect rendering, but noisy enough to hide
  real warnings in the log while debugging the above.

Packaged as `BlendLuxCore_ConditionalBatching6_persistent-data_v22.zip`
(v20 base + these three files).

## 2026-09-23 (continued): severe slowdown on instance-visibility-change frames -- diagnosed, not yet fixed

Separate from the crash above. Symptom: a frame where some instance
group's visibility toggles (e.g. new models appearing at frame 5, others
at frame 101 in the production scene) takes minutes instead of seconds
to update, and drags *unrelated, never-changing* large instance groups
(paving stones, roof tiles) into the same slowdown.

**Mechanism (confirmed by reading `object_cache.py`):**
`ObjectCache2.first_run()` groups instances by
`batch_key = (mesh_pointer, camerainvisible, is_holdout)` and issues one
bulk `luxcore_scene.DuplicateObject()` C++ call per group -- fast,
independent of instance count. `ObjectCache2.update()` has **no
equivalent bulk path**. Its "Geometry updates" loop adds a mesh's
`mesh_key` to `redefine_objs_with_these_mesh_keys` whenever
`depsgraph.updates` reports `is_updated_geometry=True` for *any* object
using that mesh -- including via the already-documented "depsgraph
reports too broad a sweep" behavior (see the `ROOT CAUSE FOUND AND
FIXED` section above: an unrelated GN system producing new instances can
spuriously flag other objects' geometry as updated on that same frame).
Once a `mesh_key` is in that set, the `object_instances` loop
(`update()`, ~line 1030) routes **every single instance** of that mesh
through the `else` branch -- a full, individual, Python-side
`_convert_obj()` call per instance -- instead of the cheap
transform-only update. For a group with thousands of instances (roof
tiles, paving stones), this is the "80s/frame -> still not done after
several minutes" regression already described in the
`_wants_individual_tracking()` docstring, just triggered a different way
(via `redefine_objs_with_these_mesh_keys`, not the reverted automatic
GN-modifier detection).

**Confirmed not fork-specific**: upstream commit `c40f585b` ("A6-II:
persistent LuxCore scene reuse for final renders", a *different*,
whole-Scene-object cache for repeated F12 on an unchanged scene, not
frame-by-frame animation) has the identical limitation --
`persistent_scene.classify()` bails to a full `first_run()` rebuild for
any object with `duplicate_count > 0`, and its own design doc lists
"instancer re-flush" as an unimplemented roadmap item. No ready-made
fix to port from there; useful as confirmation this is a genuine,
unsolved BlendLuxCore-wide gap, plus a couple of reusable patterns
(`.original` vs evaluated-datablock pointer handling in
`depsgraph_update_post`, `UpdateObjectTransformation` semantics).

**Decision for now**: user confirmed the instance hiding/reveal timing
is a pure performance optimization, not a required visual effect (the
final image does not depend on objects being invisible before their
reveal frame). Leaving visibility toggling **as-is, unfixed** for the
moment -- testing whether simply disabling the hide/reveal scheduling
entirely (everything visible from frame 1) is an acceptable trade-off
on its own (fast updates always, at the cost of more geometry for
LuxCore to trace/GI through and higher VRAM).

**Proper fix, deferred**: a "double batching" split -- a static pool
(batched once in `first_run()`, made immune to `update()`'s
`redefine_objs_with_these_mesh_keys` sweep entirely, so spurious or even
real depsgraph geometry flags on it are ignored) and a dynamic pool
(objects/groups that legitimately change, tracked individually from
`first_run()` onward the same way `always_reexport` already works
today). Discussed two ways to decide pool membership:
- Manual per-object/collection flag (cheap to implement, symmetric to
  the existing `always_reexport`, but silently drops real changes on
  anything the user forgets to flag as dynamic).
- Explicit per-frame change declaration from the calling script (the
  render-loop script, e.g. `persistent_data_gpu_test.py`, already knows
  exactly which objects/collections it toggles on which frame --
  `update()` would take that as an optional override and skip reading
  `depsgraph.updates` for redefine purposes entirely when given).
  Preferred direction: strictly more accurate than depsgraph inference
  for a scripted render pipeline where the caller already knows the
  answer, and has none of the "forgot to flag it" failure mode of the
  manual-flag approach. Not implemented yet.

## 2026-09-23 (continued): confirmed update() is slower than first_run() on the real scene; persistent-scene-reuse (v1) abandoned; v23 camera-only ships instead

Two things converged today that changed direction:

1. **persistent_scene_v1 experiment (module-level Exporter/Scene reuse
   across SEPARATE render() calls, fresh RenderConfig/RenderSession every
   frame, safe + automatic Blender compositor/denoise/save -- see the
   now-superseded `export/caches/persistent_scene_v1.py` /
   `render_persistent_scene_v1_test.py`, both still present but not the
   direction going forward) does not help. Root cause: constructing
   `pyluxcore.RenderSession(renderconfig)` itself is expensive (BVH
   build, texture/GPU upload, accelerator build) regardless of whether
   the *Python-side* Scene export was skipped -- this was already
   written down in `persistent_data_gpu_test.py`'s own "v8 postmortem"
   (a pure camera move still printed a full "Compile Geometry / ...
   Building Optix accelerator" sequence when the session was recreated
   every frame) but got under-weighted when persistent_scene_v1 was
   designed. User confirmed empirically: export time drops but total
   time per frame does not.

2. **`ObjectCache2.update()` is not just occasionally slow on redefine-
   sweep frames -- it is unconditionally slower than a full
   `first_run()` on the user's production scene**, confirmed by direct
   testing ("w moim przypadku update nie jest tani, wszystko jest
   przebudowywane znacznie dłużej niż firstrun"), independent of
   disabling the instance appear/disappear animation entirely. Full
   mechanism, worked out together with the user: `first_run()`'s hybrid
   batching registers only ONE `exported_objects` entry per batched
   group (the "base"); every other member instance (could be thousands)
   is never individually tracked. `update()`'s `object_instances` loop
   checks `obj_key in self.exported_objects` per instance -- for a
   non-base batch member this is *always* False, so it always falls to
   the expensive per-instance `_convert_obj()` path, regardless of
   whether that instance's position ever changed. And this loop runs
   whenever `Change.OBJECT` fires for *any* reason anywhere in the scene
   (`get_changes()`/`_update_scene()`), not scoped to the group that
   actually changed. Net effect: on a heavily hybrid-batched scene,
   virtually any object-level depsgraph activity on any frame forces
   full re-conversion of every batched instance in the whole scene, one
   Python call at a time -- slower than `first_run()`, which does the
   same work as one bulk C++ `DuplicateObject()` call per group.
   Real fix identified but NOT YET IMPLEMENTED: give `update()`'s
   `object_instances` loop its own "already know this instance, its
   group hasn't changed, skip entirely" recognition (a
   `ObjectCache2.batch_membership` set populated in `first_run()`), and,
   separately, bulk-rebatch (reuse `duplicate_instances()`) rather than
   individually `_convert_obj()` groups that *did* change. Scoped,
   estimated safe, but shelved for now in favor of the simpler v23 path
   below while the user regroups after several inconclusive version
   tests.

**v23 (`BlendLuxCore_ConditionalBatching6_persistent-data_v23.zip`,
`export/__init__.py` `Exporter.update_camera_only()`,
`render_v23_camera_only_test.py`)**: sidesteps both problems above by
not calling `ObjectCache2.update()` -- or reading `depsgraph.updates` at
all -- after the first frame. `first_run()` runs once; every subsequent
frame only re-Parses the camera onto the live session
(BeginSceneEdit/EndSceneEdit), confirmed cheap by the same "v8
postmortem" evidence (viewport render already does exactly this,
continuously, with no recompile). Explicit, accepted trade-off: no
object/material/light/visibility animation is reflected after frame 1 --
only the camera moves. Appropriate only for sequences where that is
true of the actual shot; not a general-purpose fix for the
`update()`/batch-membership problem above, which still needs the real
fix whenever a sequence needs both camera *and* object motion.

## 2026-09-23 (continued): "v8" limitation was an implementation bug, not a LuxCore limitation -- live session confirmed to survive separate, native render() calls (GUI-driven)

Re-opened the question the "v8 postmortem" (debug-helpers/
persistent_data_gpu_test.py) seemed to close: that a live
`pyluxcore.RenderSession` cannot survive across SEPARATE Blender
`render()` invocations (i.e. real "Render Animation"/Ctrl+F12, where
Blender calls `render()` once per frame itself), only within one
continuous internal loop. Built a GUI-driven test
(`live_session_gui_test_addon.py`, run once from Scripting to register,
then driven entirely by the native Render Animation operator) using a
**module-level** cache for `(exporter, session)` keyed by
`(scene_ptr, view_layer_name)` -- not a `RenderEngine` instance
attribute, which is the likely reason "v8" failed: Blender may not
guarantee the same Python `RenderEngine` instance persists across an
animation render's separate `render()` calls, so a session cached on
`self` could reference a stale/dead instance by the next frame.

**Result: it works.** `session.BeginSceneEdit()`/`EndSceneEdit()` on a
session created in a *previous, already-returned* `render()` call
succeeds (`Exporter.update_camera_only()`, same method v23 uses). The
"v8" crash (`BeginSceneEdit(): RuntimeError: invalid argument`) was
almost certainly the instance-attribute caching bug above, not a hard
LuxCore-side limitation.

Getting live *display* working took several more fixes, all the same
shape (the engine object this script builds didn't fully match what
`FrameBufferFinal.draw()`/`engine/final.py` assume a "LuxCoreRenderEngine"
provides) -- worth recording since they'll bite again if this is
ported into the real engine or reused elsewhere:
- `utils_view_layer.State.active_view_layer` must be set (module-level
  global, read by `draw()` to pick which result layer to deliver into;
  `engine/final.py`'s `render()` sets it, this script initially didn't).
- `GetOutputFloat(RGB_IMAGEPIPELINE, ...)` raised `RuntimeError: Film
  Output not available: 3` on a session reused via BeginSceneEdit/
  EndSceneEdit + `Film.Clear()` -- `SaveOutput()` with the *same* output
  type worked fine (it computes the imagepipeline on demand;
  `GetOutputFloat()` only reads an already-computed buffer). Fixed by
  calling `film.AsyncExecuteImagePipeline(0)` and waiting for
  `HasDoneAsyncExecuteImagePipeline()` before `GetOutputFloat()` -- the
  same pattern `_refresh_denoiser()` already uses for the *denoiser's*
  imagepipeline index, just applied to the main combined pass (index 0)
  too.
- `draw()` reads `engine.exporter` (for AOV/lightgroup import,
  `engine.exporter.lightgroup_cache`) and `engine.has_denoiser()` /
  `engine.DENOISED_OUTPUT_NAME` / `engine.aov_imagepipelines` (from
  `LuxCoreRenderEngine`, `engine/base.py`) -- a bare
  `bpy.types.RenderEngine` subclass has none of this. **Real fix, not
  another one-off patch:** make the test engine inherit from
  `LuxCoreRenderEngine` itself instead of `bpy.types.RenderEngine`,
  overriding only `render()`. Gets `reset()`, `has_denoiser()`,
  `update_render_passes()`, `__del__`, etc. for free, correctly, in one
  step -- this is what actually stopped the whack-a-mole `AttributeError`
  sequence, not another individual attribute fix.

**Confirmed working end to end** (GUI-driven, camera-only, same
trade-off as v23: only the camera re-parses after frame 1). This means
a native "Render Animation" workflow with real persistent-data speedup
AND automatic Blender compositor/denoise/save (never modified --
`begin_result()`/`end_result()` happen exactly once per normal,
separate `render()` call, so Blender's own postprocess machinery never
needs to know anything unusual happened internally) is achievable, not
just the single-continuous-call script workaround.

**Follow-up fix, same day: `engine.aov_imagepipelines`/`engine.exporter` are
INSTANCE attributes, wiped every frame.** `LuxCoreRenderEngine.__init__()`
itself calls `self.reset()` (sets `self.aov_imagepipelines = {}`,
`self.exporter = None`, etc.) -- Blender constructing a *fresh* Python
engine instance per separate `render()` call (near-certain, same
underlying fact that made the module-level `_cache` necessary for
`exporter`/`session` in the first place) means these two attributes get
silently wiped before `render()` even runs, regardless of anything our
own code does. Symptom chain: frame 1 (FULL_EXPORT, `aovs.convert()`
runs fresh) had a working DENOISED pass; every frame after (
`update_camera_only()` never touches `aov_imagepipelines`) showed
`has_denoiser()=False` and a black DENOISED pass, because the dict a
fresh instance started with was empty and nothing repopulated it.
**Fix:** cache `dict(self.aov_imagepipelines)` in the module-level
`_cache` entry alongside `exporter`/`session` at every FULL_EXPORT, and
restore it onto `self.aov_imagepipelines` at the top of every
`REUSED_LIVE_SESSION` frame, mirroring the existing `self.exporter =
exporter` pattern.

**This also explains the ~115s stall at frame 101 reported earlier the
same day** (had to be manually cancelled, `render cancelled -- stopping
cached session(s)` in the log): `_refresh_denoiser()` (`draw/final.py`)
calls `session.GetFilm().AsyncExecuteImagePipeline(index)` then `while
not session.GetFilm().HasDoneAsyncExecuteImagePipeline(): sleep(1)` --
**no timeout**. With `has_denoiser()` returning True from a stale/
partial intermediate state (mid-way through fixing the above) but the
imagepipeline `index` not matching what the reused session's Scene
actually has configured, that wait can never resolve -- hangs
indefinitely until the user cancels. Not a MIPMAPMEM/resize-policy issue
(that step logged 0 image maps to process, ~4.8s of pure overhead,
unrelated) and not the already-known `update()`/batch-membership
slowness (this test is camera-only, `update()` is never called at all).

**Net state at end of day (2026-09-23):** `live_session_gui_test_addon.py`
+ `LUXCORE_LIVE_SESSION_TEST` is a working, GUI-driven (Ctrl+F12,
native "Render Animation"), camera-only persistent-data render path with
correct live Render Result display, AOVs, and denoiser, confirmed on
both a small test scene and briefly on the real production scene
(HavenHuis). Still isolated from the real `LUXCORE` engine -- not
ported into `engine/final.py` yet, and deliberately so (see "should this
become the official mechanism" discussion: camera-only would silently
freeze all object/material/visibility animation if wired to the native
`use_persistent_data` checkbox for the real engine today -- needs the
`update()`/batch-membership fix, and more soak testing, first).

**Not yet done / open follow-ups:**
- Only camera-only tested end-to-end; extending to also cover
  `always_reexport`-flagged objects (the abandoned v24 attempt) using
  this now-confirmed-safe live-session-across-calls foundation is worth
  retrying -- the crash that killed that attempt was a *different* bug
  (batch obj_key mismatch on delete, see the v24 section above), not
  this session-lifecycle question.
- Not yet ported into the real `engine/final.py`/`LUXCORE` engine --
  still an isolated, disposable test file/engine
  (`LUXCORE_LIVE_SESSION_TEST`, not `LUXCORE`).
- Still has the separate, already-diagnosed `update()`/batch-membership
  slowness for any frame where an instance group's mesh_key needs
  redefining (unrelated to and not fixed by this session-lifecycle
  work) -- relevant again once this is extended past camera-only.

## 2026-09-24: denoiser-refresh UI crash (EXCEPTION_ACCESS_VIOLATION) --
root-caused to a Panel.poll() monkeypatch, NOT to the session-reuse code

Continuing the GUI test line (`live_session_gui_test_addon.py`/v1):
tried adding a manual "Refresh" button for the Denoiser, reachable
while Ctrl+F12 is running on the `LUXCORE_LIVE_SESSION_TEST` engine, to
check whether triggering the denoiser mid-render on a session reused
live across separate frames shows the CURRENT frame or a stale/ghosted
one (same family of question as Bug B, earlier in this file, but for
this session-lifecycle path specifically).

`ui/render/denoiser.py`'s panels hard-check
`context.scene.render.engine == "LUXCORE"` in their own `poll()`, so
they never show for a different `bl_idname`. First attempt (`v2`):
monkeypatched `poll()` on the ALREADY-REGISTERED
`LUXCORE_RENDER_PT_denoiser`/`..._bcd_advanced` classes at runtime
(`cls.poll = classmethod(...)`) to also accept the test engine.
**Result: real `EXCEPTION_ACCESS_VIOLATION` crashes in `python313.dll`**
-- reproduced on completely unrelated follow-up actions (moving the
mouse, selecting the engine, switching an editor to Image Editor),
always crashing later rather than at the patch call itself (consistent
with memory corruption from the patch, not from whatever was clicked
afterwards).

**Isolated with a clean A/B test** (each variant = v1 baseline + exactly
one delta, tested on a fresh Blender restart each time):
- `v2a` (only: override `view_update()`/`view_draw()` as no-ops instead
  of inheriting `LuxCoreRenderEngine`'s real viewport-render
  implementation -- `bl_use_preview=False` does NOT block the 3D
  viewport's "Rendered" shading mode from calling these) -- **no
  crash.**
- `v2b` (only: the `poll()` monkeypatch on the existing, already-
  registered panel classes) -- **crashed**, confirming the monkeypatch
  itself as the cause, not the viewport-render path.

**Fix, not a workaround:** don't mutate the existing, already-registered
Panel class at all. `v2c` instead defines a NEW subclass of
`LUXCORE_RENDER_PT_denoiser` (own `bl_idname`, inherits `draw()`/
`draw_header()` unchanged, only `poll()` differs) and registers it via
the normal `bpy.utils.register_class()` path -- the same "register a
fresh class" pattern already proven safe for the RenderEngine class
itself. The original panel is never touched. No crash, button reachable
and clickable during Ctrl+F12.

**Standalone test scripts from this line, all on the network share**
(`live_session_gui_test_addon_v2*.py`): v2 (crashes, kept only as the
documented negative result), v2a/v2b (isolation-only, safe to delete),
v2c (safe, working, denoiser-refresh-during-render test harness).

## 2026-09-24 (continued): integrated into the REAL "LUXCORE" engine
instead of a second test engine -- v25, built on the user's v24 backup

Question raised mid-session: why maintain a whole second RenderEngine
class at all, when several of the day's problems (denoiser panel
invisible, needing the `view_update`/`view_draw` guard) exist *only*
because of that separation? Decided to fold persistent-data-animation
directly into `engine/final.py`'s real `render_final()` path instead,
gated by a new, dedicated (NOT Blender's native `scene.render.
use_persistent_data` -- deliberately a separate LuxCore-only flag, to
avoid any assumption tied to how Cycles interprets that shared one)
checkbox: `scene.luxcore.config.use_persistent_data_animation`, off by
default.

**Base:** the user's own backup of
`BlendLuxCore_ConditionalBatching6_persistent-data_v24.zip` (network
share, also mirrored in a `Dziala` ["Works"] folder) -- NOT the current
git branch tip, which has since diverged further. `update_camera_only()`
already existed in that build (v23-era) and needed no changes.

**Implementation** (`engine/final.py`): `_render_layer()` branches at
the very top -- `if scene.luxcore.config.use_persistent_data_animation
and engine.is_animation:` (the animation-vs-single-frame flag Blender's
own `RenderEngine.is_animation` already provides, also already used
elsewhere in this same file) delegates to a new
`_render_layer_persistent_data_animation()`; otherwise the existing code
is untouched, byte-for-byte, below that branch. The new function is the
same module-level-cache/`update_camera_only()`/Python-side-halt design
already proven in `live_session_gui_test_addon.py`, just adapted to run
inside the real engine:
- Module-level `_persistent_data_cache` keyed by `(scene_ptr,
  view_layer_name)` (an engine INSTANCE attribute would not survive --
  Blender constructs a fresh Python `RenderEngine` instance per separate
  `render()` call during an animation, same fact the GUI tests already
  established).
- `scene.luxcore.halt.enable` is flipped off only for the duration of
  the frame-1 `create_session()` call and restored immediately after
  (narrower than v1's permanent-until-manually-re-enabled disable) --
  `_check_halt_conditions()` (called earlier in `render()`) still
  requires a real halt condition configured, same as any other
  animation render.
- `aov_imagepipelines` cached and restored across frames the same way
  v1's GUI test does, for the same reason (wiped by `reset()` inside
  every fresh engine instance's `__init__`).
- New `handlers/render_complete.py`, registered for both
  `render_complete` and `render_cancel`, calls
  `engine.final.stop_persistent_data_sessions()` -- stops and drops any
  cached session once the WHOLE animation finishes or is cancelled, not
  per frame (confirmed correct timing already, empirically, by the
  GUI-test line's identical handler pattern).
- New UI: `ui/render/tools.py`'s `LUXCORE_RENDER_PT_persistent_data_animation`,
  a child panel of the existing "LuxCore Tools" panel (both
  `DEFAULT_CLOSED`, near the bottom of Render Properties) -- checkbox in
  the panel header, same pattern as the neighboring "LuxCore Filesaver"
  panel. Since this is the real "LUXCORE" engine, the Denoiser panel
  needs no changes at all and just works.

Packaged (flat-zip of the modified extracted tree, no Blender available
to run the usual `extension build` command) as
`BlendLuxCore_ConditionalBatching6_persistent-data_v25_checkbox.zip`.

**CONFIRMED WORKING by user testing:** with the checkbox enabled, a new
frame's render initiates almost immediately (session already in memory,
no full re-export), and clicking the Denoiser "Refresh" button on that
fresh frame correctly denoises the CURRENT frame's data (max ~10s),
*not* a stale/ghosted one. This directly answers the open question this
whole day's investigation was chasing (does a denoiser refresh on a
live, reused session show the right frame) -- yes, when done through the
real engine's own render loop and panels, with no monkeypatching
involved.

**Not yet tested / open:** only camera motion confirmed end-to-end this
way so far -- object/material/light/visibility animation is still
frozen after frame 1 (documented, expected limitation, not a bug); the
`update()`/batch-membership slowness for scenes needing more than
camera motion is still unfixed and unrelated to this integration.

**Fixed same day:** the loop above never called
`utils_render.update_status_msg()`, so the Statistics panel (Render
Time, Samples, Samples/sec, Rays/sample) never updated during a
persistent-data-animation render, even though the render itself worked
-- added, throttled the same way the normal `_render_layer()` already
does (`_stat_refresh_interval()`), while still checking the raw pass
count every loop tick (~10/s, cheap) so Python-side halt timing stays
accurate.

**Refactored same day:** all persistent-data-animation code moved out
of `engine/final.py` into a new `engine/persistent_data_animation.py`
(`render_layer()`, `stop_sessions()`, plus its own small private
helpers -- `_stop_requested`/`_stat_refresh_interval` duplicated
locally rather than imported back from `final.py`, to avoid a circular
import). `engine/final.py`'s own diff against the pristine v24 original
is now just: one import, one entry in the reload list, and a 4-line
branch at the top of `_render_layer()` -- everything else in that file
is byte-for-byte unchanged. `handlers/render_complete.py` updated to
call `persistent_data_animation.stop_sessions()`.

**Extended same day:** swapped `update_camera_only()` for
`Exporter.update_flagged_only()` (v24-era, written but never tested --
see the "abandoned v24 attempt" section above) on the REUSED_LIVE_SESSION
branch in `engine/persistent_data_animation.py`. This is a superset
(camera + any obj.luxcore.always_reexport-flagged object/instancing-
parent, re-batched in bulk).

**Confirmed by user testing:** works for a plain, non-instanced,
non-GN moving mesh (a Cube with animated location) -- position updates
correctly every frame.

**Bug found and fixed same day (disappearance never handled):** an
always_reexport-flagged object that becomes INVISIBLE was never
cleaned up at all. Root cause: `update_flagged_only()`'s main loop only
ever visits currently-visible instances (`if not
utils.is_instance_visible(...): continue`) -- there was no code path
at all for "this flagged group used to be exported, now it's not in
this frame's visible set". Symptom, confirmed by testing a
present -> absent -> present sequence: the object stayed visible in
the render for the ENTIRE sequence, including the frame where it
should have disappeared -- only the pre-existing "drop old version
before recreating" step (meant for reappearance) incidentally deleted
the stale copy, one frame late, when it reappeared. A simpler
absent -> present (2-frame) test had accidentally not exercised this
bug at all: frame 1 is always a FULL_EXPORT via first_run(), which
correctly never exports an invisible object in the first place, so
there was nothing stale to clean up in that specific test.

**Fix:** new `Exporter.flagged_reexport_keys` (set, initialized in
`__init__`) tracks which obj_keys were exported as flagged groups by
the PREVIOUS call. Each call now also computes `seen_obj_keys` (what
was actually visible+flagged this call) and explicitly deletes
anything in `flagged_reexport_keys - seen_obj_keys` -- the vanished
groups -- before updating the tracked set for next time. Not yet
re-verified by user testing after this fix.

**Separately confirmed by testing (still open, not a new bug):**
flagging a GN "Duplicate Collection" parent whose children span
multiple distinct mesh types cascades the flag to ALL of them (each
becomes its own batch_key), so the unconditional per-frame re-batch
touches far more than the user intended -- confirmed harmless
(matched delete/recreate pairs, no leaks) but potentially a lot of
work per frame on a scene with many mesh variants under one flagged
parent. A ground-truth "did the flagged group's actual instance
list change since last frame" fingerprint check (skip the re-batch
entirely if unchanged, same technique CameraCache.diff() already uses
for the camera) was discussed as the principled fix for this -- not
yet implemented.

**Second disappearance bug found and fixed same day (the first fix was
incomplete):** tested with a real GN setup (object 'GN' hosting a
Geometry-Nodes-driven instance of 'AZR_Brzoza_lekarz_High', visibility
controlled by a `GeometryNodeInputSceneTime` node feeding a
comparison -- not a keyframe, not a Switch node, purely time-driven
inside the node tree, confirmed by the user that the scatter points
themselves never move, only this value). Confirmed via a name-printing
diagnostic added to update_flagged_only() that `depsgraph.
object_instances` DOES correctly stop yielding the tree on the
"should be hidden" frame -- ruling out any Scene-Time/depsgraph
detection problem. But the disappearance still didn't happen visually.
Root cause: `self.flagged_reexport_keys` (the fix above) starts EMPTY
in `Exporter.__init__()` and is only ever updated by
`update_flagged_only()` itself -- but the tree was first exported by
`first_run()` (inside create_session(), frame 1's FULL_EXPORT), not by
update_flagged_only(). So update_flagged_only()'s FIRST call this
session (the very frame the tree disappears on) had `flagged_reexport_
keys` still empty -- no memory that first_run() had exported it --
so `vanished_keys = flagged_reexport_keys - seen_obj_keys` was always
empty on that call regardless of what actually vanished. The tree
correctly dropped out of `seen_names` (proving the depsgraph read was
fine) but nothing told LuxCore to remove the stale copy from frame 1.

**Fix (v1, INCOMPLETE, since replaced):** new
`Exporter.compute_flagged_visible_keys(depsgraph)` (read-only, same
flagged+type+visibility filter as update_flagged_only()'s loop, no
LuxCore calls) called right after `create_session()` succeeds in
`engine/persistent_data_animation.py`'s `_create_session()`, seeding
`exporter.flagged_reexport_keys` with whatever first_run() actually
exported for flagged groups.

**Confirmed by user testing on a real production scene (a flagged
Geometry-Nodes forest, 'AZR_Brzoza_lekarz_High', ~110 then reduced to
4 instances for easier debugging): v1 did NOT work.** A
present -> absent -> present test never actually removed anything on
the "absent" frame -- all instances stayed visible on every frame.
Root cause: `compute_flagged_visible_keys()` re-derives each group's
obj_key with a SEPARATE walk over `depsgraph.object_instances`, called
AFTER `create_session()` (i.e. after first_run()'s OWN, separate walk
already ran) -- guessing which instance was "first" for a given
batch_key. Confirmed via a name+count diagnostic print added to it
(`[compute_flagged_visible_keys] seeded [(name, count), ...]`) that the
counting itself was correct (found the real 110/4 instances), but the
"removed N vanished group(s)" print was misleading -- it counts
`len(vanished_keys)` (the set-difference size), not whether
`exported_objects.pop(obj_key, None)` actually found anything. It
never did, on repeated tests -- the guessed key never matched what
first_run() had actually used. Not random (initially mis-diagnosed as
enumeration-order flakiness) -- systematic: a second, independent walk
over depsgraph.object_instances has no reason to agree with the first
one's notion of "which instance is the batch's base" once there's more
than one candidate.

**Fix (v2, the actual fix):** eliminated the second walk entirely.
`Exporter.compute_flagged_visible_keys()` removed. Instead:
- New shared `_is_always_reexport_flagged(obj, dg_obj_instance)` in
  `object_cache.py` (used by `_wants_individual_tracking()`,
  `first_run()`'s new recording step below, and
  `update_flagged_only()` -- one flag-check implementation instead of
  three copies).
- `ObjectCache2.first_run()` now records the key itself, inline, at
  the exact spot it creates a new batch (`instances[batch_key] =
  Duplis(exported_obj)`) -- `if exporter.persistent_data_animation and
  _is_always_reexport_flagged(obj, dg_obj_instance):
  exporter.flagged_reexport_keys.add(utils.make_key_from_instance(
  dg_obj_instance))`, using the SAME `dg_obj_instance` that was just
  used to create the entry. Zero possibility of disagreement -- same
  loop, same iteration, same object.
- `update_flagged_only()`'s own loop already recorded keys this way
  (unchanged); its print now also reports `actually removed` (from
  `pop()` finding something) separately from the vanished-key count,
  so a future silent-miss can't hide behind a misleading "removed N"
  message again.

Not yet re-verified by user testing (packaged as
`BlendLuxCore_ConditionalBatching6_persistent-data_v27_key_at_source.zip`).

**Confirmed by testing: v27 also did not work -- "0 vanished (actually
removed: 0)" on the disappear frame, AND zero `_convert_mesh_obj` diag
lines for the flagged tree on frame 1's FULL_EXPORT at all** (only an
unrelated 'Cube.018'). So `flagged_reexport_keys` was never populated
in the first place this time -- first_run() never called
`_convert_obj()` for this object on frame 1, despite it being visible
and correctly counted as present by the (now-removed) v1 diagnostic in
an earlier test. 0/1/0 confirmed still working exactly as before
(unaffected, since that path is created by update_flagged_only() only).

**Real root cause found, pre-existing and unrelated to any of today's
own changes:** `ObjectCache2._convert_obj()` (`object_cache.py`) has
`if dg_obj_instance.show_self: ... else: return None` wrapping its
entire mesh-export body. `show_self` is per-DepsgraphObjectInstance,
not per-object -- for a nested "Duplicate Collection" GN setup, the
SAME logical tree can yield two entries sharing one batch_key (the
"outer" Collection-Info placeholder AND the "nested" real instance,
see the 2026-09-23 "nested Collection-Info double-entry" section
above), and only one of them has `show_self=True`. `first_run()`'s
hybrid-batching loop only ever gives the FIRST instance of a new
batch_key ONE chance (`except KeyError: exported_obj =
self._convert_obj(...); if exported_obj: ... else: instances[batch_
key] = None`) -- if that first instance happens to be the non-showing
placeholder, `_convert_obj()` returns None (silently, no error, no
diagnostic), and `instances[batch_key] = None` permanently poisons the
ENTIRE batch for the rest of this call: every other, actually-showing
instance of the same mesh just hits `if duplis:` on an already-`None`
entry and does nothing. Whether the placeholder or the real instance
is encountered first depends on Blender's enumeration order for that
specific frame's evaluated depsgraph -- explaining why this frame
(present-from-frame-1) failed while 0/1/0 (created later, by
update_flagged_only(), on a different frame with presumably different
ordering) worked. This is a real, general bug in the shared hybrid-
batching code, not specific to always_reexport or persistent-data-
animation -- it could in principle silently drop ANY hybrid-batched
group in ANY render (normal or persistent-data-animation) if the
"wrong" instance happens to enumerate first, though apparently rare
enough in practice not to have been noticed before this specific
back-and-forth toggling test surfaced it.

**Fix:** in `first_run()`'s hybrid-batching branch, skip (`continue`)
any instance with `show_self=False` OUTRIGHT, before touching
`instances[batch_key]` at all -- so a non-showing placeholder never
gets the chance to poison the batch; the next, actually-showing
instance for the same batch_key still gets a fresh, un-poisoned
attempt. Packaged as
`BlendLuxCore_ConditionalBatching6_persistent-data_v28_show_self_fix.zip`.

**Confirmed by testing: v28 also did not fix it** -- still zero
`_convert_mesh_obj` diag lines for the flagged tree on frame 1, all 4
instances confirmed `show_self=True` via a targeted per-instance diag
print (v29). A second targeted diag (v30, direct dump of
`exported_objects` keys right after `create_session()`) confirmed the
object was genuinely never exported at all (`matching 'Brzoza': []`,
`flagged_reexport_keys: set()`) -- and, critically, printed
`entered batching block ... already_in_instances=True` on the very
FIRST of the 4 flagged instances processed.

**Actual root cause, found by the user:** `batch_key` is computed
PURELY from mesh identity (`obj.original.as_pointer()`,
camerainvisible, is_holdout) -- it has no notion of *which instancing
source* (which GN modifier, or a plain Collection Instance) produced a
given instance. The user's scene has the SAME Collection (containing
'AZR_Brzoza_lekarz_High') referenced by more than one source -- e.g. a
plain Collection Instance object alongside the GN-driven duplicator, or
two independent GN groups sharing one Collection. `already_in_instances
=True` on the first flagged instance proved some OTHER, unflagged
source's instance of the same mesh had already claimed that batch_key
earlier in the same first_run() pass, becoming the batch's base --
so the flagged instances just got silently appended as extra duplicate
matrices onto an already-existing, unflagged batch. That batch's key
never entered `flagged_reexport_keys` (its base was never flagged), so
`update_flagged_only()` never touched it at all -- looked identical to
"the flag has no effect". Confirmed symmetrically by the user with two
different setups: (1) a visible (unflagged) plain Collection Instance
sharing the mesh kept the GN instances permanently stuck/visible; (2)
two GN groups sharing one Collection -- hiding the first's instances
deleted the SHARED batch, wiping out the second GN group's instances
too, permanently (since the shared batch, once gone, has nothing
tracking it back into existence for either source).

**Fix:** `batch_key` now also includes the instancing parent's
identity (`dg_obj_instance.parent.original.as_pointer()`, or `None`),
but ONLY when the instance is flagged -- unflagged instances always
get `None` appended, i.e. identical grouping/behavior/performance to
before for anything never flagged (the vast majority of any scene).
A flagged mesh shared by N distinct instancing sources now gets up to
N separate batches instead of one ambiguous, possibly-unflagged shared
one -- each still one bulk `duplicate_instances()` call, cost scales
with distinct SOURCES per flagged mesh, not instance count. Applied
identically in both `ObjectCache2.first_run()` (object_cache.py) and
`Exporter.update_flagged_only()` (export/__init__.py) -- they must
compute the exact same batch_key for the same conceptual group.
Packaged as
`BlendLuxCore_ConditionalBatching6_persistent-data_v31_segregate_by_source.zip`.
Not yet verified by user testing. The two prior TEMP diagnostics (v29's
per-instance `[first_run-diag]` print, v30's direct `exported_objects`/
`flagged_reexport_keys` dump in `_create_session()`) are still in the
tree pending removal once this is confirmed working.
