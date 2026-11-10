"""Native curve metadata must survive document-engine edits and saves."""
import copy

import pytest

from blockbench_mcp.codecs import bbmodel
from blockbench_mcp.document import BlockbenchProject
from blockbench_mcp.errors import ValidationError


def fixture():
    project = BlockbenchProject(format_id="free")
    project.add_group("hinge_omega")
    project.add_animation("motion/unrelated_17")
    return project


def test_bezier_handles_metadata_and_precise_time_survive_roundtrip():
    project = fixture()
    metadata = {"bezier_left_time": [-.123, -.2, -.3], "bezier_right_time": [.1, .2, .3],
                "bezier_left_value": [-12, 3, 7], "bezier_right_value": [11, -4, 9],
                "bezier_linked": False, "editor_extension": {"label": "opaque"}}
    key = project.add_keyframe("motion/unrelated_17", "hinge_omega", "rotation", .123456789,
                               [7, 19, 31], interpolation="bezier", extra_fields=metadata)
    restored = bbmodel.loads(bbmodel.dumps(project))
    actual = next(iter(restored.animations[0].animators.values())).keyframes[0]
    assert actual.time == key.time
    assert all(actual.extra_fields[name] == value for name, value in metadata.items())
    metadata["bezier_right_value"][0] = 999
    assert key.extra_fields["bezier_right_value"][0] == 11


def test_value_edit_preserves_handles_until_explicitly_cleared():
    project = fixture()
    project.add_keyframe("motion/unrelated_17", "hinge_omega", "rotation", 0, [1, 2, 3],
                         interpolation="bezier", extra_fields={"bezier_right_time": [.2, .2, .2]})
    edited = project.add_keyframe("motion/unrelated_17", "hinge_omega", "rotation", 0, [4, 5, 6], interpolation="bezier")
    assert edited.extra_fields["bezier_right_time"] == [.2, .2, .2]
    cleared = project.add_keyframe("motion/unrelated_17", "hinge_omega", "rotation", 0, [4, 5, 6], extra_fields={})
    assert cleared.extra_fields == {}


@pytest.mark.parametrize("metadata", [
    {"bezier_right_time": [-1, 0, 0]}, {"bezier_left_time": [1, 0, 0]},
    {"bezier_left_value": [float("nan"), 0, 0]}, {"bezier_right_value": [0, 1]},
    {"bezier_linked": "false"}, {"time": 10},
])
def test_invalid_metadata_cannot_mutate_existing_animation(metadata):
    project = fixture()
    before = copy.deepcopy(project.__dict__)
    with pytest.raises(ValidationError):
        project.add_keyframe("motion/unrelated_17", "hinge_omega", "rotation", 0, [1, 2, 3], extra_fields=metadata)
    assert project.__dict__ == before


def test_tools_create_read_and_bulk_edit_expose_independent_curve_metadata():
    from blockbench_mcp import tools
    tools.project_create("numeric_curve_contract", format="free")
    tools.bone_create("joint_tau")
    tools.animation_create("motion/curve_53")
    metadata = {"bezier_right_time": [.25, .25, .25], "bezier_linked": False}
    created = tools.keyframe_add("motion/curve_53", "joint_tau", "rotation", 0, [12, 3, 7],
                                 interpolation="bezier", extra_fields=metadata)
    assert created["data"]["keyframe"]["extra_fields"] == metadata
    listed = tools.keyframe_list()["data"]["animations"][0]["bones"]["joint_tau"][0]
    assert listed["extra_fields"] == metadata
    listed["extra_fields"]["bezier_right_time"][0] = 999
    assert tools.keyframe_list()["data"]["animations"][0]["bones"]["joint_tau"][0]["extra_fields"] == metadata
    tools.keyframes_add_bulk([{"animation": "motion/curve_53", "bone": "joint_tau", "channel": "rotation",
                              "time": 1, "values": [24, 6, 14], "interpolation": "bezier",
                              "extra_fields": {"bezier_left_time": [-.25, -.25, -.25]}}])
    assert len(tools.keyframe_list()["data"]["animations"][0]["bones"]["joint_tau"]) == 2
