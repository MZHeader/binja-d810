# binja-d810

d810-style deobfuscation of mixed boolean-arithmetic (MBA) expressions for Binary Ninja, ARM64 first. AGPL-3.0-only.

The plugin adds a function-analysis activity that rewrites MBA expressions in MLIL during analysis, so HLIL shows the simplified code. Expressions split across instructions are rebuilt by following SSA definitions. Each candidate is simplified in two tiers:

- **Tier 1:** d810's rule corpus. 156 of 179 rules are Z3-proven at one or more bit widths, and a rule only fires at the widths where it was proven.
- **Tier 2:** generic MBA simplifiers. That means CoBRA when it is installed, plus a built-in linear-MBA simplifier.

Every rewrite is checked for equivalence before it is applied: random evaluation always, plus an exact signature proof for linear MBAs and a Z3 proof for everything else. Nothing is written to the binary or the database.

Design: `docs/design/specs/2026-10-15-binja-d810-core-framework-design.md`. Coverage and known gaps: `docs/design/reports/2026-10-15-v1-coverage.md`.

## Install

Requires Binary Ninja 6.0 (tested on 6.0.10601 Personal, bundled Python 3.13) on macOS arm64.

1. Link the plugin into Binary Ninja's plugin folder:
   ```bash
   ln -s "$PWD/binja_d810" "$HOME/Library/Application Support/Binary Ninja/plugins/binja_d810"
   ```
2. Install Z3 into Binary Ninja's Python. It is used to prove non-linear rewrites; without it those rewrites are checked by random sampling only. Use any Python 3.13 interpreter, since Binary Ninja's own is 3.13:
   ```bash
   BN_SITE="$HOME/Library/Application Support/Binary Ninja/python313/site-packages"
   uv pip install --python python3.13 --target "$BN_SITE" z3-solver
   ```
3. Optional, recommended: install CoBRA for polynomial MBA. The `d810-cobra` wheel ships Trail of Bits' CoBRA solver. `--no-deps` skips its unreleased `d810-ng` dependency, which this plugin does not need:
   ```bash
   uv pip install --python python3.13 --no-deps --target "$BN_SITE" d810-cobra==0.1.7
   ```
4. Restart Binary Ninja. The log shows `binja-d810: activity extension.binja_d810.mba registered`.

## Use

The activity is **off by default**. Turn it on in one of two ways:

- **One function:** right-click the function, choose **Function Analysis**, then enable **D-810: simplify MBA expressions**. The function reanalyzes.
- **A whole binary:** in **Open with Options**, enable **D-810: simplify MBA expressions** (setting `extension.binja_d810.mba`).

On heavily obfuscated code, analysis costs roughly 0.2 s per function. On a large binary, enable it per function rather than for the whole binary. Functions without mixed arithmetic and bitwise operations are skipped almost instantly.

### Settings

Settings are under **Settings > D-810**. All of them can be set globally or per binary.

| Setting | Default | Meaning |
|---|---|---|
| `binja_d810.enabled` | on | Kill switch. When off, nothing is rewritten. |
| `binja_d810.tier2` | on | Use CoBRA and the linear-MBA simplifier. |
| `binja_d810.maxVariables` | 4 | Tier 2 skips subexpressions with more distinct variables. |
| `binja_d810.maxNodes` | 200 | Larger expressions are skipped and logged as "too complex". |
| `binja_d810.maxIterations` | 10 | Maximum rewrite passes per function. |
| `binja_d810.disabledRules` | none | d810 rule names to skip. |
| `binja_d810.skipFunctions` | none | Function start addresses (hex) never rewritten. |

To set the number settings from Python, use `Settings().set_integer`. Binary Ninja ignores a value stored with `set_double` for these keys.

### What happened to a function

Each rewrite is logged to the Binary Ninja log under `binja_d810`, with the address, the rule or backend that produced it, and the before and after expressions. Skipped expressions are logged at debug level. To see one function's full report, run this in the Python console, then reanalyze the function:

```python
current_function.request_debug_report("d810")
current_function.reanalyze()
```

If anything goes wrong while a function is being rewritten, all of that function's rewrites are rolled back and the original MLIL is kept.

## Test

Unit tests need no Binary Ninja:

```bash
uv sync
uv run pytest
```

The Binary Ninja tests build the ARM64 sample dylibs, then run pytest inside a fresh Binary Ninja instance. A window opens and closes by itself, and the run takes about 2–3 minutes. They need pytest installed in Binary Ninja's Python (`uv pip install ... --target "$BN_SITE" pytest`).

```bash
tests/samples/build.sh
scripts/binja_pytest.sh                      # or: scripts/binja_pytest.sh tests/binja/test_samples.py
```

`.binja-test/coverage.json` holds the latest per-category coverage.

## Rules

`binja_d810/third_party/d810/` is a verbatim subset of d810-ng's MBA rule language and rules. Only the imports are re-rooted. The files there without a "Vendored from" header are small hand-written stand-ins for d810's IDA-dependent modules. To update:

```bash
uv run python scripts/vendor_d810.py      # re-download from the pinned d810-ng commit
uv run python scripts/verify_rules.py     # re-prove every rule at 8/16/32/64 bits (about a minute)
```

`binja_d810/rules/verified_rules.json` records, for each rule, the widths it was proven at, or why it is not used.

## License

AGPL-3.0-only (see `LICENSE`).

- **d810-ng.** The MBA rule language and rules come from [d810-ng](https://github.com/w00tzenheimer/d810-ng) (AGPL-3.0), commit `2e59ab4ce18e5e82911880137d1285d95279981a`. d810-ng is a continuation of eShard's [D-810](https://github.com/joydo/d810). The idea of this plugin, and its name, come from D-810.
- **CoBRA.** The optional Tier 2 solver is [CoBRA](https://github.com/trailofbits/CoBRA) (Apache-2.0), used through the `d810-cobra` package (MIT). It is not distributed with this repository.
