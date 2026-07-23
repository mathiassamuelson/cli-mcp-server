"""Tests for the pipeline parser and resolver."""

import pytest

from cli_mcp.catalog import ToolEntry, ToolRegistry
from cli_mcp.pipeline import (
    PipelineGrammarError,
    PipelineResolutionError,
    parse_pipeline,
    resolve_pipeline,
)


def _entry(name, *, pipe_stage=False):
    return ToolEntry(
        name=name, description="", binary_raw=name, binary=f"/usr/bin/{name}",
        prepend_args=[], timeout_seconds=10, max_bytes=8192,
        pipe_stage=pipe_stage, rules={"deny": [], "allow": ["*"]},
    )


@pytest.fixture
def registry():
    r = ToolRegistry()
    r.add(_entry("ps"))
    r.add(_entry("grep", pipe_stage=True))
    r.add(_entry("head", pipe_stage=True))
    r.add(_entry("awk", pipe_stage=True))
    return r


class TestParsePipeline:
    def test_single_segment(self):
        assert parse_pipeline("aux") == ["aux"]

    def test_multi_segment(self):
        assert parse_pipeline("aux | grep nginx | head -5") == [
            "aux", "grep nginx", "head -5",
        ]

    def test_quoted_pipe_not_separator(self):
        out = parse_pipeline('grep "a|b" file')
        assert out == ['grep "a|b" file']

    def test_single_quoted_pipe(self):
        out = parse_pipeline("grep 'a|b'")
        assert out == ["grep 'a|b'"]

    def test_empty_segment_rejected(self):
        with pytest.raises(PipelineGrammarError, match="empty"):
            parse_pipeline("ps aux | | head")

    @pytest.mark.parametrize(
        "bad",
        [
            "ps; rm -rf /",
            "ps && id",
            "ps || id",
            "ps > /tmp/x",
            "ps < /etc/passwd",
            "ps `id`",
            "ps $(id)",
            "ps & sleep",
            "ps\nmore",
        ],
    )
    def test_forbidden_metacharacters(self, bad):
        with pytest.raises(PipelineGrammarError):
            parse_pipeline(bad)

    def test_unterminated_quote(self):
        with pytest.raises(PipelineGrammarError, match="unterminated"):
            parse_pipeline('grep "unterminated')


class TestResolvePipeline:
    def test_lead_only(self, registry):
        ps = registry.get("ps")
        stages = resolve_pipeline(ps, ["aux"], registry)
        assert len(stages) == 1
        assert stages[0][0].name == "ps"
        assert stages[0][1] == "aux"

    def test_lead_and_pipe_stages(self, registry):
        ps = registry.get("ps")
        stages = resolve_pipeline(ps, ["aux", "grep nginx", "head -5"], registry)
        assert [s[0].name for s in stages] == ["ps", "grep", "head"]
        assert stages[1][1] == "nginx"
        assert stages[2][1] == "-5"

    def test_unknown_tool_in_pipe_position(self, registry):
        ps = registry.get("ps")
        with pytest.raises(PipelineResolutionError, match="unknown tool"):
            resolve_pipeline(ps, ["aux", "nope -x"], registry)

    def test_non_pipe_stage_in_pipe_position(self, registry):
        # ps is lead-only (not pipe_stage); using it downstream is rejected.
        ps = registry.get("ps")
        with pytest.raises(PipelineResolutionError, match="not a pipe stage"):
            resolve_pipeline(ps, ["aux", "ps fake"], registry)

    @pytest.mark.parametrize(
        "segment",
        ['"grep" nginx', "'grep' -v root", '"grep"', "'grep'"],
    )
    def test_quoted_tool_name_rejected(self, registry, segment):
        """Regression: shlex strips the quotes, so seg[len(tool_name):] landed
        mid-string and produced corrupted args ('p" nginx') that were then both
        filtered and executed."""
        ps = registry.get("ps")
        with pytest.raises(PipelineResolutionError, match="unquoted"):
            resolve_pipeline(ps, ["aux", segment], registry)

    def test_argument_less_pipe_stage_resolves_to_empty_args(self, registry):
        # `| grep` with no args must resolve cleanly; whether it is permitted
        # is then up to the tool's rules, not to the resolver.
        ps = registry.get("ps")
        stages = resolve_pipeline(ps, ["aux", "grep"], registry)
        assert stages[1][0].name == "grep"
        assert stages[1][1] == ""

    def test_lead_can_be_a_non_pipe_stage_tool(self, registry):
        # This is the important asymmetry: ps has pipe_stage=False, but it's
        # always allowed as the lead.
        ps = registry.get("ps")
        stages = resolve_pipeline(ps, ["aux"], registry)
        assert stages[0][0] is ps
