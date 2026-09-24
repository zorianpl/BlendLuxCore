import bpy
from array import array
from functools import lru_cache
from time import time
import hashlib

from ... import utils
import pyluxcore
from .. import mesh_converter
from ..hair import (
    convert_hair,
    warn_about_missing_uvs,
    set_hair_props,
    make_hair_shape_name,
    get_hair_material_index,
    convert_hair_curves,
)
from .exported_data import ExportedObject, ExportedPart
from .. import light, material
from ...utils.errorlog import LuxCoreErrorLog
from ...utils import node as utils_node
from ...utils import MESH_OBJECTS
from ...utils.node import get_active_output


class TriAOVDataIndices:
    RANDOM_PER_ISLAND_INT = 0
    RANDOM_PER_ISLAND_FLOAT = 1


MAX_PARTICLES_FOR_LIVE_TRANSFORM = 2000


def uses_pointiness(node_tree):
    # TODO better check would be if the node is linked to the output and actually used
    return utils_node.has_nodes(node_tree, "LuxCoreNodeTexPointiness", True)


def uses_random_per_island_uniform_float(node_tree):
    # TODO better check would be if the node is linked to the output and actually used
    return utils_node.has_nodes(
        node_tree, "LuxCoreNodeTexRandomPerIsland", True
    )


def uses_random_per_island_int(node_tree):
    # TODO better check would be if the node is linked to the output and actually used
    for node in utils_node.find_nodes_multi(
        node_tree, {"LuxCoreNodeTexMapping2D", "LuxCoreNodeTexMapping3D"}, True
    ):
        if (
            node.mapping_type in {"uvrandommapping2d", "localrandommapping3d"}
            and node.seed_type == "mesh_islands"
        ):
            return True
    return False


def needs_edge_detector_shape(node_tree):
    # TODO better check would be if the node is linked to the output and actually used
    for node in utils_node.find_nodes(
        node_tree, "LuxCoreNodeTexWireframe", True
    ):
        if node.hide_planar_edges:
            return True
    return False


def uses_displacement(obj):
    for mat_slot in obj.material_slots:
        mat = mat_slot.material
        if (
            mat
            and mat.luxcore.node_tree
            and utils_node.has_nodes_multi(
                mat.luxcore.node_tree,
                {
                    "LuxCoreNodeShapeHeightDisplacement",
                    "LuxCoreNodeShapeVectorDisplacement",
                },
                True,
            )
        ):
            return True
    return False


def define_shapes(input_shape, node_tree, exporter, depsgraph, scene_props):
    shape = input_shape

    output_node = get_active_output(node_tree)
    if output_node:
        # Convert the whole shape stack
        shape = output_node.inputs["Shape"].export_shape(
            exporter, depsgraph, scene_props, shape
        )

    # Add some shapes at the end that are required by some nodes in the node tree

    if uses_pointiness(node_tree):
        # Note: Since Blender still does not make use of the vertex alpha channel
        # as of 2.82, we use it to store the pointiness information.
        pointiness_shape = input_shape + "_pointiness"
        prefix = "scene.shapes." + pointiness_shape + "."
        scene_props.Set(pyluxcore.Property(prefix + "type", "pointiness"))
        scene_props.Set(pyluxcore.Property(prefix + "source", shape))
        shape = pointiness_shape

    _uses_random_per_island_uniform_float = (
        uses_random_per_island_uniform_float(node_tree)
    )
    _uses_random_per_island_int = uses_random_per_island_int(node_tree)
    if _uses_random_per_island_uniform_float or _uses_random_per_island_int:
        island_aov_index = TriAOVDataIndices.RANDOM_PER_ISLAND_INT

        if not _uses_random_per_island_int:
            # We don't need the int result, so use the float index for it so it gets overwrittten to save memory
            island_aov_index = TriAOVDataIndices.RANDOM_PER_ISLAND_FLOAT

        island_aov_shape = input_shape + "_island_aov"
        prefix = "scene.shapes." + island_aov_shape + "."
        scene_props.Set(pyluxcore.Property(prefix + "type", "islandaov"))
        scene_props.Set(pyluxcore.Property(prefix + "source", shape))
        scene_props.Set(
            pyluxcore.Property(prefix + "dataindex", island_aov_index)
        )
        shape = island_aov_shape

        if _uses_random_per_island_uniform_float:
            # Used to normalize the island indices from ints to floats in 0..1 range
            random_tri_aov_shape = input_shape + "_random_tri_aov_shape"
            prefix = "scene.shapes." + random_tri_aov_shape + "."
            scene_props.Set(
                pyluxcore.Property(prefix + "type", "randomtriangleaov")
            )
            scene_props.Set(pyluxcore.Property(prefix + "source", shape))
            scene_props.Set(
                pyluxcore.Property(prefix + "srcdataindex", island_aov_index)
            )
            scene_props.Set(
                pyluxcore.Property(
                    prefix + "dstdataindex",
                    TriAOVDataIndices.RANDOM_PER_ISLAND_FLOAT,
                )
            )
            shape = random_tri_aov_shape

    if needs_edge_detector_shape(node_tree):
        edge_detector_shape = input_shape + "_edge_detector"
        prefix = "scene.shapes." + edge_detector_shape + "."
        scene_props.Set(pyluxcore.Property(prefix + "type", "edgedetectoraov"))
        scene_props.Set(pyluxcore.Property(prefix + "source", shape))
        shape = edge_detector_shape

    return shape


