from time import time

_needs_reload = "bpy" in locals()

import bpy
import pyluxcore
from .. import utils
from ..utils import render as utils_render
from ..utils import compatibility as utils_compatibility
from ..utils.errorlog import LuxCoreErrorLog
from . import (
    caches,
    camera,
    config,
    imagepipeline,
    light,
    material,
    motion_blur,
    hair,
    halt,
    world,
    mesh_converter,
)
from .light import WORLD_BACKGROUND_LIGHT_NAME
from .caches.object_cache import (
    supports_live_transform, Duplis, _is_always_reexport_flagged,
)

if _needs_reload:
    import importlib

    modules = (
        caches,
        camera,
        config,
        imagepipeline,
        light,
        material,
        motion_blur,
        hair,
        halt,
        world,
        utils,
        mesh_converter,
    )
    for module in modules:
        importlib.reload(module)


class Change:
    NONE = 0

    CONFIG = 1 << 0
    CAMERA = 1 << 1
    OBJECT = 1 << 2
    MATERIAL = 1 << 3
    VISIBILITY = 1 << 4
    WORLD = 1 << 5
    IMAGEPIPELINE = 1 << 6
    HALT = 1 << 7

    REQUIRES_SCENE_EDIT = CAMERA | OBJECT | MATERIAL | VISIBILITY | WORLD
    REQUIRES_VIEW_UPDATE = CONFIG
    REQUIRES_SESSION_PARSE = IMAGEPIPELINE | HALT

    @staticmethod
    def to_string(changes):
        s = ""
        members = [
            attr
            for attr in dir(Change)
            if not callable(getattr(Change, attr))
            and not attr.startswith("__")
        ]
        for changetype in members:
            if changes & getattr(Change, changetype):
                if s:
                    s += " | "
                s += changetype

        return s if changes else "NONE"


