-- ABOUTME: Lean's `Environment.replay` (v4.32.0, src/lean/Lean/Replay.lean) with a deterministic kernel budget: every
-- ABOUTME: declaration is checked in a fresh thread with a heartbeat limit, and its heartbeats are recorded.
import Lean

open Lean

namespace LcbenchTools.BudgetReplay

/-! This is `Lean.Environment.replay` of Lean v4.32.0 (Copyright (c) 2023 Kim Morrison, Apache 2.0), copied line for
line except for `addDecl`. Lean's version calls `Kernel.Environment.addDeclCore env 0 decl` (no limit). Here each
declaration goes to the same kernel function with `maxHeartbeats := budget`, the kernel's own deterministic limit
(the one `set_option maxHeartbeats` sets for `addDecl` during elaboration).

The kernel compares its heartbeat counter, which belongs to the current thread and is never reset, with the limit. So
each declaration is checked in a new dedicated thread, whose counter starts at zero: the limit then applies to that
declaration alone, whatever ran before it. On that thread the number of small allocations (`IO.getNumHeartbeats`,
Lean's documented heartbeat count) is measured around the kernel call and recorded. It is larger than the kernel's own
count: on the toy slow-kernel fixture the kernel stopped at 3.19-3.20 times the budget in recorded heartbeats (budget
5,000,000: 15,954,685 recorded; 2,000,000: 6,393,765). A declaration whose recorded count exceeds the budget is
rejected too, so the recorded number is the one the limit is checked against; one that stays below the kernel's own
stop runs to completion first and is rejected afterwards. Both counts depend only on the declaration and the
environment, not on the machine's speed or load. A budget of 0 means no limit.

Lean v4.33 added a `maxRecDepth` argument to `Kernel.Environment.addDeclCore` (its own `Replay.lean` passes 0 there,
the only line that differs from v4.32.0). `kernel_add_decl%` reads the signature of the toolchain that compiles this
file and passes 0 for that argument when it exists, so one source serves every pinned toolchain. -/

/-- The explicit binder names of a function type, outermost first. -/
def binderNames : Expr → List Name
  | .forallE n _ b _ => n :: binderNames b
  | _ => []

open Elab Term in
/-- `kernel_add_decl% env budget decl`: `Kernel.Environment.addDeclCore env budget decl` with no cancel token, plus
`maxRecDepth := 0` (as Lean's own replay passes it) on toolchains whose kernel takes that argument. -/
elab "kernel_add_decl% " env:term:max budget:term:max decl:term:max : term => do
  let info ← getConstInfo ``Kernel.Environment.addDeclCore
  let call ← if (binderNames info.type).contains `maxRecDepth then
      `(Kernel.Environment.addDeclCore $env $budget 0 $decl none)
    else
      `(Kernel.Environment.addDeclCore $env $budget $decl none)
  elabTerm call none

/-- Heartbeat bookkeeping of a replay: total and largest count per declaration, and the declaration over budget.
Names are kept as strings: a declaration's `Name` may live in an `.olean` region that the caller frees after the
replay (`replayOne`), and a string made here does not. -/
structure Usage where
  total : Nat := 0
  maxDecl : Nat := 0
  maxDeclName : String := ""
  declarations : Nat := 0
  /-- The declaration that exceeded the budget, if any. -/
  overBudget : Option String := none

structure Context where
  newConstants : Std.HashMap Name ConstantInfo
  budget : Nat

structure State where
  env : Kernel.Environment
  remaining : NameSet := {}
  pending : NameSet := {}
  postponedConstructors : NameSet := {}
  postponedRecursors : NameSet := {}
  usage : Usage := {}

abbrev M := ReaderT Context <| StateRefT State IO

/-- Thrown when a declaration exceeds the kernel budget; `replay` reports it in `Usage.overBudget`. -/
def overBudgetMarker : String := "lcbench: kernel heartbeat budget exceeded"

/-- Check if a `Name` still needs processing. If so, move it from `remaining` to `pending`. -/
def isTodo (name : Name) : M Bool := do
  let r := (← get).remaining
  if r.contains name then
    modify fun s => { s with remaining := s.remaining.erase name, pending := s.pending.insert name }
    return true
  else
    return false

/-- Use the current `Environment` to throw a `Kernel.Exception`. -/
def throwKernelException (ex : Kernel.Exception) : M Unit := do
  throw <| .userError <| (← ex.toMessageData {} |>.toString)

/-- The kernel check of one declaration on a new thread (heartbeat counter at zero): the result and the heartbeats
(small allocations) the check used on that thread. -/
def checkOnFreshThread (env : Kernel.Environment) (budget : Nat) (d : Declaration) :
    IO (Except Kernel.Exception Kernel.Environment × Nat) := do
  let task ← IO.asTask (prio := .dedicated) do
    let before ← IO.getNumHeartbeats
    -- The result goes through a reference so that the compiler cannot move the kernel call past the second reading
    -- of the counter (it moves pure `let`s to their first use).
    let cell ← IO.mkRef (none : Option (Except Kernel.Exception Kernel.Environment))
    cell.set (some (kernel_add_decl% env (budget.toUSize) d))
    let after ← IO.getNumHeartbeats
    match (← cell.get) with
    | some result => return (result, after - before)
    | none => throw (IO.userError "unreachable: no kernel result")
  IO.ofExcept task.get

/-- Add a declaration, possibly throwing a `Kernel.Exception`; the budgeted version of Lean's `addDecl`. -/
def addDecl (d : Declaration) : M Unit := do
  let budget := (← read).budget
  let (result, used) ← checkOnFreshThread (← get).env budget d
  let name := match d.getTopLevelNames with
    | n :: _ => n.toString
    | [] => ""
  modify fun s =>
    let u := s.usage
    let u := { u with total := u.total + used, declarations := u.declarations + 1 }
    let u := if used > u.maxDecl then { u with maxDecl := used, maxDeclName := name } else u
    { s with usage := u }
  let over (s : State) := { s with usage := { s.usage with overBudget := some name } }
  match result with
  | .ok env =>
    if budget != 0 && used > budget then
      modify over
      throw <| .userError overBudgetMarker
    modify fun s => { s with env := env }
  | .error .deterministicTimeout =>
    modify over
    throw <| .userError overBudgetMarker
  | .error ex => throwKernelException ex

mutual
/--
Check if a `Name` still needs to be processed (i.e. is in `remaining`).

If so, recursively replay any constants it refers to,
to ensure we add declarations in the right order.

The construct the `Declaration` from its stored `ConstantInfo`,
and add it to the environment.
-/
partial def replayConstant (name : Name) : M Unit := do
  if ← isTodo name then
    let some ci := (← read).newConstants[name]? | unreachable!
    replayConstants ci.getUsedConstantsAsSet
    -- Check that this name is still pending: a mutual block may have taken care of it.
    if (← get).pending.contains name then
      try
        match ci with
        | .defnInfo   info =>
          addDecl (Declaration.defnDecl   info)
        | .thmInfo    info =>
          -- Ignore duplicate theorems (see Lean's `replay`: the module system may load duplicate private theorems).
          if let some (.thmInfo info') := (← get).env.find? ci.name then
            if info.name == info'.name &&
              info.type == info'.type &&
              info.levelParams == info'.levelParams &&
              info.all == info'.all
            then
              return
          addDecl (Declaration.thmDecl    info)
        | .axiomInfo  info =>
          addDecl (Declaration.axiomDecl  info)
        | .opaqueInfo info =>
          addDecl (Declaration.opaqueDecl info)
        | .inductInfo info =>
          let lparams := info.levelParams
          let nparams := info.numParams
          let all ← info.all.mapM fun n => do pure <| ((← read).newConstants[n]!)
          for o in all do
            modify fun s =>
              { s with remaining := s.remaining.erase o.name, pending := s.pending.erase o.name }
          let ctorInfo ← all.mapM fun ci => do
            pure (ci, ← ci.inductiveVal!.ctors.mapM fun n => do
              pure ((← read).newConstants[n]!))
          -- Make sure we are really finished with the constructors.
          for (_, ctors) in ctorInfo do
            for ctor in ctors do
              replayConstants ctor.getUsedConstantsAsSet
          let types : List InductiveType := ctorInfo.map fun ⟨ci, ctors⟩ =>
            { name := ci.name
              type := ci.type
              ctors := ctors.map fun ci => { name := ci.name, type := ci.type } }
          addDecl (Declaration.inductDecl lparams nparams types false)
        -- We postpone checking constructors,
        -- and at the end make sure they are identical
        -- to the constructors generated when we replay the inductives.
        | .ctorInfo info =>
          modify fun s => { s with postponedConstructors := s.postponedConstructors.insert info.name }
        -- Similarly we postpone checking recursors.
        | .recInfo info =>
          modify fun s => { s with postponedRecursors := s.postponedRecursors.insert info.name }
        | .quotInfo _ =>
          -- `Quot.lift` and `Quot.ind` have types that reference `Eq`,
          -- so we need to ensure `Eq` is replayed before adding the quotient declaration.
          replayConstant `Eq
          addDecl (Declaration.quotDecl)
        modify fun s => { s with pending := s.pending.erase name }
      catch ex =>
        if (← get).usage.overBudget.isSome then throw ex
        throw <| .userError s!"while replaying declaration '{name}':\n{ex}"

/-- Replay a set of constants one at a time. -/
partial def replayConstants (names : NameSet) : M Unit := do
  for n in names do replayConstant n

end

/--
Check that all postponed constructors are identical to those generated
when we replayed the inductives.
-/
def checkPostponedConstructors : M Unit := do
  for ctor in (← get).postponedConstructors do
    match (← get).env.find? ctor, (← read).newConstants[ctor]? with
    | some (.ctorInfo info), some (.ctorInfo info') =>
      if ! (info == info') then throw <| IO.userError s!"Invalid constructor {ctor}"
    | _, _ => throw <| IO.userError s!"No such constructor {ctor}"

/--
Check that all postponed recursors are identical to those generated
when we replayed the inductives.
-/
def checkPostponedRecursors : M Unit := do
  for ctor in (← get).postponedRecursors do
    match (← get).env.find? ctor, (← read).newConstants[ctor]? with
    | some (.recInfo info), some (.recInfo info') =>
      if ! (info == info') then throw <| IO.userError s!"Invalid recursor {ctor}"
    | _, _ => throw <| IO.userError s!"No such recursor {ctor}"

/-- The outcome of a budgeted replay: the new environment (or the error) and the heartbeat usage. -/
structure Outcome where
  env : Except String Environment
  usage : Usage

/--
"Replay" some constants into an `Environment`, sending them to the kernel for checking with a heartbeat budget per
declaration (0: no limit). Lean's `Environment.replay` with a budget; the error is a string, and a declaration over
the budget is named in `usage.overBudget`.
-/
def replay (newConstants : Std.HashMap Name ConstantInfo) (env : Environment) (budget : Nat) : IO Outcome := do
  let mut remaining : NameSet := ∅
  for (n, ci) in newConstants.toList do
    -- We skip unsafe constants, and also partial constants.
    -- Later we may want to handle partial constants.
    if !ci.isUnsafe && !ci.isPartial then
      remaining := remaining.insert n
  let state : IO.Ref State ← IO.mkRef { env := env.toKernelEnv, remaining }
  let run : M Unit := do
    for n in remaining do
      replayConstant n
    checkPostponedConstructors
    checkPostponedRecursors
  try
    ReaderT.run (ReaderT.run run { newConstants, budget }) state
    let s ← state.get
    return { env := .ok (.ofKernelEnv s.env), usage := s.usage }
  catch e =>
    return { env := .error e.toString, usage := (← state.get).usage }

end LcbenchTools.BudgetReplay
