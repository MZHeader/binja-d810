# D-810 for Binary Ninja

> Inspired by [D-810](https://github.com/joydo/d810) by eShard, with MBA rules from
> [d810-ng](https://github.com/w00tzenheimer/d810-ng).

A Binary Ninja plugin that cleans up mixed boolean-arithmetic (MBA) obfuscation, opaque
predicates and control-flow flattening. It turns junk like `(x ^ y) + 2 * (x & y)` back into
`x + y`, folds conditions that always go the same way (OLLVM-style bogus control flow) so Binary
Ninja drops the dead branches, and turns flattened state machines back into branches and loops.
Nothing is written to the binary.

Here's an export from an OLLVM-obfuscated DLL before (399 lines of HLIL):

<img src="imgs/before.png" alt="Before" width="760">

And after (4 lines):

<img src="imgs/after.png" alt="After" width="760">

## What it handles

* MBA expressions, through D-810's rules, a built-in linear MBA simplifier, and
  [CoBRA](https://github.com/trailofbits/CoBRA) if you have it installed
* OLLVM-style instruction substitution, like `(x & y) | (x ^ y)` for `x | y`
* Opaque predicates, including ones hidden behind long chains of masked constants, ones that
  only hold on every path into a merge, values carried round loops, and the classic
  number-theoretic ones like `7 * y * y - 1 != x * x`
* Divisions that can never trap, like `2 / (x * x + 1)`
* Jumps and calls through encoded tables of code pointers (Hikari-style indirect branches and
  calls), including jumps back into the same function, and identity wrappers around them,
  when no visible code writes the table
* Constants encoded against a global key whose bytes are proven unwritten, like `x ^ key ^ 0x3c1d`.
  This includes bounded writes to neighboring buffers and helpers that only read the pointer.
* Control-flow flattening: state machines driven by compare trees or jump tables, including
  nested loops, and machines split across functions, where the dispatcher calls itself for the
  next state or calls a small function per state that returns through a stub back to it. Large
  handlers and their loops stay in place while the dispatcher is removed. Self-calls and outlined
  handlers can be recovered together, including dispatchers with large argument lists and
  undeclared register inputs that the handlers provably preserve. Known register and stack
  inputs can also be carried across handlers when their values and frame layout are proven
* Junk loops that only count a variable up to some value and then throw it away
* x86, x64 and ARM64 binaries (PE, ELF and Mach-O)

Every rewrite is checked before it's applied. Conditions are only folded when Z3 proves them,
and D-810's rules are only used at the bit widths Z3 proved them for. It runs in two passes,
MLIL then HLIL, and the HLIL pass picks up whatever MLIL couldn't express. Unflattening happens
in between, by copying code rather than guessing, so the program's behaviour doesn't change.

## Requirements

* Binary Ninja 6.0 (tested on 6.0 Personal, macOS arm64)
* `z3-solver` in Binary Ninja's Python
* Optional: CoBRA, through the `d810-cobra` package

## Install

Clone it and link it into your plugins folder:

```sh
git clone https://github.com/MZHeader/binja-d810
ln -s "$PWD/binja-d810/binja_d810" "$HOME/Library/Application Support/Binary Ninja/plugins/binja_d810"
```

Then install Z3 (and CoBRA if you want it) into Binary Ninja's Python, which is 3.13:

```sh
BN_SITE="$HOME/Library/Application Support/Binary Ninja/python313/site-packages"
uv pip install --python python3.13 --target "$BN_SITE" z3-solver
uv pip install --python python3.13 --target "$BN_SITE" --no-deps d810-cobra==0.1.7   # optional
```

Restart Binary Ninja. You should see `binja-d810: activities ... registered` in the log.

## Usage

It's off by default. To run it on one function, right-click it, go to **Function Settings** and
tick **D-810: simplify MBA expressions**. The function reanalyzes straight away.

<img src="imgs/function-settings.png" alt="Function Settings menu" width="520">

To run it on a whole binary, tick the same option in **File > Open with Options** before opening
it. Functions without obfuscation are skipped almost instantly, but heavily obfuscated ones can
use their whole time budget each, so on big binaries it's often quicker to enable it only for the
functions you care about.

Every rewrite is logged under `binja_d810`. For a full report on one function, run this in the
Python console:

```python
current_function.request_debug_report("d810")
current_function.reanalyze()
```

## Settings

Everything is under **Settings > D-810** and can be set globally or per binary. The on/off switch
itself lives in a separate **extension** group that Binary Ninja creates.

<img src="imgs/settings.png" alt="D-810 settings" width="860">

<img src="imgs/settings-toggle.png" alt="The on/off switch" width="860">

* **Time budget per function**: 20 s by default. Binary Ninja gives up on a function that takes
  too long to analyze, so when a binary is opened with D-810 on, its per-function limit is raised
  to twice the budget. The passes share that allowance; jump-table dispatchers keep up to half
  for control-flow recovery and HLIL cleanup. Table and type changes can trigger another run, so total
  time can exceed the budget.
* **Generic MBA simplifier (Tier 2)**: the linear simplifier and CoBRA. Turn it off to use the
  rules only.
* **Skipped functions** and **Disabled rules**: addresses and rule names to leave alone.
* **Flow fixes**: on by default. Resolves code-pointer tables and identity wrappers, and undoes
  it all when D-810 is turned off. Your own types and branch targets always win.
* **Unflatten control flow**: on by default. It only touches a function with a clear dispatcher:
  a loop over a state variable that's only ever set to constants. It also removes junk loops.
* **Read-only ranges**: address ranges (`0x1000-0x1fff`, end included) to treat as constant. Use
  them for data you know never changes. Clearing a range puts the old types back.
* **Enabled**: a kill switch for everything.

## Notes

* D-810 removes the global reads its folds leave unused. Bare `data_...` lines that were there
  before it ran are left alone. They're harmless.
* Calls through resolved API pointers can show extra arguments, because Binary Ninja guesses them
  from whatever registers happen to be set. Giving the pointer a type fixes it.
* Unflattening can leave a flag-like compare behind when the next state is picked before a loop,
  or kept in two places at once. Machines that compute their next state are left alone.
* State functions pulled into their dispatcher are copied, not moved, so they're still there for
  anything else that calls them. Unoptimized builds that widen a state function's result on the
  way out keep their calls. Unmodeled stack/frame inputs and clobbered hidden registers also
  keep their calls.
* Table resolution checks the decoded targets and any visible writes to the table, but it can't
  see changes made by a loader. The debug report says why a table was rejected.

## Tests

```sh
uv sync && uv run pytest    # unit tests, no Binary Ninja needed
tests/samples/build.sh      # build the sample binaries (needs Homebrew LLVM for PE/ELF)
scripts/binja_pytest.sh     # integration tests, in a hidden Binary Ninja instance
```

The integration tests need `pytest` installed in Binary Ninja's Python too.

## License

AGPL-3.0, since the rules in `binja_d810/third_party/d810/` come from d810-ng (re-download them
with `scripts/vendor_d810.py`). CoBRA is Apache-2.0 and isn't included.
