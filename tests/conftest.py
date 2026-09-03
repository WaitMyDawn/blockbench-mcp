"""pytest 公共夹具：干净的内存项目 + 重置全局会话。"""

from __future__ import annotations

import pytest

from blockbench_mcp.document import BlockbenchProject
from blockbench_mcp.session import session


@pytest.fixture()
def project() -> BlockbenchProject:
    return BlockbenchProject(name="test", format_id="bedrock", texture_width=64, texture_height=64)


@pytest.fixture(autouse=True)
def _clean_session():
    session.reset()
    yield
    session.reset()