def warn_about_subdivision_levels(obj):
    for modifier in obj.modifiers:
        if modifier.type == "SUBSURF" and modifier.show_viewport:
            if not modifier.show_render:
                LuxCoreErrorLog.add_warning(
                    "Subdivision modifier enabled in viewport, but not in final render",
                    obj_name=obj.name,
                )
            elif modifier.render_levels < modifier.levels:
                LuxCoreErrorLog.add_warning(
                    f"Final render subdivision level ({modifier.render_levels}) smaller than viewport subdivision level ({modifier.levels})",
                    obj_name=obj.name,
                )


def get_material(obj, material_index, depsgraph):
    material_override = (
        depsgraph.view_layer_eval.material_override
    )  # the view layer override material
    # Evaluate if the override_exclude checkbox is ticked
    override_exclude = False
    material = None
    if material_index < len(obj.material_slots):
        material = obj.material_slots[material_index].material
    if material is not None:
        node_tree = material.luxcore.node_tree
        if (
            node_tree is not None
        ):  # happens e.g. in default cube scene when only cycles nodes are defined
            output_node = get_active_output(node_tree)
            try:
                override_exclude = output_node.override_exclude
            except AttributeError:
                override_exclude = True

    if material_override and material is None:
        mat = material_override
    elif material_override and not override_exclude:
        mat = material_override
    elif material_index < len(obj.material_slots):
        mat = material

        if mat is None:
            # Note: material.convert returns the fallback material in this case
            msg = "No material attached to slot %d" % (material_index + 1)
            LuxCoreErrorLog.add_warning(msg, obj_name=obj.name)
    else:
        # The object has no material slots
        LuxCoreErrorLog.add_warning("No material defined", obj_name=obj.name)
        # Use fallback material
        mat = None

    return mat


def export_material(
    obj, material_index, exporter, depsgraph, is_viewport_render, force_holdout=False
):
    mat = get_material(obj, material_index, depsgraph)

    if mat:
        # We need the original material, not the evaluated one, otherwise
        # Blender gives us "NodeTreeUndefined" as mat.node_tree.bl_idname
        mat = mat.original

        lux_mat_name, mat_props = material.convert(
            exporter, depsgraph, mat, is_viewport_render, obj.name, force_holdout
        )
        node_tree = mat.luxcore.node_tree
        return lux_mat_name, mat_props, node_tree
    else:
        lux_mat_name, mat_props = material.fallback()
        return lux_mat_name, mat_props, None


def make_psys_key(obj, psys, is_instance):
    psys_lib_name = psys.settings.library.name if psys.settings.library else ""
    return obj.name_full + psys.name + psys_lib_name + str(is_instance)


def get_total_particle_count(particle_system, is_viewport_render):
    """
    Note: this function does not return the amount of particles that are actually visible in a given
    frame (because it's hard to find that number), but the maximum number the particle system will ever create.
    """
    settings = particle_system.settings

    if (
        settings.render_type in {"NONE", "HALO", "LINE"}
        or (settings.type == "HAIR" and settings.render_type == "PATH")
        or (is_viewport_render and settings.display_method != "RENDER")
    ):
        return 0

    particle_count = settings.count
    if is_viewport_render:
        particle_count *= settings.display_percentage / 100
    if settings.child_type != "NONE":
        particle_count *= (
            settings.child_percent
            if is_viewport_render
            else settings.rendered_child_count
        )
    return particle_count


@lru_cache(maxsize=32)
def supports_live_transform(particle_system):
    if not particle_system:
        return True
    total_particles = get_total_particle_count(particle_system, True)
    return total_particles <= MAX_PARTICLES_FOR_LIVE_TRANSFORM


