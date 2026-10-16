# binja-d810

d810-style deobfuscation of mixed boolean-arithmetic (MBA) expressions for Binary Ninja, on x86 (32-bit), x64 and ARM64, for PE, ELF and Mach-O. AGPL-3.0-only.

The plugin rewrites MBA expressions during analysis, in two passes enabled by one setting:

- **MLIL pass.** Rewrites expressions in MLIL, rebuilding expressions split across instructions by following SSA definitions.
- **HLIL pass.** Rewrites, in HLIL, what MLIL could not express. Typically, optimized code overwrites the registers holding the inputs before the instruction that combines them; HLIL still reads the original variables.

Each candidate is simplified in two tiers:

- **Tier 1:** d810's rule corpus. 156 of 179 rules are Z3-proven at one or more bit widths, and a rule only fires at the widths where it was proven.
- **Tier 2:** generic MBA simplifiers. That means CoBRA when it is installed, plus a built-in linear-MBA simplifier.

Every rewrite is checked for equivalence before it is applied: random evaluation always, plus an exact signature proof for linear MBAs and a Z3 proof for everything else. Each rewrite records whether it was proven or only tested by sampling, and why. Expressions containing loads, calls or other effects are never rewritten through: only their pure parts are. Nothing is written to the binary or the database.

Design: `docs/design/specs/2026-10-15-binja-d810-v2-design.md` (v1: `docs/design/specs/2026-10-15-binja-d810-core-framework-design.md`). Coverage and known gaps: `docs/design/reports/2026-10-15-v2.0-coverage.md`.

## Install

Requires Binary Ninja 6.0 (tested on 6.0.10601 Personal, bundled Python 3.13) on macOS arm64. The binaries analyzed can be any architecture Binary Ninja lifts; the corpus covers x86, x64 and ARM64.

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
4. Restart Binary Ninja. The log shows `binja-d810: activities extension.binja_d810.mba, extension.binja_d810.mba_hlil registered`.

## Use

The activity is **off by default**. Turn it on in one of two ways:

- **One function:** right-click the function, choose **Function Analysis**, then enable **D-810: simplify MBA expressions**. The function reanalyzes.
- **A whole binary:** in **Open with Options**, enable **D-810: simplify MBA expressions** (setting `extension.binja_d810.mba`).

The same setting enables both passes; the HLIL pass has no switch of its own.

Each function gets a time budget (`binja_d810.functionSeconds`, 5 s by default) shared by both passes of one analysis run. When it runs out, rewrites already verified are applied and nothing new is attempted; the report says "budget exhausted". The budget never exceeds half of Binary Ninja's own per-function limit (`analysis.limits.maxFunctionAnalysisTime`), so the plugin cannot make Binary Ninja abandon a function. Enable D-810 per function on heavily obfuscated code: with a whole binary enabled, many functions are analyzed in parallel and compete for Python, so more of them hit the budget. Functions without mixed arithmetic and bitwise operations are skipped almost instantly.

### Settings

Settings are under **Settings > D-810**. All of them can be set globally or per binary.

| Setting | Default | Meaning |
|---|---|---|
| `binja_d810.enabled` | on | Kill switch. When off, nothing is rewritten. |
| `binja_d810.tier2` | on | Use CoBRA and the linear-MBA simplifier. |
| `binja_d810.maxVariables` | 4 | CoBRA skips subexpressions with more distinct variables. The linear simplifier counts atoms instead (sub-expressions it treats as opaque), up to 6. |
| `binja_d810.maxNodes` | 200 | Larger expressions are skipped and logged as "too complex". |
| `binja_d810.maxIterations` | 10 | Maximum rewrite passes per function. |
| `binja_d810.disabledRules` | none | d810 rule names to skip. |
| `binja_d810.skipFunctions` | none | Function start addresses (hex) never rewritten. |
| `binja_d810.functionSeconds` | 5 | Time budget per function, shared by both passes (capped at half of `analysis.limits.maxFunctionAnalysisTime`). |

To set the number settings from Python, use `Settings().set_integer`. Binary Ninja ignores a value stored with `set_double` for these keys.

### What happened to a function

Each rewrite is logged to the Binary Ninja log under `binja_d810`, with the pass (`mlil` or `hlil`), the address, the rule or backend that produced it, how it was verified (`proven by signature`, `proven by z3`, or `tested by sampling` with the reason no proof was available) and the before and after expressions. Skipped expressions are logged at debug level, including roots left alone because they contain an effect. To see one function's full report for both passes, with timing, run this in the Python console, then reanalyze the function:

```python
current_function.request_debug_report("d810")
current_function.reanalyze()
```

If anything goes wrong while a function is being rewritten, all of that pass's rewrites are rolled back and that IL is kept as Binary Ninja produced it; the other pass's verified rewrites stand.

## Test

Unit tests need no Binary Ninja:

```bash
uv sync
uv run pytest
```

The Binary Ninja tests build the samples, then run pytest inside a fresh Binary Ninja instance. A window opens and closes by itself. They need pytest installed in Binary Ninja's Python (`uv pip install ... --target "$BN_SITE" pytest`), and Homebrew LLVM's `lld-link` and `ld.lld` for the PE and ELF builds.

```bash
tests/samples/build.sh                       # ARM64 Mach-O, x64 PE, i686 PE, x64 ELF, each at -O0 and -O2
scripts/binja_pytest.sh                      # everything (about 25 minutes)
scripts/binja_pytest.sh tests/binja -k x86_64-pe   # one architecture: arm64, x86_64-pe, i686-pe, x86_64-elf
```

`.binja-test/coverage.json` holds the latest per-build, per-category coverage, including how each final HLIL was validated (`tests/binja/oracle.py`: proven, exhaustive, sampled).

Opt-in suites, skipped by default:

- `BINJA_D810_SPIKE=1 scripts/binja_pytest.sh tests/binja/spikes` reruns the HLIL and formats spikes.
- Real samples, static analysis only: `BINJA_D810_SAMPLES=<dir> scripts/real_samples.sh <label>` loads each binary in its own Binary Ninja process and writes `.local/real/<label>/<sample>.json`. `BINJA_D810_PLUGIN=<dir>` measures another plugin revision (for example a `v1.0` worktree) in an isolated Binary Ninja user directory. Nothing under `.local/` is committed.

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
