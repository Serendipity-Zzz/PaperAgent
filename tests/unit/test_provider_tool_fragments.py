import pytest

from paperagent.providers import ToolCallFragmentAssembler


def test_fragmented_tool_arguments_are_reassembled_only_at_finish() -> None:
    fragments = ToolCallFragmentAssembler()
    fragments.add(0, call_id="call-1", name="math.double", arguments_fragment="{")
    fragments.add(0, arguments_fragment='"value":')
    fragments.add(0, arguments_fragment="4}")
    assert fragments.finish()[0].arguments == {"value": 4}


def test_invalid_or_incomplete_fragmented_tool_arguments_fail_closed() -> None:
    fragments = ToolCallFragmentAssembler()
    fragments.add(0, call_id="call-1", name="math.double", arguments_fragment="{")
    with pytest.raises(ValueError, match="invalid streamed"):
        fragments.finish()

    incomplete = ToolCallFragmentAssembler()
    incomplete.add(0, call_id="call-1", arguments_fragment="{}")
    with pytest.raises(ValueError, match="incomplete streamed"):
        incomplete.finish()
