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
* Arithmetic helpers with private register conventions, including nested helpers and proven
  scratch-slot round trips
* Dead private stack writes and non-escaping scratch accesses, including partial byte ranges
* Comparisons and Boolean decisions reconstructed from arithmetic flags, integer encodings and
  supported conditional-compare merges
* Private allocation round trips when explicit semantic contracts establish their memory behavior
* Bounded constant-input buffer computations, with decoded-text comments at their uses
* Invariant expressions over bounded mutable-global value sets, including supported atomic updates
* Opaque predicates, including ones hidden behind long chains of masked constants, ones that
  only hold on every path into a merge, values carried round loops, and the classic
  number-theoretic ones like `7 * y * y - 1 != x * x`
* Divisions that can never trap, like `2 / (x * x + 1)`
* Jumps and calls through encoded tables of code pointers (Hikari-style indirect branches and
  calls), including jumps back into the same function, and identity wrappers around them,
  when no visible code writes the table
* Constants encoded against a global key whose bytes are proven unwritten, like `x ^ key ^ 0x3c1d`.
  This includes bounded writes to neighboring buffers and helpers that only read the pointer,
  including returned pointers and whole local pointer slots whose later uses are proven safe.
* Control-flow flattening: state machines driven by compare trees or jump tables, including
  nested loops, and machines split across functions, where the dispatcher calls itself for the
  next state or calls a small function per state that returns through a stub back to it. Large
  handlers and their loops stay in place while the dispatcher is removed. Self-calls and outlined
  handlers can be recovered together, including dispatchers with large argument lists and
  undeclared register inputs that the handlers provably preserve. Known register and stack
  inputs can also be carried across handlers when their values and frame layout are proven
* Dead scalar loops whose termination can be proved, including arithmetic loops left after branch cleanup
* Equivalent scalar branches: a condition can be removed when both arms leave the same live values
* x86, x64 and ARM64 binaries (PE, ELF and Mach-O)

Every rewrite is checked before it's applied. Conditions are only folded when Z3 proves them,
and D-810's rules are only used at the bit widths Z3 proved them for. It runs in two passes,
MLIL then HLIL, and the HLIL pass picks up whatever MLIL couldn't express. Unflattening happens
in between, by copying code rather than guessing, so the program's behaviour doesn't change.

## Requirements

* Binary Ninja 6.0 (tested on 6.0 Personal, macOS arm64)
* `z3-solver` in Binary Ninja's Python
* Optional: CoBRA, through the `d810-cobra` package

Workflow setup checks the available native stages before installing activities. Missing optional
workflows are left alone; a failed insertion keeps the original workflow intact.

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

It's off by default. Use **Plugins > D810 > Enable for Current Function**, or right-click the
function, go to **Function Settings** and tick **D-810: simplify MBA expressions**.
Both reanalyze the function straight away.

<img src="imgs/function-settings.png" alt="Function Settings menu" width="520">

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

<img src="imgs/settings.png" alt="D-810 settings" width="860">

<img src="imgs/settings-toggle.png" alt="The on/off switch" width="860">

* **Time budget per function**: 20 s by default. Binary Ninja gives up on a function that takes
  too long to analyze, so when a binary is opened with D-810 on, its per-function limit is raised
  to twice the budget. The passes share that allowance; jump-table dispatchers keep up to half
  for control-flow recovery and HLIL cleanup. Table and type changes can trigger another run, so total
  time can exceed the budget.
* **Generic MBA simplifier (Tier 2)**: the linear simplifier and CoBRA. Turn it off to use the
  rules only.
* **Factor masked bitwise expressions**: on by default. Finds shared terms across constant
  bit masks; the bitwise limits and proof checks are described below.
* **Skipped functions** and **Disabled rules**: addresses and rule names to leave alone.
* **Flow fixes**: on by default. Resolves code-pointer tables and identity wrappers, and undoes
  it all when D-810 is turned off. Your own types and branch targets always win.
* **Unflatten control flow**: on by default. It only touches a function with a clear dispatcher:
  a loop over a state variable that's only ever set to constants. It also removes junk loops.
* **Pure-callee summaries**: on by default. Exposes arithmetic across helper calls before MLIL
  generation. Turn it off to restore the calls; the limits are listed below.
* **Clean up private scratch slots**: on by default. Removes proven dead stack traffic and
  exposes private loads/stores to the expression passes. Disabling it restores the original IL.