class Exporter(object):
    def __init__(self, stats=None):
        self.scene = None  # TODO I would like to remove this, the evaluated scene is temporary
        self.stats = stats

        self.config_cache = caches.StringCache()
        self.camera_cache = caches.CameraCache()
        # self.object_cache = caches.ObjectCache()
        self.object_cache2 = caches.ObjectCache2()
        self.material_cache = caches.MaterialCache()
        self.visibility_cache = caches.VisibilityCache()
        self.world_cache = caches.WorldCache()
        self.imagepipeline_cache = caches.StringCache()
        self.halt_cache = caches.StringCache()
        self.motion_blur_enabled = False
        # Set by callers doing persistent-data animation rendering (a live
        # RenderSession reused across frames via BeginSceneEdit/EndSceneEdit,
        # see debug-helpers/persistent_data_gpu_test.py) to force
        # use_instancing=True for every object, including ones that would
        # otherwise get their world transform baked directly into the mesh
        # (ObjectCache2._compute_use_instancing()). Confirmed by testing
        # (2026-09-23, PERSISTENT_DATA_ANIMATION_NOTES.md): a baked-transform
        # mesh (DefineMeshExt(transformation=...)) gets its transform
        # corrupted (multiplied by itself, once) on a live session that
        # later applies an unrelated scene edit -- the exact same Python-side
        # export was verified correct (single bake, right values, no
        # re-export) and this does NOT reproduce with a normal one-shot
        # final render (fresh session per frame), so the bug is inside
        # LuxCore's live BeginSceneEdit/EndSceneEdit handling of that
        # specific code path, not in this addon's export logic. Objects that
        # already use_instancing=True (transform sent as a separate
        # scene.objects.<key>.transformation property instead of baked into
        # geometry) were not affected. Has no effect on a normal final
        # render (this flag stays False there).
        self.persistent_data_animation = False

        # obj_keys of always_reexport-flagged groups exported by the last
        # call to update_flagged_only() (see that method) -- lets it
        # notice a flagged group that VANISHED (became invisible) since
        # last call, so it can be explicitly removed from the live
        # LuxCore scene. The main loop there only ever visits VISIBLE
        # instances, so without this a flagged object that became
        # invisible was never cleaned up at all (confirmed by testing:
        # it stayed rendered forever, until it reappeared and the
        # already-existing "drop old version before recreating" step
        # incidentally deleted it one frame late).
        self.flagged_reexport_keys = set()

        # A dictionary with the following mapping:
        # {node_key: luxcore_name}
        # Most of the time node_key == luxcore_name, but some nodes have to insert
        # implicit textures n front of themselves which changes their luxcore_name.
        # Avoids re-exporting the same node multiple times.
        # TODO: currently the node cache has to be cleared when an output node starts
        # to export, because we don't have one global properties object.
        self.node_cache = {}

        # If a light/material uses a lightgroup, the id is stored here during export
        self.lightgroup_cache = set()

    def create_session(
        self, depsgraph, context=None, engine=None, view_layer=None
    ):
        # Notes:
        # In final render, context is None

        print("[Exporter] Creating session")
        start = time()
        # TODO 2.8 I'm not too happy about this, we shouldn't keep any
        # reference to temporary data, even if only for a while
        self.scene = depsgraph.scene_eval
        scene = self.scene
        stats = self.stats
        if stats:
            stats.reset()

        # We have to run the compatibility code before export because it could
        # be that the user has linked/appended assets with node trees from
        # previous versions of the addon since opening the .blend file.
        utils_compatibility.run()

        # Scene
        image_resize_policy_props = (
            scene.luxcore.config.image_resize_policy.convert()
        )
        luxcore_scene = pyluxcore.Scene(
            pyluxcore.Properties(), image_resize_policy_props
        )
        scene_props = pyluxcore.Properties()

        # Camera (needs to be parsed first because it is needed for hair
        # tesselation)
        self.camera_cache.diff(
            self, scene, depsgraph, context
        )  # Init camera cache
        luxcore_scene.Parse(self.camera_cache.props)

        if utils.is_valid_camera(scene.camera):
            blur_settings = scene.camera.data.luxcore.motion_blur
            # Don't export camera blur in viewport
            camera_blur = blur_settings.camera_blur and not context
            self.motion_blur_enabled = (
                blur_settings.enable
                and (blur_settings.object_blur or camera_blur)
                and (blur_settings.shutter > 0)
            )

        # Objects and lights
        is_viewport_render = context is not None
        instances = self.object_cache2.first_run(
            self,
            depsgraph,
            view_layer,
            engine,
            luxcore_scene,
            scene_props,
            context,
        )
        if instances is None:
            # Export was cancelled by user
            return None

        # Always init (cheap), so a persistent-data final/animation render can
        # also diff visibility between frames, same as viewport already does
        self.visibility_cache.init(depsgraph, context)

        # Motion blur
        # Motion blur seems not to work in viewport render, i.e. matrix_world
        # is the same on every frame
        if not context and utils.is_valid_camera(scene.camera):
            if self.motion_blur_enabled:
                motion_blur_props, cam_moving = motion_blur.convert(
                    context,
                    engine,
                    scene,
                    depsgraph,
                    self.object_cache2.exported_objects,
                )

                if cam_moving:
                    # Re-export the camera with motion blur enabled
                    # (This is fast and we only have to step through the scene once in total, not twice)
                    camera_props = camera.convert(
                        self, scene, depsgraph, context, cam_moving
                    )
                    motion_blur_props.Set(camera_props)

                scene_props.Set(motion_blur_props)

        # World
        world_props = world.convert(self, depsgraph, scene, is_viewport_render)
        scene_props.Set(world_props)

        if (
            scene.luxcore.debug.enabled
            and scene.luxcore.debug.print_properties
        ):
            print("-" * 50)
            print("DEBUG: Scene Properties:\n")
            print(
                "(Note: does not contain dupli props, only the props of the base object)\n"
            )
            print(scene_props)
            print("-" * 50)
        luxcore_scene.Parse(scene_props)
        # We can only duplicate the instances *after* the scene_props were
        # parsed so the base objects are available for luxcore_scene
        self.object_cache2.duplicate_instances(instances, luxcore_scene, stats)
        # The instances dict can be quite large, delete explicitely (TODO maybe
        # even call gc.collect()?)
        del instances

        # Regularly check if we should abort the export (important in heavy scenes)
        if engine and engine.test_break():
            return None

        # Convert config at last because all lightgroups and passes have to be
        # already defined
        config_props = config.convert(self, scene, context, engine)
        if str(config_props) == "":
            # Config props are empty: there was a critical error in config
            # export, we can't render
            raise Exception("Errors in config, check error log")

        # Init config cache (convert to string here because config_props gets
        # changed below)
        self.config_cache.diff(str(config_props))

        # Imagepipeline
        imagepipeline_props = imagepipeline.convert(scene, context)
        self.imagepipeline_cache.diff(
            imagepipeline_props
        )  # Init imagepipeline cache
        # Add imagepipeline to config props
        config_props.Set(imagepipeline_props)

        # Halt conditions
        halt_props = halt.convert(scene)
        self.halt_cache.diff(halt_props)
        config_props.Set(halt_props)

        light_count = luxcore_scene.GetLightCount()
        if light_count > 1000:
            msg = (
                f"The scene contains a lot of light sources ({light_count}), "
                "performance might suffer "
                f"(each triangle of a meshlight counts as a separate light)"
            )
            LuxCoreErrorLog.add_warning(msg)
        if stats:
            stats.light_count.value = light_count

        # Create the renderconfig
        if (
            scene.luxcore.debug.enabled
            and scene.luxcore.debug.print_properties
        ):
            print("-" * 50)
            print("DEBUG: Config Properties:\n")
            print(config_props)
            print("-" * 50)
        renderconfig = pyluxcore.RenderConfig(config_props, luxcore_scene)

        # Regularly check if we should abort the export (important in heavy
        # scenes)
        if engine and engine.test_break():
            return None

        export_time = time() - start
        print("Export took %.1f s" % export_time)
        if stats:
            stats.export_time.value = export_time
            self._init_stats(stats, config_props, scene)

        # Pre-compile CUDA or OpenCL kernels for viewport and final.
        renderengine_type = config_props.Get("renderengine.type").GetString()
        if (
            renderengine_type.endswith("OCL")
            and not renderconfig.HasCachedKernels()
        ):
            if engine:
                gpu_backend = utils.get_addon_preferences(
                    bpy.context
                ).gpu_backend
                message = (
                    f"Compiling {gpu_backend} kernels (just once, "
                    "usually takes 15-30 minutes)"
                )
                engine.report({"INFO"}, message)
                engine.update_stats(message, "")

            # Copy config props so we can pass scene.epsilon.min,
            # scene.epsilon.max and opencl.devices.select to the kernel
            config_props_copy = pyluxcore.Properties(config_props)
            engines = ["PATHOCL", "RTPATHOCL"]
            if renderengine_type == "TILEPATHOCL":
                # Only pre-compile for tiled path if requested, since it's
                # rarely used
                engines.append("TILEPATHOCL")
            config_props_copy.Set(
                pyluxcore.Property(
                    "kernelcachefill.renderengine.types", engines
                )
            )
            pyluxcore.KernelCacheFill(config_props_copy)

        # Inform about pre-computations that can take a long time to complete,
        # like caches
        if engine:
            message = "Creating RenderSession"

            # Caches are never used in viewport render
            if not is_viewport_render:
                # The second argument of Get() is used as fallback if the
                # property is not set
                cache_indirect = config_props.Get(
                    "path.photongi.indirect.enabled", [False]
                ).GetBool()
                cache_caustics = config_props.Get(
                    "path.photongi.caustic.enabled", [False]
                ).GetBool()
                cache_envlight = scene.luxcore.config.envlight_cache.enabled
                cache_dls = (
                    config_props.Get("lightstrategy.type", [""]).GetString()
                    == "DLS_CACHE"
                )

                if stats:
                    stats.cache_indirect.value = cache_indirect
                    stats.cache_caustics.value = cache_caustics
                    stats.cache_envlight.value = cache_envlight
                    stats.cache_dls.value = cache_dls

                cache_state = {
                    "Indirect Light": cache_indirect,
                    "Caustics": cache_caustics,
                    "Env. Light": cache_envlight,
                    "DLSC": cache_dls,
                }
                enabled_caches = [
                    key for key, value in cache_state.items() if value
                ]

                if any(enabled_caches):
                    message += (
                        ", computing caches ("
                        + ", ".join(enabled_caches)
                        + ")"
                    )

            message += " ..."
            engine.update_stats(
                "Export Finished (%.1f s)" % export_time, message
            )

        # Do not hold reference to temporary data
        self.scene = None
        return pyluxcore.RenderSession(renderconfig)

    def get_viewport_changes(self, depsgraph, context=None):
        self.scene = depsgraph.scene_eval
        changes = Change.NONE

        config_props = config.convert(self, self.scene, context)
        if self.config_cache.diff(config_props):
            changes |= Change.CONFIG

        if self.camera_cache.diff(self, self.scene, depsgraph, context):
            changes |= Change.CAMERA

        # Do not hold reference to temporary data
        self.scene = None
        return changes

    def get_changes(self, depsgraph, context=None, changes=None, force_diff=False):
        self.scene = depsgraph.scene_eval
        final = context is None

        # Particle system counts might have changed
        supports_live_transform.cache_clear()

        if not final:
            if changes is None:
                changes = self.get_viewport_changes(depsgraph, context)

        if changes is None:
            changes = Change.NONE

        # force_diff: used by final/animation render with persistent data enabled,
        # to detect changes between animation frames the same way viewport does
        # (camera is not diffed here in the viewport case, get_viewport_changes()
        # above already did it)
        if not final or force_diff:
            if final and self.camera_cache.diff(self, self.scene, depsgraph, context):
                changes |= Change.CAMERA

            if self.object_cache2.diff(depsgraph):
                changes |= Change.OBJECT

            if self.material_cache.diff(depsgraph):
                changes |= Change.MATERIAL

            if self.visibility_cache.diff(depsgraph, context):
                changes |= Change.VISIBILITY

                if self.visibility_cache.has_new_objects:
                    changes |= Change.OBJECT

            if self.world_cache.diff(depsgraph):
                changes |= Change.WORLD

        # Relevant during final render
        imagepipeline_props = imagepipeline.convert(depsgraph.scene, context)
        if self.imagepipeline_cache.diff(imagepipeline_props):
            changes |= Change.IMAGEPIPELINE

        if final:
            # Halt conditions are only used during final render
            halt_props = halt.convert(depsgraph.scene)
            if self.halt_cache.diff(halt_props):
                changes |= Change.HALT

        # Do not hold reference to temporary data
        self.scene = None
        return changes

    def update(self, depsgraph, context, session, changes):
        self.scene = depsgraph.scene_eval
        print("[Exporter] Update because of:", Change.to_string(changes))
        # Invalidate node cache
        self.node_cache.clear()

        if changes & Change.CONFIG:
            # We already converted the new config settings during
            # get_changes(), re-use them
            session = self._update_config(session, self.config_cache.props)

        if changes & Change.REQUIRES_SCENE_EDIT:
            luxcore_scene = session.GetRenderConfig().GetScene()
            session.BeginSceneEdit()

            try:
                props = self._update_scene(
                    depsgraph, context, changes, luxcore_scene
                )
                luxcore_scene.Parse(props)
            except Exception as error:
                LuxCoreErrorLog.add_error(error)
                import traceback

                traceback.print_exc()

            try:
                session.EndSceneEdit()
            except RuntimeError as error:
                import traceback

                traceback.print_exc()
                LuxCoreErrorLog.add_error(error)
                print("Fatal error, stopping session.")
                session.Stop()  # TODO not sure if this works
                raise

            if session.IsInPause():
                session.Resume()

        if changes & Change.REQUIRES_SESSION_PARSE:
            self.update_session(changes, session)

        # Do not hold reference to temporary data
        self.scene = None

        # We have to return and re-assign the session in the RenderEngine,
        # because it might have been replaced in _update_config()
        return session

    def update_camera_only(self, depsgraph, session):
        """
        Persistent-data animation, v23 (2026-09-23): deliberately does NOT
        look at depsgraph.updates / Change detection / ObjectCache2.update()
        at all -- every object, material, light, world and instance is
        treated as 100% static after the first frame's first_run(). The
        ONLY thing re-parsed on this live session, every frame, is the
        camera.

        Why: ObjectCache2.update()'s per-instance diffing (the
        "object_instances" loop in object_cache.py) has no way to
        recognize an instance that already belongs to an existing,
        unchanged hybrid-batched group -- only the group's single "base"
        object is tracked in exported_objects, so every OTHER member
        instance looks brand new to that loop, every single time it runs,
        regardless of whether anything actually moved. And it runs
        whenever Change.OBJECT fires for ANY reason anywhere in the scene
        (see get_changes()), not just for the group that changed. On a
        heavily hybrid-batched scene this makes update() slower than a
        full first_run() -- confirmed by the user, not a hypothesis (see
        PERSISTENT_DATA_ANIMATION_NOTES.md). A proper fix (teach the
        object_instances loop to recognize and skip unchanged batch
        members, and bulk-rebatch only groups that truly changed) is
        still needed for a version where OBJECTS also animate. This
        method is for the case where the camera is the only thing that
        actually needs to move between frames: it sidesteps the whole
        problem by never calling into that code path at all.

        Camera-only BeginSceneEdit/EndSceneEdit on a live session was
        already confirmed cheap and safe by prior testing (viewport
        render continuously moves the camera through this exact same
        path with no expensive recompile -- see the "v8 postmortem" in
        debug-helpers/persistent_data_gpu_test.py).

        Caller is responsible for keeping this ONE session alive across
        ALL frames from within a single continuous render() call (see
        that same script's module docstring for why a live session must
        never be kept alive *across separate* render() invocations).
        """
        scene = depsgraph.scene_eval
        luxcore_scene = session.GetRenderConfig().GetScene()

        session.BeginSceneEdit()
        try:
            self.camera_cache.diff(self, scene, depsgraph, None)
            luxcore_scene.Parse(self.camera_cache.props)
        except Exception as error:
            LuxCoreErrorLog.add_error(error)
            import traceback

            traceback.print_exc()

        try:
            session.EndSceneEdit()
        except RuntimeError as error:
            import traceback

            traceback.print_exc()
            LuxCoreErrorLog.add_error(error)
            print("Fatal error, stopping session.")
            session.Stop()
            raise

        if session.IsInPause():
            session.Resume()

        return session

    def update_flagged_only(self, depsgraph, session, view_layer=None):
        """
        2026-09-24 fix (v2 -- the first attempt was incomplete): a
        flagged group that became INVISIBLE was never cleaned up at
        all -- the main loop below only ever visits currently-visible
        instances, so disappearance had no handler on its own.

        self.flagged_reexport_keys tracks which obj_keys currently
        belong to flagged groups, so a group missing from this call's
        visible set can be explicitly removed below. It is seeded and
        maintained in exactly TWO places, both computing obj_key from
        the SAME dg_obj_instance that was actually used to create/find
        the entry, in the SAME loop iteration -- never re-derived by a
        separate, later walk over depsgraph.object_instances:
        - first_run() (ObjectCache2.first_run(), object_cache.py),
          right where it creates a NEW batch for a flagged group.
        - This method's own loop, below.
        The first version of this fix seeded flagged_reexport_keys via
        a SEPARATE function (compute_flagged_visible_keys(), since
        removed) that re-walked depsgraph.object_instances after
        first_run() already ran and guessed which instance first_run()
        had used as each batch's base. Confirmed by testing (real
        production scene, a flagged Geometry-Nodes-driven forest) that
        this guess is systematically wrong, not randomly: the "removed
        N vanished group(s)" count was never 0, but the actual
        `.pop(obj_key, None)` lookup silently missed every time,
        deleting nothing (a coincidental, unrelated object happened to
        share that count once, which is what made it initially look
        like partial success). Instances stayed visible on every frame
        of a present -> absent -> present test. Root-caused to the
        second, independent walk landing on a different "first"
        instance than first_run()'s own single pass did -- eliminated
        entirely by recording the key at the source instead of
        guessing it afterwards.

        v24 (2026-09-23): update_camera_only() plus a targeted, bulk
        refresh of exactly the objects/instancing-parents flagged with
        obj.luxcore.always_reexport -- nothing else. Still no
        depsgraph.updates inspection, no Change detection, no touching
        any other object: same "explicit, not inferred" philosophy as
        update_camera_only(), extended to cover a small, user-designated
        set of meshes that DO need to move/change, instead of freezing
        literally everything but the camera.

        Key difference from ObjectCache2.update()'s existing (slow, see
        PERSISTENT_DATA_ANIMATION_NOTES.md) per-instance fallback: this
        re-batches flagged groups the SAME way first_run() does --
        grouped by (mesh, visibility, holdout) with ONE bulk
        duplicate_instances()/DuplicateObject() call per group -- instead
        of calling _convert_obj() once per instance. Cost is proportional
        to the number of flagged GROUPS, not the number of flagged
        INSTANCES.

        Still has to walk depsgraph.object_instances once to find which
        instances belong to a flagged object/parent (Blender has no
        "give me only instances of X" API) -- that enumeration itself is
        cheap (one flag check per instance); what's skipped is the
        expensive per-instance _convert_obj() conversion for every non-
        flagged instance in the scene.

        Any previously exported version of a flagged group is deleted
        from the live LuxCore scene first, so a group whose instance
        count or positions changed doesn't leak the old dupli objects or
        end up double-batched.
        """
        scene = depsgraph.scene_eval
        luxcore_scene = session.GetRenderConfig().GetScene()
        object_cache2 = self.object_cache2

        session.BeginSceneEdit()
        try:
            self.camera_cache.diff(self, scene, depsgraph, None)
            luxcore_scene.Parse(self.camera_cache.props)

            scene_props = pyluxcore.Properties()
            instances = {}
            dropped_keys = set()
            seen_obj_keys = set()
            seen_names = set()
            flagged_but_filtered = []

            for dg_obj_instance in depsgraph.object_instances:
                obj = dg_obj_instance.object

                if not _is_always_reexport_flagged(obj, dg_obj_instance):
                    continue
                if obj.type not in utils.MESH_OBJECTS:
                    flagged_but_filtered.append((obj.name, f"wrong type: {obj.type}"))
                    continue
                if not utils.is_instance_visible(dg_obj_instance, obj, None):
                    flagged_but_filtered.append((obj.name, "not visible"))
                    continue

                seen_names.add(obj.name)
                # 2026-09-24: must match first_run()'s batch_key exactly
                # (object_cache.py) -- segregated by instancing-parent
                # identity too, so a flagged mesh shared with some OTHER,
                # unflagged instancing source (or a second, independent
                # flagged group) never merges into the same batch. Every
                # instance reaching this point is already confirmed
                # flagged (see the `continue` above), so this is
                # unconditional here.
                parent = dg_obj_instance.parent
                batch_key = (
                    obj.original.as_pointer(), False, False,
                    parent.original.as_pointer() if parent else None,
                )

                if batch_key not in instances:
                    # First instance of this flagged group seen this call:
                    # drop whatever we exported for it before (if
                    # anything), so re-batching below starts clean.
                    if batch_key not in dropped_keys:
                        dropped_keys.add(batch_key)
                        obj_key = utils.make_key_from_instance(dg_obj_instance)
                        seen_obj_keys.add(obj_key)
                        old = object_cache2.exported_objects.pop(obj_key, None)
                        if old is not None:
                            old.delete(luxcore_scene)

                    exported_obj = object_cache2._convert_obj(
                        self, dg_obj_instance, obj, depsgraph, luxcore_scene,
                        scene_props, False, view_layer, None,
                    )
                    if exported_obj:
                        instances[batch_key] = Duplis(exported_obj)
                    else:
                        instances[batch_key] = None
                    continue

                duplis = instances[batch_key]
                if duplis is None:
                    continue
                obj_id = obj.original.luxcore.id
                if obj_id == -1:
                    obj_id = dg_obj_instance.random_id & 0xFFFFFFFE
                duplis.object_ids.append(obj_id)
                duplis.matrices.extend(
                    pyluxcore.BlenderMatrix4x4ToList(
                        dg_obj_instance.matrix_world.copy()
                    )
                )

            luxcore_scene.Parse(scene_props)
            object_cache2.duplicate_instances(instances, luxcore_scene, self.stats)

            # The loop above only ever visits VISIBLE instances -- a
            # flagged group that became invisible since the last call
            # never shows up in it at all, so without this it was never
            # cleaned up. Remove anything we were tracking last call
            # that didn't show up as visible+flagged this call. Every
            # key in flagged_reexport_keys was recorded at its source
            # (see this method's docstring) -- if pop() ever misses
            # here now, that's a real, different bug, not the stale
            # second-guess this replaced.
            vanished_keys = self.flagged_reexport_keys - seen_obj_keys
            actually_removed = 0
            for obj_key in vanished_keys:
                old = object_cache2.exported_objects.pop(obj_key, None)
                if old is not None:
                    old.delete(luxcore_scene)
                    actually_removed += 1
            self.flagged_reexport_keys = seen_obj_keys

            print(
                f"[update_flagged_only] refreshed {len(instances)} flagged "
                f"group(s) {sorted(seen_names)}, {len(vanished_keys)} "
                f"vanished (actually removed: {actually_removed})"
            )
            if flagged_but_filtered:
                print(f"[update_flagged_only] flagged but skipped: {flagged_but_filtered}")
        except Exception as error:
            LuxCoreErrorLog.add_error(error)
            import traceback

            traceback.print_exc()

        try:
            session.EndSceneEdit()
        except RuntimeError as error:
            import traceback

            traceback.print_exc()
            LuxCoreErrorLog.add_error(error)
            print("Fatal error, stopping session.")
            session.Stop()
            raise

        if session.IsInPause():
            session.Resume()

        return session

    def update_session(self, changes, session):
        if changes & Change.IMAGEPIPELINE:
            session.Parse(self.imagepipeline_cache.props)
        if changes & Change.HALT:
            session.Parse(self.halt_cache.props)

    def _update_config(self, session, config_props):
        # Note: Currently not used, see the comment on force_session_restart() in engine/viewport.py
        raise NotImplementedError(
            "_update_config() currently not supported due to memory leak "
            "(see https://github.com/LuxCoreRender/BlendLuxCore/issues/577)"
        )
        # renderconfig = session.GetRenderConfig()
        # session.Stop()
        #
        # renderconfig.Parse(config_props)
        # session = pyluxcore.RenderSession(renderconfig)
        # session.Start()
        # return session

    def _update_scene(self, depsgraph, context, changes, luxcore_scene):
        props = pyluxcore.Properties()

        if changes & Change.CAMERA:
            # We already converted the new camera settings during
            # get_changes(), re-use them
            props.Set(self.camera_cache.props)

        if changes & Change.OBJECT:
            # Get original view_layer from evaluated (evaluated doesn't work correctly with indirect_only_get)
            view_layer_eval = depsgraph.view_layer_eval
            scene_orig = depsgraph.scene_eval.original
            view_layer = scene_orig.view_layers.get(view_layer_eval.name)
            self.object_cache2.update(
                self, depsgraph, luxcore_scene, props, context, view_layer
            )

        if changes & Change.MATERIAL:
            self.material_cache.update(self, depsgraph, context, props)

        if changes & Change.VISIBILITY:
            for key in self.visibility_cache.objects_to_remove:
                try:
                    exported_obj = self.object_cache2.exported_objects.pop(key)
                    exported_obj.delete(luxcore_scene)
                    print("Removed object with key", key)
                except KeyError:
                    # This is ok, not every exportable object is added to exported_objects
                    # (e.g. batched/duplicated instances only have ONE entry in
                    # exported_objects per batch, not one per instance - so most
                    # individual instance keys from VisibilityCache will miss here)
                    print("Could not remove object with key", key, "(not in exported_objects, likely batched)")

            if self.visibility_cache.objects_to_remove:
                # Disabled, same family of bug as RemoveUnusedMeshes() below: these
                # can incorrectly remove materials/textures/imagemaps that are still
                # referenced by unrelated, unchanged objects (observed: an unrelated
                # static object losing its texture/material after a VISIBILITY-driven
                # cleanup elsewhere in the scene)
                # luxcore_scene.RemoveUnusedMeshes()  # TODO for some reason this deletes even some meshes that are still in use
                # luxcore_scene.RemoveUnusedMaterials()
                # luxcore_scene.RemoveUnusedTextures()
                # luxcore_scene.RemoveUnusedImageMaps()
                pass

        if changes & Change.WORLD:
            if (
                not self.scene.world
                or self.scene.world.luxcore.light == "none"
            ):
                luxcore_scene.DeleteLight(WORLD_BACKGROUND_LIGHT_NAME)

            world_props = world.convert(
                self, depsgraph, self.scene, is_viewport_render=context is not None
            )
            props.Set(world_props)

        return props

    def _init_stats(self, stats, config_props, scene):
        render_engine = config_props.Get("renderengine.type").GetString()
        stats.render_engine.value = utils_render.engine_to_str(render_engine)
        sampler = config_props.Get("sampler.type").GetString()
        stats.sampler.value = utils_render.sampler_to_str(sampler)

        config_settings = scene.luxcore.config
        path_settings = config_settings.path

        if render_engine == "BIDIRCPU":
            path_depths = (
                config_settings.bidir_path_maxdepth,
                config_settings.bidir_light_maxdepth,
            )
        else:
            path_depths = (
                path_settings.depth_total,
                path_settings.depth_diffuse,
                path_settings.depth_glossy,
                path_settings.depth_specular,
            )
        stats.path_depths.value = path_depths

        if path_settings.use_clamping:
            stats.clamping.value = path_settings.clamping
        else:
            stats.clamping.value = 0

        stats.use_hybridbackforward.value = (
            config_props.Get(
                "path.hybridbackforward.enable", [False]
            ).GetBool()
            and render_engine != "BIDIRCPU"
        )
