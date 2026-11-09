"""Data Lens: automatic structural maps and exact bounded queries for local CSV/JSON/JSONL files."""

from __future__ import annotations

from .data_lens_middleware import make_middleware
from .data_lens_tools import (
    PROFILE_SCHEMA, PROFILE_TOOL, QUERY_SCHEMA, QUERY_TOOL, TOOLSET, make_handlers, profile_file, query_file,
)

__all__ = ["register", "profile_file", "query_file"]


def register(ctx) -> None:
    get_config = ctx.get_config
    profile_handler, query_handler = make_handlers(get_config)
    ctx.register_tool(name=PROFILE_TOOL, toolset=TOOLSET, schema=PROFILE_SCHEMA, handler=profile_handler,
                      description=PROFILE_SCHEMA["description"], emoji="🔎")
    ctx.register_tool(name=QUERY_TOOL, toolset=TOOLSET, schema=QUERY_SCHEMA, handler=query_handler,
                      description=QUERY_SCHEMA["description"], emoji="🔎")
    ctx.register_middleware("tool_execution", make_middleware(get_config))
