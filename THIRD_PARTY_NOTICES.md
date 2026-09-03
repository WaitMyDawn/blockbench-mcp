# Third-Party Notices

This project (`blockbench-mcp`) is licensed under the **MIT License** (see
[LICENSE](LICENSE), Copyright (c) 2026 Yagen).

The sections below list third-party dependencies and bundled sample assets,
together with their licenses and attributions. All dependency licenses here are
permissive (MIT / BSD / HPND). The only GPL-licensed item is a build-time tool
that carries a special exception; it does not impose copyleft on this project's
source.

---

## 1. Runtime dependencies

These are installed as package dependencies (via `pyproject.toml`) and are not
vendored into the repository. Their license texts are distributed with the
packages themselves.

| Package | Version (as tested) | License |
|---|---|---|
| `mcp` | 1.29.1 | MIT |
| `pillow` | 12.3.0 | HPND (MIT-compatible, permissive) |
| `uvicorn` (transitive via `mcp[cli]`) | 0.52.4 | BSD-3-Clause |
| `fastmcp` + other `mcp[cli]` transitive deps | — | MIT / Apache-2.0 / BSD |

No runtime dependency uses GPL / AGPL / LGPL.

## 2. Development dependencies

| Package | License |
|---|---|
| `pytest` | MIT |
| `lzstring` | MIT |

## 3. Build dependency

| Package | License | Note |
|---|---|---|
| `pyinstaller` | GPL-2.0-or-later, **with a special bootloader exception** | The exception permits bundling an application into a PyInstaller executable without relicensing the application's source. It is used only at build time. |

## 4. Bundled sample assets (`examples/models/`)

These are complete `.bbmodel` files shipped in `examples/models/` and registered
in `examples/catalog.json`. Their licenses are per-asset and **not** covered by
this project's MIT license unless stated otherwise.

| Sample | Author | License | Note |
|---|---|---|---|
| `polar_bear` | GodaOo | MIT | Retain the original copyright / permission notice. |
| `bettermodel_demon_knight` | toxicity188 (BetterModel) | MIT | Retain the original copyright / permission notice. |
| `img2bb_chimpanzee` | orca-gamedev (img2blockbench) | MIT | Retain the original copyright / permission notice. |
| `img2bb_coyote` | orca-gamedev (img2blockbench) | MIT | Retain the original copyright / permission notice. |
| `img2bb_elephant` | orca-gamedev (img2blockbench) | MIT | Retain the original copyright / permission notice. |

> For the MIT sample assets above, the upstream copyright notice and permission
> notice must be retained in distributions. See `examples/catalog.json` for the
> per-sample records.

## 5. Project-owned asset

- `redeemer` — **© Yagen, All Rights Reserved (ARR).** This is an original asset
  authored by the project owner, and is **not** distributed under the MIT license.
  No third-party attribution is required. It is intentionally excluded from the
  MIT grant above.

## 6. Trademarks

- "Blockbench" is a trademark of the Blockbench project. This project uses the
  name only to describe interoperability / compatibility ("Blockbench MCP server")
  and does not claim affiliation with or endorsement by the Blockbench team.

---

_Generated for the `blockbench-mcp` repository. If you add new third-party
assets or dependencies, update this notice accordingly._