def _wants_individual_tracking(obj, dg_obj_instance):
    # Hybrid batching merges same-mesh dupli instances into ONE
    # ExportedObject + N-1 DuplicateObject() copies in LuxCore; a batch is
    # only ever built once (first_run()) and its base key checked for
    # removal via VisibilityCache -- update() never re-batches, so an
    # instance-count change on an already-batched group is only ever
    # correctly picked up when the group goes to exactly zero. Skipping
    # batching entirely makes every instance individually tracked under its
    # own key instead, the same way a manually hidden/shown object already
    # is -- slower and heavier on LuxCore-side memory, but reliably
    # re-evaluated every diff frame. Manual opt-in only:
    # obj.luxcore.always_reexport, set by the user on the object (or on the
    # Collection Instance empty that hosts a "Duplicate Collection" setup),
    # see DESC_ALWAYS_REEXPORT in properties/blender_object.py.
    #
    # There used to also be automatic detection here (any Geometry Nodes
    # modifier on the instancing parent), added after a second Collection
    # Instance object without the manual flag reproduced the same
    # "doesn't disappear" bug as the first one. Reverted (2026-09-23): on a
    # heavy production scene (millions of instances, many GN systems, most
    # of them with a stable instance count that never actually needed this)
    # it forced every single GN-sourced instance group out of hybrid
    # batching, turning one fast batched DuplicateObject() C++ call per
    # group into one Python _convert_obj() call per instance -- a
    # confirmed severe regression (80s/frame -> still not done after
    # several minutes). Correctness for the few GN systems that actually
    # need this is back to being the user's responsibility to flag by hand;
    # see PERSISTENT_DATA_ANIMATION_NOTES.md for the reasoning either way.
    parent = dg_obj_instance.parent
    if getattr(obj.luxcore, "always_reexport", False):
        return True
    if parent is not None and getattr(parent.luxcore, "always_reexport", False):
        return True
    return False


def _compute_use_instancing(exporter, dg_obj_instance, obj, is_viewport_render):
    # Objects with displacement in the node tree are instanced to avoid discrepancies between viewport and final render
    # Proxy objects are always instanced too: their geometry is loaded from an external file in local
    # space (not baked with the object's transform), so the object's own transform must always be sent
    # to the engine separately - which only happens on the instancing path (see ExportedObject.get_props).
    return (
        is_viewport_render
        or getattr(exporter, "persistent_data_animation", False)
        or dg_obj_instance.is_instance
        or utils.can_share_mesh(obj.original)
        or (exporter.motion_blur_enabled and obj.luxcore.enable_motion_blur)
        or uses_displacement(obj)
        or (hasattr(obj.luxcore, "use_proxy") and obj.luxcore.use_proxy)
    )


def _update_stats(
    engine, current_obj_name, extra_obj_info, current_index, total_object_count
):
    engine.update_stats(
        "Export",
        f"Object: {current_obj_name}{extra_obj_info} ({current_index}/{total_object_count})",
    )
    engine.update_progress(current_index / total_object_count)


def get_obj_count_estimate(depsgraph):
    # This is faster than len(depsgraph.object_instances)
    # TODO: count dupliverts and dupliframes
    obj_count = len(depsgraph.objects)
    for obj in depsgraph.objects:
        try:
            for psys in obj.particle_systems:
                obj_count += get_total_particle_count(psys, False)
        except AttributeError:
            pass
    return obj_count


class Duplis:
    def __init__(self, exported_obj):
        self.exported_obj = exported_obj
        self.matrices = array("f", [])
        self.object_ids = array("I", [])

    def get_count(self):
        return len(self.object_ids)


