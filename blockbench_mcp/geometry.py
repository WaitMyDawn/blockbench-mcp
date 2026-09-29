"""Shared static world transforms for Blockbench's default ZYX Euler order."""
from __future__ import annotations

import math
from .document import BlockbenchProject, Element
from .errors import ValidationError


def rotate_point(origin: list[float], rotation: list[float], point: list[float]) -> list[float]:
    # THREE.Euler(..., 'ZYX') composes Rz * Ry * Rx: apply X, then Y, then Z.
    rx, ry, rz = (math.radians(v) for v in rotation)
    x, y, z = (point[i] - origin[i] for i in range(3))
    y, z = y * math.cos(rx) - z * math.sin(rx), y * math.sin(rx) + z * math.cos(rx)
    x, z = x * math.cos(ry) + z * math.sin(ry), -x * math.sin(ry) + z * math.cos(ry)
    x, y = x * math.cos(rz) - y * math.sin(rz), x * math.sin(rz) + y * math.cos(rz)
    return [x + origin[0], y + origin[1], z + origin[2]]


def world_cubes(project: BlockbenchProject) -> list[tuple[Element, list[list[float]]]]:
    """Transform each exported cube's eight corners, child first then ancestors."""
    groups = {g.uuid: g for g in project.groups}
    elements = {e.uuid: e for e in project.elements}
    results = []

    def walk(uids, ancestors, active):
        for uid in uids:
            if uid in groups:
                g = groups[uid]
                if uid in active:
                    raise ValidationError(f"骨骼层级存在环：{g.name}")
                if g.export:
                    walk(g.children, ancestors + [g], active | {uid})
                continue
            cube = elements.get(uid)
            if cube is None or not cube.export:
                continue
            corners = []
            for x in (cube.from_[0], cube.to[0]):
                for y in (cube.from_[1], cube.to[1]):
                    for z in (cube.from_[2], cube.to[2]):
                        p = rotate_point(cube.origin, cube.rotation, [x, y, z])
                        for g in reversed(ancestors):
                            p = rotate_point(g.origin, g.rotation, p)
                        corners.append(p)
            results.append((cube, corners))

    walk(project.root_children, [], set())
    return results
