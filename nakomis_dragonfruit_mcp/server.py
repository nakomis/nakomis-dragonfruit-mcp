#!/usr/bin/env python3
"""nakomis-dragonfruit-mcp: headless resin print preparation with DragonFruit's engine.

Unofficial. Not affiliated with or endorsed by the Open Resin Alliance or the
DragonFruit project.

Every tool wraps `dragonfruit-cli`, `dragonfruit-ts-cli` or our own
`dragonfruit-mcp-tools` as a subprocess and returns a typed result with a
`warnings` list: anything the caller must hear goes there rather than being
dropped. The tools themselves live in `nakomis_dragonfruit_mcp/tools/`.
"""

from __future__ import annotations

from nakomis_dragonfruit_mcp.app import mcp
from nakomis_dragonfruit_mcp.tools import (  # noqa: F401
    engine,
    hollow,
    islands,
    mesh,
    printfile,
    slicing,
)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
