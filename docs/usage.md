# Usage and settings

[Back to README](../README.md) · [Deobfuscation reference](reference.md)

Workflow setup checks the available native stages before installing activities. Missing optional
workflows are left alone; a failed insertion keeps the original workflow intact.

## Usage

It's off by default. Use **Plugins > D810 > Enable for Current Function**, or right-click the
function, go to **Function Settings** and tick **D-810: simplify MBA expressions**.
Both reanalyze the function straight away.

<img src="../imgs/function-settings.png" alt="Function Settings menu" width="520">

To run it on a whole binary, use **Plugins > D810 > Enable for This Binary**, or tick the same
option in **File > Open with Options** before opening it. The menu also has disable commands
for the current function and the whole binary; these undo D810's flow fixes. Your skipped-function
list and time budgets still apply. Functions without obfuscation are skipped almost instantly,
but heavily obfuscated ones can use their whole time budget each, so on big binaries it's often
quicker to enable it only for the functions you care about.

Every rewrite is logged under `binja_d810`. The log says **D810: analysis complete** once native
analysis and D810's queued changes have finished. Pass failures are reported as errors; budget
warnings still mean some work was left unfinished.

Use **Plugins > D810 > Show Function Report** for the latest report, or run this in the Python console:

```python
current_function.request_debug_report("d810")
current_function.reanalyze()
```

## Settings

Everything is under **Settings > D-810** and can be set globally or per binary. The on/off switch
itself lives in a separate **extension** group that Binary Ninja creates.

<img src="../imgs/settings.png" alt="D-810 settings" width="860">

<img src="../imgs/settings-toggle.png" alt="The on/off switch" width="860">

* **Time budget per function**: 20 s by default. Binary Ninja gives up on a function that takes
  too long to analyze, so when a binary is opened with D-810 on, its per-function limit is raised
  to twice the budget. The passes share that allowance; jump-table dispatchers keep up to half
  for control-flow recovery and HLIL cleanup. Table and type changes can trigger another run, so total
  time can exceed the budget.
* **Generic MBA simplifier (Tier 2)**: the linear simplifier and CoBRA. Turn it off to use the
  rules only.
* **Factor masked bitwise expressions**: on by default. Finds shared terms across constant
  bit masks; see the [bitwise reference](reference.md#masked-bitwise-expressions) for limits and proof checks.
* **Skipped functions** and **Disabled rules**: addresses and rule names to leave alone.
* **Flow fixes**: on by default. Resolves code-pointer tables and identity wrappers, and undoes
  it all when D-810 is turned off. Your own types and branch targets always win.
* **Unflatten control flow**: on by default. It only touches a function with a clear dispatcher:
  a loop over a state variable that's only ever set to constants. It also removes junk loops.
* **Pure-callee summaries**: on by default. Exposes arithmetic across helper calls before MLIL
  generation. Turn it off to restore the calls; see the [helper reference](reference.md#arithmetic-helpers) for limits.
* **Clean up private scratch slots**: on by default. Removes proven dead stack traffic and
  exposes private loads/stores to the expression passes. Disabling it restores the original IL.
* **Recombine flag conditions**: on by default. Replaces proven flag formulas with comparisons
  in MLIL and HLIL, including bounded conditional-compare merges.
* **Simplify private allocation round trips**: on by default. Requires explicit semantic
  contracts; ordinary allocator names and signatures are insufficient.
* **Read-only ranges**: address ranges (`0x1000-0x1fff`, end included) to treat as constant. Use
  them for data you know never changes. Clearing a range puts the old types back.
* **Enabled**: a kill switch for everything.