* **Recombine flag conditions**: on by default. Replaces proven flag formulas with comparisons
  in MLIL and HLIL, including bounded conditional-compare merges.
* **Simplify private allocation round trips**: on by default. Requires explicit semantic
  contracts; ordinary allocator names and signatures are insufficient.
* **Read-only ranges**: address ranges (`0x1000-0x1fff`, end included) to treat as constant. Use
  them for data you know never changes. Clearing a range puts the old types back.
* **Enabled**: a kill switch for everything.

## Masked bitwise expressions

The bitwise simplifier compares its existing normal form with a Boolean polynomial whose
coefficients are bit masks. This can reduce `(x & ~m) | ((x ^ y) & m)` to `x ^ (y & m)` when
`m` is constant. The calculation comes from the expression's truth table; it does not match
particular masks or instruction sequences. Arithmetic subexpressions remain independent inputs.
Only smaller candidates that pass the normal exact-width proof are applied. Rewrites appear
under `bitwise` in the function report and logger. The function deadline bounds the work.

`binja_d810.bitwiseMaskFactoring` defaults to true; disabling it keeps the previous bitwise
normal form. `bitwiseInputs` defaults to 4 (range 1–4), and `bitwiseConstants` defaults to 4
(range 1–8). These limits apply to both forms. Random-mask fixtures at 8/16/32/64 bits check
final values independently on ARM64, x64 PE/ELF and i686, at O0 and O2. This simplifies the
mixing operations inside loops; it does not prove their runtime inputs constant.

## Encoded comparisons

Constant equalities can hide a simpler input test behind additions, XORs, rotations and
multiplications. D810 inverts these operations at their exact widths. Odd multipliers use
their modular inverse; even multipliers and shifts retain the required bit masks, or prove
that the target value is impossible. Casts retain their width and sign constraints. Ordered
comparisons are excluded because these encodings do not generally preserve order.

The MLIL and HLIL passes keep version-safe inputs, reject protected/volatile expressions and
require a final exact proof. A timeout leaves the code unchanged. Applied and skipped results
appear under **preimages** in the function report and logger. Disabling
`binja_d810.comparisonPreimages` and reanalysing restores the original comparisons.

| Setting | Default |
| --- | ---: |
| `binja_d810.comparisonPreimages` | true |
| `binja_d810.preimageInstructions` | 20,000 IL instructions |
| `binja_d810.preimageNodes` | 2,000 expression nodes |
| `binja_d810.preimageWork` | 200,000 visits across all rounds |
| `binja_d810.preimageSites` | 128 comparison candidates |
| `binja_d810.preimageSteps` | 64 encoding layers |
| `binja_d810.preimageRounds` | 2 |
| `binja_d810.preimageProofMilliseconds` | 200 per proof, within the function deadline |

Randomized C fixtures cover 8/16/32/64-bit encodings on ARM64, x64 PE/ELF and i686 at O0/O2.
An independent final-IL oracle checks their results; effect, ordering, budget and rollback
controls stay unchanged. Compilers may leave unsupported forms intact rather than simplify them.

## Bounded recurrence predicates

D810 can recover input tests hidden behind a finite scalar loop. Loop control must be known;
arithmetic inputs can remain unknown. The pass summarizes the loop, finds complete preimages
of its final equality, and keeps correlations between repeated input bits. A meet-in-the-middle
search bounds the work. Known bits narrow the domains without assuming unknown bits are zero.
An optional Z3 proof reduces the resulting predicate to a single input equality or masked test.

The supported state operations are addition, subtraction, XOR, odd constant multiplication,
negation, complement and bounded rotations. All arithmetic uses the original bit widths.
This is exact finite enumeration, not a guessed hash input or a probabilistic collision test.
If either search half, the solution count or the time budget exceeds its cap, no partial
answer is used. Completed proofs can survive a later timeout. Unknown control, calls, memory
effects, aliases, stale inputs and unsupported operations keep the code unchanged. Anonymous
scalar field reads are supported on little-endian IL; partial writes inside loops are excluded.

Rewrites appear under **preimages**, including search-state counts and rejection reasons.
Only the predicates are replaced; existing dead-loop cleanup removes a loop if its values
are no longer read and its body is supported. Loops containing scalar field reads can have
their predicates simplified while the older dead-loop pass retains the loop. Disabling
`binja_d810.recurrencePreimages` and reanalysing restores the IL.

