import bpy
from bpy.props import PointerProperty, BoolProperty, FloatProperty, IntProperty, StringProperty
from bpy.types import PropertyGroup
from .hair import LuxCoreHair

DESC_VISIBLE_TO_CAM = (
    "If disabled, the object will not be visible to camera rays. "
    "Note that it will still be visible in indirect light, shadows and reflections"
)
DESC_MOTION_BLUR = "Export this object as instance if object motion blur is enabled in camera settings"
DESC_OBJECT_ID = (
    "ID for Object ID AOV. If -1 is set, the object name is hashed to a number and used as ID. "
    "The ID can be accessed from the Object ID node in material node trees. "
    "Note that the random IDs of LuxCore can be greater than 32767 "
    "(the ID Mask node in the compositor can't handle those numbers)"
)
DESC_EXCLUDE_FROM_RENDER = (
    "The object will be excluded from render. "
    "Useful if you need objects to render for other engines, but not for LuxCore"
)
DESC_ALWAYS_REEXPORT = (
    "Force this object's instances (or, if this is a Geometry Nodes / "
    "Collection Instance parent, everything it instances) to be "
    "re-checked and refreshed every frame during a Persistent Data "
    "(Animation) render -- mesh/material data is still cached, only "
    "the instance list and transforms are re-evaluated. Why this is "
    "manual, not automatic: we tried inferring this from Blender's own "
    "dependency graph (depsgraph.updates / is_updated_geometry) instead "
    "of requiring this flag, and found it unreliable for this purpose -- "
    "it can report a change too broadly (an unrelated Geometry Nodes "
    "system producing new instances elsewhere in the scene can spuriously "
    "flag other, untouched objects as 'updated' on the same frame) or "
    "miss a real instance-count change on the group that actually needs "
    "it, either of which is worse than doing nothing. So Persistent Data "
    "(Animation) uses the same model as other engines built around a "
    "persistent render session (e.g. Octane): every object is treated as "
    "completely static by default once exported, and nothing is "
    "re-checked unless you explicitly say so with this flag -- no "
    "inference, no guessing from change-tracking metadata. Enable this "
    "on Geometry Nodes setups whose instance count changes over the "
    "animation (instances appearing/disappearing) or on any object that "
    "otherwise needs to move/change after the first frame. Leave "
    "disabled for anything static, to keep it on the faster batched "
    "export path"
)


class LuxCoreObjectProps(PropertyGroup):
    visible_to_camera: BoolProperty(
        name="Visible to Camera", default=True, description=DESC_VISIBLE_TO_CAM
    )
    exclude_from_render: BoolProperty(
        name="Exclude from Render",
        default=False,
        description=DESC_EXCLUDE_FROM_RENDER,
    )
    enable_motion_blur: BoolProperty(
        name="Motion Blur", default=True, description=DESC_MOTION_BLUR
    )
    always_reexport: BoolProperty(
        name="Always Re-check (Persistent Data)",
        default=False,
        description=DESC_ALWAYS_REEXPORT,
    )
    use_proxy: BoolProperty(
        name="Use Proxy",
        default=False,
        description="Load geometry from the Proxy File below instead of exporting the Blender mesh. "
                     "If disabled, the Proxy File path is ignored even if set"
    )
    scene_shape: StringProperty(
        name="Proxy File", default="", subtype="FILE_PATH",
        description="Path to an external PLY file on disk, used as a proxy instead of exporting the Blender "
                     "mesh. If the filename ends in a number (e.g. 'tree007.ply'), sibling files in the same "
                     "folder sharing the same base name are auto-detected and used as additional materials, "
                     "with the number mapped to the material slot index (as produced by LuxCore's "
                     "'Only write LuxCore scene' option)"
    )
    proxy_apply_modifiers: BoolProperty(
        name="Apply Modifiers",
        default=True,
        description="When generating a proxy, export the mesh after modifiers (Subdivision, Bevel, ...) "
                     "are applied. Disable this if the object's own modifier stack scatters/instances other "
                     "objects (e.g. a Geometry Nodes distributor), since applying modifiers would realize "
                     "all those instances into one merged mesh instead of keeping them as instances"
    )
    id: IntProperty(
        name="Object ID",
        default=-1,
        min=-1,
        soft_max=32767,
        description=DESC_OBJECT_ID,
    )
    hair: PointerProperty(
        name="LuxCore Hair Curve Settings",
        description="LuxCore hair curve settings",
        type=LuxCoreHair,
    )

    @classmethod
    def register(cls):
        bpy.types.Object.luxcore = PointerProperty(
            name="LuxCore Object Settings",
            description="LuxCore object settings",
            type=cls,
        )

    @classmethod
    def unregister(cls):
        del bpy.types.Object.luxcore