class ObjectCache2:
    def __init__(self):
        self.exported_objects = {}
        self.exported_meshes = {}
        self.exported_hair = {}

    def first_run(
        self,
        exporter,
        depsgraph,
        view_layer,
        engine,
        luxcore_scene,
        scene_props,
        context,
    ):
        is_viewport_render = bool(context)
        instances = {}

        # Hybrid batching: Conditional + Smart
        # Check once if scene uses indirect_only/holdout to avoid per-instance overhead
        scene_uses_indirect_or_holdout = False
        if view_layer:
            def check_layer_coll(lc):
                if lc.indirect_only or lc.holdout:
                    return True
                for child in lc.children:
                    if check_layer_coll(child):
                        return True
                return False
            scene_uses_indirect_or_holdout = check_layer_coll(view_layer.layer_collection)

            if scene_uses_indirect_or_holdout:
                print(f"[Export] Hybrid batching: Scene uses indirect_only/holdout - full smart batching")
            else:
                print(f"[Export] Hybrid batching: Scene clean - fast batching")

        if engine:
            obj_count_estimate = max(1, get_obj_count_estimate(depsgraph))
        else:
            obj_count_estimate = 0

        # Particle system counts might have changed
        supports_live_transform.cache_clear()

        for index, dg_obj_instance in enumerate(depsgraph.object_instances):
            obj = dg_obj_instance.object

            if (
                dg_obj_instance.is_instance
                and not (
                    is_viewport_render
                    and supports_live_transform(
                        dg_obj_instance.particle_system
                    )
                )
                and obj.type in MESH_OBJECTS
                # Smart batching: always allow batching, group by (mesh, visibility) later
                and not _wants_individual_tracking(obj, dg_obj_instance)
            ):
                # This code is optimized for large amounts of duplis. Drawback is that objects generated from this
                # code can't be transformed later in a viewport render session (due to BlendLuxCore implementation
                # reasons, not because of LuxCore)
                if engine and index % 5000 == 0:
                    if engine.test_break():
                        return None
                    _update_stats(
                        engine, obj.name, " (dupli)", index, obj_count_estimate
                    )

                # Hybrid batching: Conditional + Smart
                if scene_uses_indirect_or_holdout:
                    # Full smart batching: Calculate visibility and holdout per instance
                    instance_visible = utils.visible_to_camera(dg_obj_instance, is_viewport_render, view_layer)

                    # Holdout overrides indirect_only
                    check_obj = dg_obj_instance.parent if dg_obj_instance.is_instance else obj
                    is_holdout = utils.is_holdout_object(check_obj.original, view_layer)

                    if is_holdout:
                        instance_visible = True  # Holdout needs to be visible to camera to cut hole

                    # Key: (mesh_pointer, camerainvisible, is_holdout)
                    # Must distinguish holdout vs normal visible - they need different materials!
                    camerainvisible = not instance_visible
                    batch_key = (obj.original.as_pointer(), camerainvisible, is_holdout)
                else:
                    # Fast batching: Simple key without per-instance overhead
                    # All instances assumed visible, no holdout/indirect_only checks
                    batch_key = (obj.original.as_pointer(), False, False)  # (mesh, camerainvisible=False, is_holdout=False)

                try:
                    # The code in this try block is performance-critical, as it is
                    # executed most often when exporting millions of instances.
                    duplis = instances[batch_key]
                    # If duplis is None, then a non-exportable object like a curve with zero faces is being duplicated
                    if duplis:
                        obj_id = dg_obj_instance.object.original.luxcore.id
                        if obj_id == -1:
                            obj_id = dg_obj_instance.random_id & 0xFFFFFFFE
                        duplis.object_ids.append(obj_id)
                        # We need a copy of matrix_world here, not sure why, but if we don't
                        # make a copy, we only get an identity matrix in C++
                        duplis.matrices.extend(
                            pyluxcore.BlenderMatrix4x4ToList(
                                dg_obj_instance.matrix_world.copy()
                            )
                        )
                except KeyError:
                    if engine:
                        if engine.test_break():
                            return None
                        _update_stats(
                            engine,
                            obj.name,
                            " (dupli)",
                            index,
                            obj_count_estimate,
                        )
                    exported_obj = self._convert_obj(
                        exporter,
                        dg_obj_instance,
                        obj,
                        depsgraph,
                        luxcore_scene,
                        scene_props,
                        is_viewport_render,
                        view_layer,
                        engine,
                    )
                    if exported_obj:
                        # Note, the transformation matrix and object ID of this first instance is not added
                        # to the duplication list, since it already exists in the scene
                        # Smart batching: Store by (mesh, visibility) key
                        instances[batch_key] = Duplis(
                            exported_obj
                        )
                    else:
                        # Could not export the object, happens e.g. with curve objects with zero faces
                        instances[batch_key] = None
            else:
                # This code is for singular objects and for duplis that should be movable later in a viewport render
                if not utils.is_instance_visible(
                    dg_obj_instance, obj, context
                ):
                    continue

                if engine:
                    if engine.test_break():
                        return None
                    _update_stats(
                        engine, obj.name, "", index, obj_count_estimate
                    )

                self._convert_obj(
                    exporter,
                    dg_obj_instance,
                    obj,
                    depsgraph,
                    luxcore_scene,
                    scene_props,
                    is_viewport_render,
                    view_layer,
                    engine,
                )

        # self._debug_info()
        return instances

    def duplicate_instances(self, instances, luxcore_scene, stats):
        """
        We can only duplicate the instances *after* the scene_props were parsed so the base
        objects are available for luxcore_scene. Needs to happen before this method is called.
        """
        start_time = time()

        for duplis in instances.values():
            if duplis is None:
                # If duplis is None, then a non-exportable object like a curve with zero faces is being duplicated
                continue

            if duplis.get_count() == 0:
                # Only one instance was created (and is already present in the luxcore_scene), nothing to duplicate
                continue

            duplis.exported_obj.duplicate_count = duplis.get_count()

            for part in duplis.exported_obj.parts:
                src_name = part.lux_obj
                dst_name = src_name + "dupli"
                luxcore_scene.DuplicateObject(
                    src_name,
                    dst_name,
                    duplis.get_count(),
                    duplis.matrices,
                    duplis.object_ids,
                )

                # TODO: support steps and times (motion blur)
                # steps = 0 # TODO
                # times = array("f", [])
                # luxcore_scene.DuplicateObject(src_name, dst_name, count, steps, times, transformations)

        if stats:
            stats.export_time_instancing.value = time() - start_time

    def _debug_info(self):
        print("Objects in cache:", len(self.exported_objects))
        print("Meshes in cache:", len(self.exported_meshes))
        # for key, exported_mesh in self.exported_meshes.items():
        #     if exported_mesh:
        #         print(key, exported_mesh.mesh_definitions)
        #     else:
        #         print(key, "mesh is None")

    def _get_mesh_key(self, obj, use_instancing, is_viewport_render=True):
        if hasattr(obj.luxcore, "use_proxy") and obj.luxcore.use_proxy and obj.luxcore.scene_shape != "":
            # Proxy geometry comes from the file path, not from obj.data. Different objects can
            # point at different proxy files while sharing the same (irrelevant) placeholder mesh
            # data, e.g. linked duplicates - so the cache key must be based on the resolved path,
            # not on obj.data, or they would wrongly share/overwrite each other's cached geometry.
            # Hashed rather than used raw, since the path (slashes, drive letters, spaces) would
            # otherwise end up embedded in LuxCore SDL shape/property names.
            abspath = bpy.path.abspath(obj.luxcore.scene_shape)
            path_hash = hashlib.md5(abspath.encode("utf-8")).hexdigest()[:16]
            return "proxy_" + path_hash

        # Important: we need the data of the original object, not the evaluated one.
        # The instancing state has to be part of the key because a non-instanced mesh
        # has its transformation baked-in and can't be used by other instances.
        modified = utils.has_deforming_modifiers(obj.original)
        source = (
            obj.original.data
            if (use_instancing and not (modified or obj.type == "META"))
            else obj.original
        )
        key = utils.get_luxcore_name(source, is_viewport_render)
        if use_instancing:
            key += "_instance"
        return key

    def _convert_obj(
        self,
        exporter,
        dg_obj_instance,
        obj,
        depsgraph,
        luxcore_scene,
        scene_props,
        is_viewport_render,
        view_layer=None,
        engine=None,
    ):
        """Convert one DepsgraphObjectInstance amd keep track of it with self.exported_objects"""

        if obj.data is None:
            return None
        warn_about_subdivision_levels(obj)

        obj_key = utils.make_key_from_instance(dg_obj_instance)
        exported_stuff = None
        props = pyluxcore.Properties()

        if dg_obj_instance.show_self:
            if obj.type in MESH_OBJECTS:
                if obj.type == "CURVES" and not obj.data == None:
                    if obj.data.rna_type.name == "Hair Curves":
                        visible_to_cam = utils.visible_to_camera(
                            dg_obj_instance, is_viewport_render, view_layer
                        )
                        is_for_duplication = (
                            is_viewport_render or dg_obj_instance.is_instance
                        )
                        lux_shape = convert_hair_curves(
                            exporter,
                            depsgraph,
                            obj,
                            obj_key,
                            luxcore_scene,
                            is_for_duplication,
                        )
                        if lux_shape:
                            mat = obj.data.materials[0]
                            if mat:
                                node_tree = mat.luxcore.node_tree
                                if node_tree:
                                    lux_shape = define_shapes(
                                        lux_shape,
                                        node_tree,
                                        exporter,
                                        depsgraph,
                                        scene_props,
                                    )

                            self.exported_hair[obj_key] = lux_shape
                        if lux_shape:
                            # Check if object is in holdout layer collection
                            force_holdout = utils.is_holdout_object(obj.original, view_layer)
                            lux_mat, mat_props, node_tree = export_material(
                                obj, 0, exporter, depsgraph, is_viewport_render, force_holdout
                            )
                            scene_props.Set(mat_props)
                            set_hair_props(
                                scene_props,
                                lux_shape,
                                lux_shape,
                                lux_mat,
                                visible_to_cam,
                                is_for_duplication,
                                dg_obj_instance.matrix_world,
                                False,
                            )

                        # TODO handle case when exported_stuff is None
                        #  (we'll have to create a new ExportedObject just for the hair mesh)
                        if exported_stuff and lux_shape:
                            # Should always be the case because lights can't have particle systems
                            assert isinstance(exported_stuff, ExportedObject)
                            exported_stuff.parts.append(
                                ExportedPart(lux_shape, lux_shape, lux_mat)
                            )
                else:

                    exported_stuff = self._convert_mesh_obj(
                        exporter,
                        dg_obj_instance,
                        obj,
                        obj_key,
                        depsgraph,
                        luxcore_scene,
                        scene_props,
                        is_viewport_render,
                        view_layer,
                    )
                if exported_stuff:
                    props = exported_stuff.get_props()
            elif obj.type == "LIGHT":
                props, exported_stuff = light.convert_light(
                    exporter,
                    obj,
                    obj_key,
                    depsgraph,
                    luxcore_scene,
                    dg_obj_instance.matrix_world.copy(),
                    is_viewport_render,
                )

        # Convert hair
        for psys in obj.particle_systems:
            settings = psys.settings

            if (
                psys.particles
                and settings.type == "HAIR"
                and settings.render_type == "PATH"
            ):
                # Can't use the memory address of the psys as key because it changes
                # when the psys is updated (e.g. because some hair moves)
                is_for_duplication = (
                    is_viewport_render or dg_obj_instance.is_instance
                )
                psys_key = make_psys_key(obj, psys, is_for_duplication)
                lux_obj = make_hair_shape_name(obj_key, psys)
                visible_to_cam = utils.visible_to_camera(
                    dg_obj_instance, is_viewport_render, view_layer
                )
                mat_index = get_hair_material_index(psys)

                try:
                    lux_shape = self.exported_hair[psys_key]
                except KeyError:
                    lux_shape = convert_hair(
                        exporter,
                        obj,
                        obj_key,
                        psys,
                        depsgraph,
                        luxcore_scene,
                        scene_props,
                        is_viewport_render,
                        is_for_duplication,
                        dg_obj_instance.matrix_world,
                        visible_to_cam,
                        engine,
                    )
                    if lux_shape:
                        mat = get_material(obj, mat_index, depsgraph)
                        if mat:
                            node_tree = mat.luxcore.node_tree
                            if node_tree:
                                lux_shape = define_shapes(
                                    lux_shape,
                                    node_tree,
                                    exporter,
                                    depsgraph,
                                    scene_props,
                                )

                        self.exported_hair[psys_key] = lux_shape

                if lux_shape:
                    # Check if object is in holdout layer collection
                    force_holdout = utils.is_holdout_object(obj.original, view_layer)
                    lux_mat, mat_props, node_tree = export_material(
                        obj, mat_index, exporter, depsgraph, is_viewport_render, force_holdout
                    )
                    scene_props.Set(mat_props)
                    set_hair_props(
                        scene_props,
                        lux_obj,
                        lux_shape,
                        lux_mat,
                        visible_to_cam,
                        is_for_duplication,
                        dg_obj_instance.matrix_world,
                        settings.luxcore.hair.instancing == "enabled",
                    )

                # TODO handle case when exported_stuff is None
                #  (we'll have to create a new ExportedObject just for the hair mesh)
                if exported_stuff and lux_shape:
                    # Should always be the case because lights can't have particle systems
                    assert isinstance(exported_stuff, ExportedObject)
                    exported_stuff.parts.append(
                        ExportedPart(lux_obj, lux_shape, lux_mat)
                    )

        if exported_stuff:
            scene_props.Set(props)
            self.exported_objects[obj_key] = exported_stuff

        return exported_stuff

    def _convert_mesh_obj(
        self,
        exporter,
        dg_obj_instance,
        obj,
        obj_key,
        depsgraph,
        luxcore_scene,
        scene_props,
        is_viewport_render,
        view_layer,
    ):
        transform = dg_obj_instance.matrix_world
        use_instancing = _compute_use_instancing(exporter, dg_obj_instance, obj, is_viewport_render)

        mesh_key = self._get_mesh_key(obj, use_instancing, is_viewport_render)

        if use_instancing and mesh_key in self.exported_meshes:
            exported_mesh = self.exported_meshes[mesh_key]
            loaded_from_cache = True
        else:
            exported_mesh = mesh_converter.convert(
                obj,
                mesh_key,
                depsgraph,
                luxcore_scene,
                is_viewport_render,
                use_instancing,
                transform,
                exporter,
            )
            self.exported_meshes[mesh_key] = exported_mesh
            loaded_from_cache = False

        if exported_mesh:
            # Check if object is in holdout layer collection (like Cycles)
            # For instances, check the parent object (similar to visible_to_camera logic)
            check_obj = dg_obj_instance.parent if dg_obj_instance.is_instance else obj
            force_holdout = utils.is_holdout_object(check_obj.original, view_layer)

            mat_names = []
            for idx, (shape_name, mat_index) in enumerate(
                exported_mesh.mesh_definitions
            ):
                shape = shape_name
                lux_mat_name, mat_props, node_tree = export_material(
                    obj, mat_index, exporter, depsgraph, is_viewport_render, force_holdout
                )
                scene_props.Set(mat_props)
                mat_names.append(lux_mat_name)

                # Meshes in the cache already have the shapes added.
                # (This assumes that the instances use the same materials as the original mesh)
                if node_tree and not loaded_from_cache:
                    warn_about_missing_uvs(obj, node_tree)
                    shape = define_shapes(
                        shape, node_tree, exporter, depsgraph, scene_props
                    )

                exported_mesh.mesh_definitions[idx] = [shape, mat_index]

            obj_transform = transform.copy() if use_instancing else None
            # Diagnostic (2026-09-23): tracking down origin/scale corruption
            # on GN instance reappearance for objects with un-applied
            # location/scale. use_instancing=False bakes the object's own
            # transform into the cached mesh's vertex data (world-space,
            # obj_transform stays None); use_instancing=True keeps the mesh
            # in local space and sends obj_transform separately instead. If
            # the SAME mesh_key ends up cached once with use_instancing=False
            # (transform baked into vertices) and reused later with
            # use_instancing=True (transform ALSO applied via
            # obj_transform), the object's own transform would effectively
            # apply twice -- this print is meant to catch exactly that.
            print(f"  [diag] _convert_mesh_obj obj={obj.name!r} use_instancing={use_instancing} "
                  f"mesh_key={mesh_key!r} loaded_from_cache={loaded_from_cache} "
                  f"is_instance={dg_obj_instance.is_instance} "
                  f"transform_translation={tuple(round(x, 4) for x in transform.translation)} "
                  f"transform_scale={tuple(round(x, 4) for x in transform.to_scale())} "
                  f"obj_transform={'None' if obj_transform is None else 'SET'}")
            obj_id = utils.make_object_id(dg_obj_instance)

            visible = utils.visible_to_camera(
                dg_obj_instance, is_viewport_render, view_layer
            )

            # Holdout overrides indirect_only - holdout needs object to be visible to camera
            # to "cut a hole" in the film. In reflections/GI it will still be visible normally.
            if force_holdout:
                visible = True

            return ExportedObject(
                obj_key,
                exported_mesh.mesh_definitions,
                mat_names,
                obj_transform,
                visible,
                obj_id,
            )

    def diff(self, depsgraph):
        only_scene = len(depsgraph.updates) == 1 and isinstance(
            depsgraph.updates[0].id, bpy.types.Scene
        )
        return depsgraph.id_type_updated("OBJECT") and not only_scene

    def update(self, exporter, depsgraph, luxcore_scene, scene_props, context, view_layer=None):
        if view_layer is None:
            view_layer = depsgraph.view_layer_eval
        is_viewport_render = bool(context)
        redefine_objs_with_these_mesh_keys = []

        # Geometry updates (mesh edit, modifier edit etc.)
        if depsgraph.id_type_updated("OBJECT"):
            for dg_update in depsgraph.updates:
                if dg_update.is_updated_geometry and isinstance(
                    dg_update.id, bpy.types.Object
                ):
                    obj = dg_update.id
                    if not utils.is_obj_visible(obj) or (
                        context and not obj.visible_in_viewport_get(context.space_data)
                    ):
                        continue

                    if obj.type in MESH_OBJECTS:
                        if obj.type == "CURVES" and not obj.data == None:
                            if obj.data.rna_type.name == "Hair Curves":
                                obj_key = utils.make_key(obj)
                                del self.exported_hair[obj_key]
                        else:
                            # An object can be cached under up to two different
                            # mesh_key variants depending on context:
                            # use_instancing=True (shared local-space mesh, e.g.
                            # for a GN/particle dupli of this object) and
                            # use_instancing=False (singular object, world
                            # transform baked into the mesh). Only refresh
                            # whichever variant(s) are actually cached, each
                            # with its own correct transform -- previously this
                            # unconditionally used use_instancing=True (a
                            # leftover meant only for viewport, "Always
                            # instance in viewport so we can move objects
                            # around"), which for a use_instancing=False
                            # object computed the WRONG mesh_key. That wrong
                            # key still landed in redefine_objs_with_these_
                            # mesh_keys, which later forced a full re-convert
                            # of the object further down in this method --
                            # re-baking its world transform into the
                            # ALREADY-baked mesh a second time (observed as
                            # the object's scale/origin getting multiplied by
                            # itself on the frame some unrelated Geometry
                            # Nodes system first produced instances elsewhere
                            # in the scene -- see
                            # PERSISTENT_DATA_ANIMATION_NOTES.md).
                            for candidate_instancing in (False, True):
                                mesh_key = self._get_mesh_key(obj, candidate_instancing, is_viewport_render)
                                if mesh_key not in self.exported_meshes:
                                    continue

                                # if mesh_key not in self.exported_meshes:
                                # TODO this can happen if a deforming modifier is added
                                #  to an already-exported object. how to handle this case?

                                transform = None if (is_viewport_render or candidate_instancing) else obj.matrix_world
                                exported_mesh = mesh_converter.convert(
                                    obj,
                                    mesh_key,
                                    depsgraph,
                                    luxcore_scene,
                                    is_viewport_render,
                                    candidate_instancing,
                                    transform,
                                )

                                if exported_mesh:
                                    for i in range(
                                        len(exported_mesh.mesh_definitions)
                                    ):
                                        shape, mat_index = (
                                            exported_mesh.mesh_definitions[i]
                                        )
                                        mat = get_material(
                                            obj, mat_index, depsgraph
                                        )

                                        if mat:
                                            node_tree = mat.luxcore.node_tree
                                            if node_tree:
                                                shape = define_shapes(
                                                    shape,
                                                    node_tree,
                                                    exporter,
                                                    depsgraph,
                                                    scene_props,
                                                )

                                        exported_mesh.mesh_definitions[i] = (
                                            shape,
                                            mat_index,
                                        )

                                self.exported_meshes[mesh_key] = exported_mesh

                                # We arrive here not only when the mesh is edited, but also when the material
                                # of the object is changed in Blender. In this case we have to re-define all
                                # objects using this mesh (just the properties, the mesh is not re-exported).
                                redefine_objs_with_these_mesh_keys.append(mesh_key)

                        # Re-export hair systems of objects with updated geometry
                        for psys in obj.particle_systems:
                            settings = psys.settings

                            if (
                                psys.particles
                                and settings.type == "HAIR"
                                and settings.render_type == "PATH"
                            ):
                                # Can't use the memory address of the psys as key because it changes
                                # when the psys is updated (e.g. because some hair moves)
                                psys_key = make_psys_key(obj, psys, True)
                                del self.exported_hair[psys_key]
                    elif obj.type == "LIGHT":
                        obj_key = utils.make_key(obj)
                        props, exported_stuff = light.convert_light(
                            exporter,
                            obj,
                            obj_key,
                            depsgraph,
                            luxcore_scene,
                            obj.matrix_world.copy(),
                            is_viewport_render,
                        )
                        if exported_stuff:
                            self.exported_objects[obj_key] = exported_stuff
                            scene_props.Set(props)

        # TODO maybe not loop over all instances, instead only loop over updated
        #  objects and check if they have a particle system that needs to be updated?
        #  Would be better for performance with many particles, however I'm not sure
        #  we can find all instances corresponding to one particle system?

        # Currently, every update that doesn't require a mesh re-export happens here
        for dg_obj_instance in depsgraph.object_instances:
            if not supports_live_transform(dg_obj_instance.particle_system):
                continue

            obj = dg_obj_instance.object
            if not utils.is_instance_visible(dg_obj_instance, obj, context):
                continue

            obj_key = utils.make_key_from_instance(dg_obj_instance)
            # Same formula _convert_mesh_obj() uses -- must match, since this
            # decides whether we take the cheap transform-only update below or
            # fall through to a full _convert_obj() (which recomputes this
            # itself); using a different/wrong use_instancing here caused a
            # real bug, see the comment above the "Geometry updates" loop.
            use_instancing = _compute_use_instancing(exporter, dg_obj_instance, obj, is_viewport_render)
            mesh_key = self._get_mesh_key(obj, use_instancing, is_viewport_render)

            if (
                obj_key in self.exported_objects and obj.type != "LIGHT"
            ) and not mesh_key in redefine_objs_with_these_mesh_keys:
                exported_obj = self.exported_objects[obj_key]
                updated = False

                if exported_obj.transform != dg_obj_instance.matrix_world:
                    exported_obj.transform = (
                        dg_obj_instance.matrix_world.copy()
                    )
                    updated = True

                obj_id = utils.make_object_id(dg_obj_instance)
                if exported_obj.obj_id != obj_id:
                    exported_obj.obj_id = obj_id
                    updated = True

                visible = utils.visible_to_camera(
                    dg_obj_instance, is_viewport_render, view_layer
                )

                # Holdout overrides indirect_only
                check_obj = dg_obj_instance.parent if dg_obj_instance.is_instance else dg_obj_instance.object
                if utils.is_holdout_object(check_obj.original, view_layer):
                    visible = True

                if exported_obj.visible_to_camera != visible:
                    exported_obj.visible_to_camera = visible
                    updated = True

                if updated:
                    scene_props.Set(exported_obj.get_props())
            else:
                # Object is new and not in LuxCore yet, or it is a light, do a full export
                self._convert_obj(
                    exporter,
                    dg_obj_instance,
                    obj,
                    depsgraph,
                    luxcore_scene,
                    scene_props,
                    is_viewport_render,
                    view_layer,
                )

        # self._debug_info()