| Setting | Default |
| --- | ---: |
| `binja_d810.recurrencePreimages` | true |
| `binja_d810.recurrenceWork` | 16,000,000 visits |
| `binja_d810.recurrenceLoops` | 64 candidates |
| `binja_d810.recurrenceSites` | 128 subsequent expressions |
| `binja_d810.recurrenceIterations` | 64 iterations per loop |
| `binja_d810.recurrenceStates` | 65,536 states per search half |
| `binja_d810.recurrenceDomainBits` | 8 unknown bits per side input |
| `binja_d810.recurrenceSolutions` | 16 matching domain assignments |
| `binja_d810.recurrenceSeconds` | 6 seconds within the shared function budget |
| `binja_d810.recurrenceCleanupReserveSeconds` | 1 second reserved for later cleanup |

Instruction, node, stage, round and proof limits use `preimageInstructions`, `preimageNodes`,
`preimageSteps`, `preimageRounds` and `preimageProofMilliseconds` above. Scalar widths use
`controlBits` (64 by default). Synthetic C fixtures vary 32/64-bit state parameters and include
impossible targets. An independent Python model enumerates every input in their finite domain;
an independent final-IL oracle checks the results on ARM64, x64 PE/ELF and i686 at O0/O2.
This does not cover arbitrary cryptographic hashes, unbounded loops or runtime memory inputs.

## Constant branches

A pure, proven constant HLIL guard can select its original arm, including direct conditional
assignments and returns. The selected arm keeps its calls, stores and control transfers.
Labels that permit another entry, protected regions and effectful guards prevent removal.
The feature uses exact-width proofs, the shared deadline and the `control*` limits below;
native edit failures roll back. Counts and skipped reasons appear under **control cleanup**.
Set `binja_d810.constantBranches` to false and reanalyse to restore the original branches.
It defaults to true and can be controlled independently of equivalent-branch merging.

## Equivalent scalar branches

D810 compares both arms of a structured HLIL branch over shared, unconstrained inputs. It
tracks assignment order and proves every value used outside the branch equal at its exact
width. Private intermediate values may differ. When the guard is pure and every output proof
succeeds, one original arm replaces the branch. The guard does not have to be constant.

Calls, memory accesses, volatile or aliased variables, partial writes, protected operations
and unsupported control flow keep the branch unchanged. Shared HLIL expression indices are
excluded so use regions cannot overlap ambiguously. This is scalar state equivalence;
it does not infer that two effectful calls or stores are interchangeable. Changes and skipped
proofs appear under **control cleanup** in the function report and logger. Disable the feature
with `binja_d810.equivalentBranches`; reanalysis restores the original IL.

| Setting | Default |
| --- | ---: |
| `binja_d810.equivalentBranches` | true |
| `binja_d810.controlInstructions` | 20,000 HLIL instructions |
| `binja_d810.controlNodes` | 2,000 nodes per scalar expression or composed output |
| `binja_d810.controlWork` | 80,000 visits across all rounds |
| `binja_d810.controlBranches` | 64 candidates across all rounds |
| `binja_d810.controlRounds` | 3 |
| `binja_d810.controlBits` | 64 bits; maximum 64 |
| `binja_d810.controlProofMilliseconds` | 200 per output, bounded by the shared function deadline |

Synthetic C fixtures cover ARM64, x64 PE/ELF and i686 at O0/O2. Independent final-value proofs
check 8/16/32/64-bit computations; native IL tests cover different live outputs, effects, work
limits and rollback. All output proofs are exact; an inconclusive result leaves the branch alone.

## Dead finite scalar loops

After branch cleanup, a loop may compute values that nobody reads. D810 removes such a loop
only when its body and guards are pure scalar operations, none of its writes are read outside
it, and exact bounded interpretation proves it terminates. Unknown arithmetic inputs remain
unknown; every decision controlling execution must be determined from known values at the
original bit widths. Counter wraparound, signed comparisons, breaks, continues and loop updates
are included. This can remove dead arithmetic/hash loops without assuming their inputs constant.

Entry constants come only from a straight-line prefix in the same enclosing block. Unknown
control flow discards those facts. Calls, memory traffic, escaped or volatile variables, partial
writes, nested loops and unsupported operations are rejected. A live result, unknown decision
or exhausted bound keeps the loop. The pass does not summarize live loops or invert hashes.

