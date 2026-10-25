# D-810 for Binary Ninja

Simplifies mixed boolean-arithmetic (MBA), opaque predicates and control-flow flattening
in Binary Ninja. Works through IL; nothing is written to the binary.

- Cleans arithmetic, encoded comparisons and flag conditions.
- Recovers flattened dispatchers and arithmetic helper chains.
- Removes proven dead branches, loops and private stack traffic.
- Recovers bounded loop predicates and constant buffers.

Supports x86, x64 and ARM64 binaries in PE, ELF and Mach-O. Tested with Binary Ninja 6.0
Personal on macOS arm64. Windows and Linux host installations remain unverified.

<details>
<summary>Example: an OLLVM-obfuscated function reduced from 399 HLIL lines to 4</summary>

Before:

<img src="imgs/before.png" alt="Before deobfuscation" width="760">

After:

<img src="imgs/after.png" alt="After deobfuscation" width="760">

</details>

## Install

Requires Binary Ninja 6.0 and Z3 in its Python 3.13 environment. On macOS:

```sh
git clone --branch v0.2.0-beta.1 https://github.com/MZHeader/binja-d810
ln -s "$PWD/binja-d810/binja_d810" "$HOME/Library/Application Support/Binary Ninja/plugins/binja_d810"

BN_SITE="$HOME/Library/Application Support/Binary Ninja/python313/site-packages"
uv pip install --python python3.13 --target "$BN_SITE" z3-solver
uv pip install --python python3.13 --target "$BN_SITE" --no-deps d810-cobra==0.1.7  # optional
```

Restart Binary Ninja after installation.

## Use

Open **Plugins > D810 > Enable for Current Function** or **Enable for This Binary**.
The plugin is off by default. Start with individual functions on large binaries.

Use **Show Function Report** to inspect changes and skipped proofs. The log prints
**D810: analysis complete** when analysis finishes. Disable commands undo D810's owned changes.

Settings are under **Settings > D-810**. The default budget is 20 seconds per function;
unsupported cases and incomplete proofs are left alone.

## Documentation

- [Usage, settings and reports](docs/usage.md)
- [Passes, proof requirements and coverage limits](docs/reference.md)
- [Building fixtures and running tests](docs/development.md)

## Credits and license

[AGPL-3.0](LICENSE). Inspired by eShard's [D-810](https://github.com/joydo/d810), using rules
from [d810-ng](https://github.com/w00tzenheimer/d810-ng). Optional
[CoBRA](https://github.com/trailofbits/CoBRA) is Apache-2.0 and is not bundled.