`binja_d810.deadFiniteLoops` defaults to true. `deadLoopIterations` defaults to 64 body executions
per proof, and `deadLoopSites` to 64 candidates across all rounds. The `controlInstructions`,
`controlNodes`, `controlWork`, `controlRounds` and `controlBits` limits above also apply to this
pass. Each cleanup pass has its own work cap and shares the existing function deadline.
Reports and logs list removed loops, exact iteration counts and rejection reasons under
**control cleanup**. Disabling the feature and reanalysing restores the original IL.
Unused scalar initializers and declarations feeding removed loops are also cleared after a
fresh use check. Effectful sources and assignment expressions with observed values stay intact.

Tests use randomized starts/steps at 8/16/32/64 bits and native C fixtures on ARM64, x64 PE/ELF
and i686 at O0/O2. They check final values independently and preserve live results, stores,
unknown/infinite loops, escapes and protected operations. Termination and removal are exact
checks; no heuristic trip count is used.

## Arithmetic helpers

D810 reads the helper's lifted IL to find its register inputs and outputs, including registers
missing from its declared calling convention. It builds fixed-width expression summaries and
substitutes checked copies of the body at ordinary calls. The copies retain flag writes and
call/return stack effects. Unsupported effects, loops, tail calls and recursion stay unchanged.

Scratch stores need a byte-level proof that the caller cannot read or escape them. This currently
supports direct acyclic caller branches and multiple returns, with summaries for every call. Each block
is checked once. At joins, the proof keeps every possible byte writer and treats differing scalar
values as unknown; ambiguous stack-derived values reject the proof. A read on any path blocks the
affected helper, and partial overwrites do not hide later reads. Loops, indirect branches, unproved calls, protected
branches and uncertain or unbalanced stack positions stay unchanged.
Saved stack return addresses are checked as well as the return opcode, including in nested summaries.

The stores are retained in the copied IL; this pass does not clear alias flags or
remove arbitrary stack writes. Fixed read-only loads are allowed; writable and relocated loads are not.

The default limits are:

| Setting | Default |
| --- | ---: |
| Callee instruction limit, including nested expansion | 64 |
| Callee expression limit | 2,000 nodes |
| Callee summary depth | 4 |
| Callee frame byte limit | 256 |
| Callee caller instruction limit | 4,096 |
| Callee call-site limit | 64 caller sites |
| Callee proof timeout | 500 ms |
| Callee red-zone byte limit | 128, capped by the platform ABI |
| `binja_d810.calleeFrameControlFlow` | true; disable to restrict scratch proofs to one block |
| `binja_d810.calleeFrameWork` | 40,000 graph, merge and IL visits per caller-frame proof |

Depth bounds composition through the current IL; helpers simplified in an earlier analysis can
already be flatter. Summary planning and substitution share the function time budget.
Unknown and kernel platforms get no red-zone
allowance; setting its limit to zero disables that support. The ABI bounds come from the
[System V AMD64 specification](https://gitlab.com/x86-psABIs/x86-64-ABI/-/blob/master/x86-64-ABI/low-level-sys-info.tex)
and [Apple's ARM64 documentation](https://developer.apple.com/documentation/xcode/writing-arm64-code-for-apple-platforms).

**Prefer native callee inlining** is an optional comparison mode, off by default. For register-only
helpers it records a user inline flag and restores it when disabled. It needs Flow fixes for the
undo queue. Frame-dependent helpers continue to use direct substitution, since a global inline
flag would affect unproved callers too. Existing no-inline flags are respected.

The debug report's `callees` entries list substitutions, checked blocks, work visits and rejection reasons. Synthetic fixtures
cover ARM64, x86-64 and ordinary i686 calls at O0/O2, with independent arithmetic models and undo
checks. Branched-caller fixtures also cover i686 private registers, reads on only one path, partial
overwrites, separate call sites, multiple returns and repeated branch joins. Independent Z3
execution checks final results for every input. These tests do not establish coverage on real malware. Direct call-link substitution is
currently supported on those three architectures; other architectures are left unchanged.

## Private scratch slots

D810 follows stack addresses through copies and fixed offsets. When every use is accounted for,
it converts local loads/stores to variable accesses and clears their alias status in the generated
IL. It then removes a store only if no path can read any of its bytes before they are overwritten.
An additional SSA check preserves live logical values that happen to share a stack location.
Unused pure address calculations are removed with the stores.

Escaping addresses, unknown calls, volatile/protected slots and unmodeled memory accesses stay
conservative. Partial overwrites preserve the remaining bytes. Overlapping logical variables
keep their alias identities; partial accesses on big-endian targets are currently excluded.
The proof uses exact byte ranges and all CFG paths, without guessing which branch will run.

Cleanup runs before MBA rewriting and after control-flow recovery, under the shared function
budget. Its settings are:

| Setting | Default |
| --- | ---: |
| `binja_d810.scratchSlots` | true |
| `binja_d810.scratchInstructions` | 20,000 instructions |
| `binja_d810.scratchNodes` | 40,000 proof nodes per round |
| `binja_d810.scratchFrameBytes` | 4,096 distinct stack bytes |
| `binja_d810.scratchLivenessVisits` | 100,000 fixpoint visits |
| `binja_d810.scratchRounds` | 4 rounds per cleanup stage |
| `binja_d810.scratchAddressDepth` | 64 SSA definitions |

The report's `scratch` entries show removed stores, promoted accesses, cleared aliases and
rejection reasons. No user types or binary bytes are changed; disabling the feature regenerates
the original aliases. Synthetic ARM64, x86-64 and i686 fixtures check independent arithmetic
models, escapes, partial overlaps, budgets and disable/restore. One i686 PE O2 partial-store
fixture exposes an existing native MLIL error that loses the upper bytes before D810 runs;
the suite records it as an expected failure and separately checks that D810 preserves the baseline.
These fixtures do not establish coverage on real malware.

## Flag conditions

D810 rebuilds signed and unsigned comparisons from pure boolean and arithmetic expressions.
For conditional compares, it follows the actual SSA values leaving each arm of a branch and
combines them with the branch condition. It checks every proposed expression at its exact
bit widths, then removes newly unused flag calculations. A flag also used as external data
keeps that use and blocks the dependent rewrite.

The native passes read IL semantics, without matching register or symbol names. A small
subtraction model isolates ARM64's no-borrow carry convention from x86's borrow convention;
unit tests prove all ten comparisons at several widths. Native C fixtures cover all eight
ARM64/x86-64/i686 builds, with an independent symbolic interpreter checking complete branch
paths and observable stores. On x86-64, ordinary compare chains are often already simplified
by Binary Ninja. The ARM64 signed and unsigned conditional-compare fixtures lose their merged
flag locals. Up to three rounds expose later comparisons after earlier guards simplify; all
rounds share the work, condition-count and time limits.

Boolean decisions can also be encoded as integer sums, products, masks and XORs. D810 builds
a bounded truth table over independent predicates and proposes a simpler Boolean expression.
Each result is checked against the original at its exact widths before application. The pass
never assumes an arbitrary integer is Boolean or that the predicates themselves are constant.
Existing purity, version and flag-use checks still apply. Synthetic fixtures cover 8/16/32/64-bit
encodings on ARM64, x64 and i686, with independent checks of final values and observable stores.
Flag-use confinement follows SSA phi outputs, including dead cycles, to their actual consumers.
A phi with no observable consumer no longer blocks a rewrite. Any data use outside the accepted
condition slices still rejects it; this check does not delete phis or their other consumers.

Before native flag lowering, D810 can expand overflow from original integer subtraction IL.
It captures both operands, keeps the original instruction and flag effects, then adds an
overflow formula proven against a widened signed subtraction. Architecture metadata identifies
integer flag writers and condition dependencies; bounded liveness selects the reaching writes.
Liveness solves block boundaries and scans instructions once, revisiting only affected
predecessors. Conditions that do not use overflow skip that proof entirely. Raw flag uses are
checked against their reaching writer, and rebuild work is checked before allocating new IL.
The existing work limit and shared deadline still apply.
Native tests cover operand clobbering and 8/16/32/64-bit signed-minimum cases on ARM64 and x86.
Memory operands, protected operations and flag sources that also feed raw data keep their original IL.

Supported merges are bounded two-arm branches, including one empty arm. Loop-carried or ambiguous
merges, stale operands, volatile/protected values and unknown flag operations stay unchanged.
Native `ADD_OVERFLOW` remains unmodeled. The earlier subtraction proof avoids it where the
original flag source is available; its semantics are never guessed from the displayed expression.
Explicit arithmetic overflow formulas are supported and proven. These are synthetic results;
no real-malware coverage is claimed.

| Setting | Default |
| --- | ---: |
| `binja_d810.flagConditions` | true; disable to restore the original conditions |
| `binja_d810.flagSourceOverflow` | true; disable only the early subtraction-overflow expansion |
| `binja_d810.flagSourceBits` | 64 bits; maximum 64 |
| `binja_d810.flagRounds` | 3; work and condition limits apply across all rounds |
| `binja_d810.flagBooleanCircuits` | true; disable integer-encoded Boolean reconstruction |
| `binja_d810.flagBooleanAtoms` | 4 independent predicates; maximum 8 |
| `binja_d810.flagPhiUses` | true; follow phi outputs when proving flag-use confinement |
| `binja_d810.flagInstructions` | 20,000 instructions |
| `binja_d810.flagNodes` | 2,000 expression nodes |
| `binja_d810.flagWork` | 80,000 proof visits per pass |
| `binja_d810.flagCandidates` | 128 comparison attempts per expression |
| `binja_d810.flagConditionsPerPass` | 128 condition roots |
| `binja_d810.flagMergeDepth` | 8 nested merges |
| `binja_d810.flagProofMilliseconds` | 200 ms per proof |

All stages share the function's time budget. The `flags` report entries list source models,
successful recombinations and rejection reasons. These are regenerated IL edits, with no binary or user
metadata changes.

## Private allocation round trips

D810 can forward a private object's loads from preceding stores, including partial overlaps.
It proves all pointer uses, rejects escapes and observations of the pointer's numeric value,
and keeps unknown calls, memory effects and control-flow crossings conservative. Constant-size
and pointer-forwarding call/tail-call wrappers are checked from their MLIL bodies.

This needs a contract guaranteeing fresh, ordinary, nonnull storage. Removing the allocation,
stores and release also requires an explicit guarantee that the complete lifetime is unobservable,
including failure, exceptions and effects on later allocations. Without that stronger guarantee,
only loads are forwarded; calls and stores remain. Possible allocation failure retains the original
memory accesses. A null check does not authorize deletion of its failure path.

The installed type libraries we tested supplied signatures but no such effect guarantees.
D810 does **not** identify allocators by names such as `malloc` or `free`. Contracts can come from
the `binja_d810.memory` type attribute or explicit per-function metadata. D810 also follows Binary
Ninja's recorded import provenance to the original type-library object, recovering contracts
even when rebuilt signatures have lost their custom attributes. This works through checked
wrappers and does not match allocator or library names. Conflicting declarations are rejected;
an imported signature must expose the arguments required by its contract. For a selected
allocator with the required guarantees, per-function metadata remains another option:

```python
current_function.store_metadata("binja_d810.memory",
    '{"version":1,"role":"allocate","parameter":0,"fresh":true,"ordinary":true,"nonnull":true,"elidable":false}')
```

`parameter` is the zero-based size argument. A release uses
`{"version":1,"role":"deallocate","parameter":0,"elidable":false}`, where the parameter is
the object pointer. Set `elidable` on both contracts only when the stronger lifetime guarantee
has been established. Reanalyze the callers after changing contracts. D810 reads these user-owned
contracts and leaves them intact when disabled; it makes no user type or binary changes.

| Setting | Default |
| --- | ---: |
| `binja_d810.allocationRoundTrips` | true; disable to restore the original IL |
| `binja_d810.allocationInstructions` | 20,000 instructions |
| `binja_d810.allocationNodes` | 40,000 proof visits |
| `binja_d810.allocationSites` | 64 calls |
| `binja_d810.allocationBytes` | 4,096 bytes per object |
| `binja_d810.allocationWrapperInstructions` | 64 instructions |
| `binja_d810.allocationWrapperDepth` | 4 wrappers |
| `binja_d810.allocationExpressionNodes` | 2,000 nodes |
| `binja_d810.allocationContractBytes` | 4,096 UTF-8 bytes |

The pass shares the function deadline. The `allocations` report lists forwarded loads, removed
lifetimes and rejection reasons. Synthetic C fixtures cover ordinary imported allocation calls,
boxed-value wrappers, partial writes, escapes, pointer observations, missing guarantees and undo
on all eight builds. Elision tests explicitly assume infallible, unobservable test contracts;
they do not prove those properties for a real allocator or for malware. Unannotated library
allocations remain unchanged.

Import-provenance fixtures provide explicit test guarantees in synthetic type libraries and
strip them from the bound signatures. They verify automatic lookup, wrapper forwarding, nullable
rejection and disable/restore across all eight builds. They do not supply guarantees for the
installed standard libraries or establish that a real allocator's lifetime can be erased.

## Constant buffers and decoded text

`binja_d810.constantBuffers` (default **true**) finds deterministic MLIL regions throughout a
function, including after calls and inside separate branches. Scalar arithmetic, bounded loops,
immutable image reads and local byte stores are evaluated at their actual widths. Each region
must have one proved entry; shared tails remain outside it, and retained paths cannot reenter
it. Register writes and live values are preserved. Unknown inputs, writable reads, pointer-bit
observations and unmodeled effects stop that candidate; other independent regions can still run.

Calls remain in place and evaluation starts with fresh unknown memory after them. Earlier frame
pointer captures block later frame rewrites. Read-only, noncapturing helper uses may be proved
from their IL; ambiguous return or pointer uses stay conservative. Constant register definitions
and local addresses can be bound through bounded SSA chains. The pass never executes the binary
or calls a helper.

This is exact concrete evaluation of the input IL, with separate control-flow and live-out
checks. String recognition is a display heuristic: printable ASCII ending in NUL, or a buffer
followed by a constant length argument. The computed bytes are known; the callee's interpretation
of them is not assumed. The debug report records steps, text and rejection reasons.
`binja_d810.constantBufferComments` (default **true**) controls the comments. They use native
undo, preserve user edits, and are removed when the feature or D810 is disabled. Comments
require Flow fixes; IL evaluation does not. No binary bytes change.

Limits share the function time budget. Each key below has the `binja_d810.` prefix:

| Setting | Default |
| --- | --- |
| `constantInstructions` | 4,096 MLIL instructions |
| `constantExpressionNodes` | 2,000 nodes per expression |
| `constantSteps` | 10,000 evaluation steps across attempted regions |
| `constantTrips` | 256 visits per instruction |
| `constantFrameBytes` | 4,096 bytes of local frame |
| `constantProofWork` | 100,000 proof visits |
| `constantRegionStarts` | 64 candidate starts across the function |
| `constantRegions` | 8 disjoint replacements |
| `constantDefinitionDepth` | 32 nested SSA definitions for entry bindings |
| `constantAnnotationSteps` | 4,096 retained-tail instructions |
| `constantScalarBits` | 128 bits |
| `constantStringBytes` | 256 bytes |
| `constantStringMinimum` | 4 printable bytes |
| `constantStoreBytes` | 8 bytes per emitted store; maximum 8 |

SIMD, floating arithmetic, unknown pointer bytes and ambiguous logical/physical stack overlaps stay
unchanged. Native synthetic constant-copy operations are identified from their IL metadata and
original stores, never by library name. Their bytes have already been computed by Binary Ninja.

Tests generate rotate/multiply/add/XOR streams from 16 randomized seeds in loop and unrolled
forms, built for ARM64, x86-64 PE/ELF and i686 at O0/O2. Final IL bytes are read back independently
and compared with the Python generator. Fixtures disable compiler vectorization and stack
protectors to test scalar semantics. Three x86 O2 cases currently have incorrect native
constant-copy bytes **before D810**; strict expected failures record that limitation and check
that D810 preserves the original IL's bytes. These tests make no real-malware coverage claim.
Additional fixtures check loops after calls, two sequential regions, conditional regions sharing
a tail, earlier pointer capture, shared limits and independent final-byte readback on both arms.
Fixture helper types match their C declarations; an incorrectly inferred pointer return can
otherwise prevent the noncapture proof.

## Mutable global value sets

`binja_d810.mutableValueSets` (default **true**) extends the existing global-write checks.
For a private image-initialized integer, every visible write must be proven to XOR or add a
constant. The pass computes closure under all those updates, including any guard order and
number of repetitions. Partial XOR/add writes are modeled at the written slice's actual width
and endianness: addition wraps inside those bytes and cannot carry into neighboring bytes.
Unknown writes, escapes, exports, relocations and volatile globals are rejected.

Proofs wait for the view's initial analysis and cross-reference discovery. An early caller is
automatically retried through the idle queue; this also works with Flow fixes disabled.

A read remains a read. Separate memory observations receive separate Z3 symbols. A consumer
folds only when exact-width Z3 proof shows the same result for every combination of possible
values and all remaining inputs. Original memory reads are retained and marked against dead
store elimination; the pass does not rewrite stores or global types. Constrained proofs do not
enter the ordinary, unconstrained proof cache. This is a conservative set of possibilities,
not a claim that every value is reachable under the program's actual guards.

The debug report keeps the skipped-constant-key write address and adds the possible values.
`binja_d810.mutableValueComments` (default **true**) adds an undoable comment at the reading
function. Disabling the feature regenerates the original IL and removes its owned comment;
user edits are preserved. Comments require Flow fixes. These checks use the same visible-code,
address-escape and external-writability assumptions as the existing global analysis.

All limits share the function deadline. Keys below have the `binja_d810.` prefix:

| Setting | Default |
| --- | --- |
| `mutableInstructions` | 20,000 instructions per inspected function |
| `mutableProofWork` | 40,000 proof visits |
| `mutableExpressionNodes` | 2,000 nodes per expression |
| `mutableGlobals` | 32 candidate globals/domains |
| `mutableReaders` | 256 references per global |
| `mutableValues` | 64 values per closure |
| `mutableWrites` | 1,024 candidate writes |
| `mutableClosureSteps` | 4,096 update applications |
| `mutableBits` | 64 bits per global/consumer; maximum 64 |
| `mutableProofMilliseconds` | 200 ms per solver query |
| `mutableCandidates` | 128 arithmetic consumers |
| `mutableEvidenceBytes` | 1 MiB of code evidence for deferred comments |

Synthetic C tests cover guarded updates, atomic XOR/add, zero initialization, byte/word writes,
independent reads, oversized closures and excluded globals on eight builds. Installed Binary
Ninja lowers the tested ARM64 atomic instructions and x86 locked operations to ordinary
load/store IL; x86 O0 compare-exchange retries are handled through bounded SSA copy/phi
provenance. Unrecognized atomic intrinsics stay unsupported. No instruction-text or function-name
matching is used, and these tests do not establish real-malware coverage. Partial-add tests
include byte and word wraparound on both primary architectures, with independent byte models
also checking little- and big-endian slices.

## Coverage and proof limits

All six passes have plain Python tests and synthetic native fixtures. Successful checks establish
the supported IL transformations, not coverage on a malware family. Every pass shares the
function budget and reports rejected or exhausted proofs. Disable keys below use the
`binja_d810.` prefix; disabling regenerates original IL and releases owned metadata.

| Feature | Proven behavior | Heuristics, assumptions and remaining limits | Disable setting |
| --- | --- | --- | --- |
| Callee summaries | Exact register/frame arithmetic, all-path scratch proofs and checked call substitution | Bounded acyclic helpers and callers; unknown calls or ambiguous stack joins reject scratch proofs. Direct call-link support is ARM64/x86/x64. Optional native inlining is a comparison mode. | `calleeSummaries` |
| Scratch slots | No-escape checks and byte-range liveness on every CFG path | Unknown effects, ambiguous logical overlaps and big-endian partial fields stay unchanged. | `scratchSlots` |
| Flag conditions | Exact-width comparison/Boolean equivalence, original subtraction-overflow proofs and confined flag uses | Bounded truth tables and two-arm merges; early models need pure operands and known flag metadata. Native `ADD_OVERFLOW` remains unmodeled. | `flagConditions` |
| Allocation round trips | Exact private byte forwarding under explicit contracts | Fresh/nonnullable storage and elidable lifetime guarantees are assumptions supplied by the analyst; installed libraries did not provide them. No name matching. | `allocationRoundTrips` |
| Constant buffers | Exact scalar execution of multiple regions, final bytes, live-outs and entry/exit checks | Escaped frames, unknown inputs/effects and SIMD remain unsupported. Printable text and adjacent-length interpretation are display heuristics. Native input-IL errors remain visible in tests. | `constantBuffers` |
| Mutable globals | Closed full/partial update sets and Z3 invariance for independent reads | Visible-code/no-escape assumptions; guard order is overapproximated. Large sets and unknown atomic intrinsics are excluded. | `mutableValueSets` |

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
