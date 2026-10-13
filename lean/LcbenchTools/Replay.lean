-- ABOUTME: Kernel replay of every charged (owner) module in one process: the owners' background imports are loaded once
-- ABOUTME: and each owner's declarations go through the kernel (Environment.replay), as stock `leanchecker M` does.
import Lean
import LcbenchTools.BudgetReplay

open Lean

namespace LcbenchTools.Replay

/-! Stock `leanchecker M` (Lean v4.32.0, `src/lean/LeanChecker.lean`, default mode) reads the `.olean` parts of `M`,
imports `M`'s direct imports from their `.olean` files (trusted, not re-checked) and replays the constants of `M`'s
most private part into that environment with `Environment.replay`, which sends each declaration to the kernel.
One `leanchecker` process per owner module pays a full Mathlib import and a walk of the whole search path each time.

`replayOwners` does the same checks with one import. It loads the non-owner imports of all owners once (the
background), then replays the owners one module at a time in import order, each on top of the environment that
already holds the kernel-checked constants of the owners before it. So an owner-to-owner dependency is re-checked
by the kernel instead of trusted from its `.olean`, and every other constant is checked by the same kernel function
against the same imported constants as in stock `leanchecker`.

Before replaying, a scope check confirms that the shared environment cannot accept anything stock `leanchecker`
would reject: every constant an owner declaration uses (structure names of projections included) must be declared in
that owner or in a module of its own import closure (stock `leanchecker` has only those), and an owner constant may
share its name with a background constant or with another owner's constant only when both are the same theorem
statement (`mergeableTheorems`, the copies Lean's own import merges; realized `eq_1` and `congr_simp` lemmas are such
copies). Such a copy is still sent to the kernel, in its own owner's replay, under a fresh name (`copyName`), so every
copy is checked where it is declared; a name declared more than once must have, in each owner's own import closure,
only copies equal to the one the shared environment holds. If a
check fails, or a background module imports an owner, every owner is instead replayed exactly as stock `leanchecker`
does (`replayOne`, one import per owner), each in a child process of its own (`lcbench-check replay-one`). A process maps each `.olean` file at a fixed base address, so
a second import of the same files in one process cannot map them and copies them into private memory, about one full
import per owner; a fresh process per owner costs what one stock `leanchecker` process costs.

Both modes send the declarations to the kernel through `BudgetReplay.replay`, which is `Environment.replay` with a
deterministic heartbeat budget per declaration (`--kernel-heartbeats`, 0 = none) and records the heartbeats each
declaration used; a declaration over the budget stops the replay and is named in the report. -/

/-- The `.olean` parts of module `m`, exactly as `replayFromImports` reads them: the exported part, then the server
and private parts when present. -/
def readParts (m : Name) : IO (Array (ModuleData × CompactedRegion)) := do
  let mFile ← findOLean m
  unless (← mFile.pathExists) do
    throw <| IO.userError s!"object file '{mFile}' of module {m} does not exist"
  let mut fnames := #[mFile]
  let sFile := OLeanLevel.server.adjustFileName mFile
  if (← sFile.pathExists) then
    fnames := fnames.push sFile
    let pFile := OLeanLevel.private.adjustFileName mFile
    if (← pFile.pathExists) then
      fnames := fnames.push pFile
  let parts ← readModuleDataParts fnames
  if parts.size = 0 then throw <| IO.userError "failed to read module data"
  return parts

/-- The exported part (it holds the imports) and the most private part (it holds every constant). -/
def firstData (parts : Array (ModuleData × CompactedRegion)) : ModuleData :=
  match parts[0]? with
  | some (d, _) => d
  | none => default

def lastData (parts : Array (ModuleData × CompactedRegion)) : ModuleData :=
  match parts.back? with
  | some (d, _) => d
  | none => default

/-- The constants of the last ("most private") part, which subsumes the others. -/
def ownConstants (parts : Array (ModuleData × CompactedRegion)) : Std.HashMap Name ConstantInfo := Id.run do
  let data := lastData parts
  let mut out : Std.HashMap Name ConstantInfo := {}
  for name in data.constNames, ci in data.constants do
    out := out.insert name ci
  return out

/-- Stock `leanchecker`'s check of one module (`replayFromImports`, line for line, with the budgeted replay; the
`.olean` regions stay mapped until the process exits), and the number of the module's constants. -/
unsafe def replayOne (m : Name) (budget : Nat) : IO (BudgetReplay.Outcome × Nat) := do
  let parts ← readParts m
  let mod := firstData parts
  let consts := ownConstants parts
  let (_, s) ← importModulesCore mod.imports |>.run
  let env ← finalizeImport s mod.imports {} 0 false false (isModule := true)
  -- the regions are not freed: the process exits after the replay, and the kernel threads of the budgeted replay
  -- may still hold objects of this environment in thread-local caches (freeing them crashed the fallback path)
  return (← BudgetReplay.replay consts env budget, consts.size)

structure ModuleReport where
  module : Name
  ms : Nat
  constants : Nat
  usage : BudgetReplay.Usage := {}

def ModuleReport.toJson (m : ModuleReport) : Json :=
  Json.mkObj [("module", Lean.toJson m.module.toString), ("ms", Lean.toJson m.ms),
    ("constants", Lean.toJson m.constants), ("declarations_checked", Lean.toJson m.usage.declarations),
    ("heartbeats", Lean.toJson m.usage.total), ("max_declaration_heartbeats", Lean.toJson m.usage.maxDecl),
    ("max_declaration", Lean.toJson m.usage.maxDeclName)]

/-- One owner replayed as stock `leanchecker` does, as printed by `lcbench-check replay-one`: its report row plus `ok`,
`error` and `over_budget`. -/
unsafe def replayOneJson (m : Name) (budget : Nat) : IO Json := do
  let started ← IO.monoMsNow
  let (out, count) ← try replayOne m budget
    catch e => pure ({ env := .error e.toString, usage := {} }, 0)
  let row : ModuleReport := { module := m, ms := (← IO.monoMsNow) - started, constants := count,
                              usage := out.usage }
  let error := match out.env with | .error e => some e | .ok _ => none
  return row.toJson.mergeObj <| Json.mkObj [("ok", Lean.toJson error.isNone),
    ("error", match error with | some e => Lean.toJson e | none => Json.null),
    ("over_budget", match out.usage.overBudget with | some n => Lean.toJson n | none => Json.null)]

/-- `replayOne` of owner `m` in a child process (`lcbench-check replay-one`, this executable): its report row, its
error (`none` when the kernel accepted every declaration) and the declaration over the budget. A child that ends
without a report is an error. -/
def replayInChild (m : Name) (budget : Nat) : IO (ModuleReport × Option String × Option String) := do
  let started ← IO.monoMsNow
  let out ← IO.Process.output { cmd := (← IO.appPath).toString,
                                args := #["replay-one", "--kernel-heartbeats", toString budget, m.toString] }
  let ms := (← IO.monoMsNow) - started
  let line := (out.stdout.splitOn "\n").filter (· != "") |>.getLast? |>.getD ""
  let parsed : Except String (ModuleReport × Option String × Option String) := do
    let j ← Json.parse line
    let usage : BudgetReplay.Usage := {
      total := ← j.getObjValAs? Nat "heartbeats", maxDecl := ← j.getObjValAs? Nat "max_declaration_heartbeats",
      maxDeclName := ← j.getObjValAs? String "max_declaration",
      declarations := ← j.getObjValAs? Nat "declarations_checked",
      overBudget := (j.getObjValAs? String "over_budget").toOption }
    let row : ModuleReport := { module := m, ms, constants := ← j.getObjValAs? Nat "constants", usage }
    let error := if ← j.getObjValAs? Bool "ok" then none
      else some ((j.getObjValAs? String "error").toOption.getD "replay-one reported no error text")
    return (row, error, usage.overBudget)
  match parsed with
  | .ok result => return result
  | .error _ =>
    return ({ module := m, ms, constants := 0 },
            some s!"replay-one {m} exited with code {out.exitCode} without a report\n{out.stderr}", none)

structure Report where
  /-- "shared" (one background import) or "per_module" (stock `leanchecker` semantics, one import per owner). -/
  mode : String
  /-- Why the shared mode was not used (empty in shared mode). -/
  fallbackReason : String := ""
  /-- Copies of a name the shared environment already held, each sent to the kernel under a fresh name. -/
  duplicateCopies : Nat := 0
  modules : Array ModuleReport := #[]
  importMs : Nat := 0
  /-- The owner module whose replay failed and the kernel's message; `none` when every owner replayed. -/
  failure : Option (Name × String) := none
  /-- The kernel heartbeat budget per declaration (0 = none) and the declaration that exceeded it. -/
  budget : Nat := 0
  overBudget : Option String := none

/-- Owners in an order where every owner comes after the owners it imports (stable in the given order). -/
def importOrder (owners : Array Name) (importsOf : Name → Array Name) : Except String (Array Name) := Id.run do
  let mut done : NameSet := {}
  let mut order : Array Name := #[]
  let mut progress := true
  while progress && order.size < owners.size do
    progress := false
    for o in owners do
      if done.contains o then continue
      if (importsOf o).all fun i => !owners.contains i || done.contains i then
        done := done.insert o
        order := order.push o
        progress := true
  if order.size < owners.size then return .error "the owner modules import each other in a cycle"
  return .ok order

/-- The modules `m` imports, directly or not: owners through their `.olean` headers, background modules through the
imported environment. -/
def importClosureOf (env : Environment) (ownerImports : Std.HashMap Name (Array Name)) (m : Name) : NameSet := Id.run do
  let mut seen : NameSet := {}
  let mut stack := (ownerImports[m]?).getD #[] |>.toList
  while !stack.isEmpty do
    let x := stack.head!
    stack := stack.tail!
    if seen.contains x then continue
    seen := seen.insert x
    let next := match ownerImports[x]? with
      | some is => is
      | none => match env.getModuleIdx? x with
        | some idx => env.header.moduleData[idx.toNat]!.imports.map fun (i : Import) => i.module
        | none => #[]
    stack := next.toList ++ stack
  return seen

/-- The structure names of the projections in `e`, each subterm visited once. -/
unsafe def projStructsUnsafe (e : Expr) : NameSet :=
  let rec visit (e : Expr) : StateM (PtrSet Expr × NameSet) Unit := do
    if (← get).1.contains e then return
    modify fun (seen, names) => (seen.insert e, names)
    match e with
    | .forallE _ d b _ | .lam _ d b _ => visit d; visit b
    | .letE _ t v b _ => visit t; visit v; visit b
    | .app f a => visit f; visit a
    | .mdata _ b => visit b
    | .proj s _ b => modify (fun (seen, names) => (seen, names.insert s)); visit b
    | _ => pure ()
  ((visit e).run (mkPtrSet, {})).2.2

@[implemented_by projStructsUnsafe]
opaque projStructs (e : Expr) : NameSet := {}

/-- Every constant a declaration uses: `ConstantInfo.getUsedConstantsAsSet` (the constants of its type and value)
plus the structure names of the projections in them, which `getUsedConstants` leaves out. -/
def usedNames (ci : ConstantInfo) : NameSet :=
  ci.getUsedConstantsAsSet ++ projStructs ci.type ++
    match ci.value? (allowOpaque := true) with
    | some v => projStructs v
    | none => {}

/-- Two copies of one name that Lean's import keeps as one: theorems with equal type, universe parameters and `all`
(`subsumesInfo`, Lean v4.32.0 `src/lean/Lean/Environment.lean:2209`; the proofs may differ). -/
def mergeableTheorems (a b : ConstantInfo) : Bool :=
  match a, b with
  | .thmInfo x, .thmInfo y => x.type == y.type && x.levelParams == y.levelParams && x.all == y.all
  | _, _ => false

/-- The same declaration: kind, universe parameters, type and value (`Expr` `==`). -/
def sameDecl (a b : ConstantInfo) : Bool :=
  let kind := fun (c : ConstantInfo) => match c with
    | .thmInfo _ => 0 | .defnInfo _ => 1 | .opaqueInfo _ => 2 | _ => 3
  kind a < 3 && kind a == kind b && a.levelParams == b.levelParams && a.type == b.type &&
    a.value? (allowOpaque := true) == b.value? (allowOpaque := true)

/-- The copy of `n` the shared environment holds before any owner is replayed or while owner `o` is: the background's,
else that of the first owner in import order that declares `n` (`firstOwner`). -/
def heldCopy (env : Environment) (firstOwner : Std.HashMap Name Name)
    (constsOf : Std.HashMap Name (Std.HashMap Name ConstantInfo)) (n : Name) : Option ConstantInfo :=
  match env.find? n with
  | some c => some c
  | none => firstOwner[n]? >>= fun o => ((constsOf[o]?).getD {})[n]?

/-- Why the shared environment could accept something that stock `leanchecker` rejects, if it could. `order` is the
owners in import order (`importOrder`); the copy of a name the shared environment holds is `heldCopy`. -/
def scopeProblem (env : Environment) (order : Array Name) (ownerImports : Std.HashMap Name (Array Name))
    (constsOf : Std.HashMap Name (Std.HashMap Name ConstantInfo)) : Option String := Id.run do
  for o in order do
    if env.getModuleIdx? o |>.isSome then
      return some s!"a background module imports the charged module {o}"
  -- a name an owner declares that the shared environment already holds (from the background or an earlier owner)
  -- must be the same theorem statement, and the copy must not refer to its own name: it is checked under a fresh
  -- name, where a reference to the name would reach the held copy instead of being unknown, as in its own module
  let mut firstOwner : Std.HashMap Name Name := {}
  for o in order do
    for (n, ci) in (constsOf[o]?).getD {} do
      let clash := if env.contains n then some s!"{n} (in {o}) is also a background constant"
        else (firstOwner[n]?).map fun o' => s!"{n} is declared in both {o'} and {o}"
      match clash, heldCopy env firstOwner constsOf n with
      | some why, some held =>
        if !mergeableTheorems held ci then
          return some s!"{why}, and the two are not the same theorem statement"
        if (usedNames ci).contains n then
          return some s!"{why}, and the copy in {o} refers to its own name"
      | _, _ => firstOwner := firstOwner.insert n o
  -- Every module (background or owner) that declares a name more than one module declares, with its copy. Lean's
  -- import keeps one copy of such a theorem and records the first module loaded as its home, which can lie outside an
  -- owner's import closure although a module inside it declares the name too.
  let mut first : Std.HashMap Name (Name × ConstantInfo) := {}
  let mut multi : Std.HashMap Name (Array (Name × ConstantInfo)) := {}
  for d in env.header.moduleData, m in env.header.moduleNames do
    for n in d.constNames, ci in d.constants do
      match first[n]? with
      | none => first := first.insert n (m, ci)
      | some f => multi := multi.insert n (((multi[n]?).getD #[f]).push (m, ci))
  for o in order do
    for (n, ci) in (constsOf[o]?).getD {} do
      match first[n]? with
      | none => first := first.insert n (o, ci)
      | some f => multi := multi.insert n (((multi[n]?).getD #[f]).push (o, ci))
  for o in order do
    let scope := importClosureOf env ownerImports o
    let own := (constsOf[o]?).getD {}
    for (n, ci) in own do
      for c in usedNames ci do
        if let some decls := multi[c]? then
          -- the copies of c that o's own check would see (o's and its import closure's) must all equal the copy the
          -- shared environment holds, so that o's declarations are checked against the same constant
          let onPath := decls.filter fun (m, _) => m == o || scope.contains m
          if !onPath.isEmpty then
            let same := match heldCopy env firstOwner constsOf c with
              | some held => onPath.all fun (_, d) => sameDecl d held
              | none => false
            if !same then
              return some (s!"{n} (in {o}) uses {c}, of which a copy in {o} or its imports differs from the one " ++
                "the shared environment holds")
            continue
        let home := if own.contains c then some o else match firstOwner[c]? with
          | some o' => some o'
          | none => (env.getModuleIdxFor? c).map fun idx => env.header.moduleNames[idx.toNat]!
        match home with
        | some h => if h != o && !scope.contains h then
            return some s!"{n} (in {o}) uses {c} from {h}, which {o} does not import"
        | none => return some s!"{n} (in {o}) uses {c}, which no imported module declares"
  return none

/-- A name for checking a copy of `n` that is not `taken`: `_lcbench_copy.n`, numbered when needed. -/
def copyName (n : Name) (taken : Name → Bool) : Name := Id.run do
  let base := `_lcbench_copy ++ n
  if !taken base then return base
  let mut k := 1
  while taken (Name.mkNum base k) do
    k := k + 1
  return Name.mkNum base k

/-- Each owner's constants to replay in the shared environment: a copy of a name the environment already holds (the
background's, or an earlier owner's in import order) under a fresh name (`copyName`), so the kernel checks it in its
own owner's replay; the number of such copies. Run after `scopeProblem` found none. -/
def renameCopies (env : Environment) (order : Array Name)
    (constsOf : Std.HashMap Name (Std.HashMap Name ConstantInfo)) :
    Std.HashMap Name (Std.HashMap Name ConstantInfo) × Nat := Id.run do
  let mut declared : NameSet := {}
  for o in order do
    for (n, _) in (constsOf[o]?).getD {} do
      declared := declared.insert n
  let mut seen : NameSet := {}
  let mut used : NameSet := {}
  let mut out : Std.HashMap Name (Std.HashMap Name ConstantInfo) := {}
  let mut copies := 0
  for o in order do
    let mut mine : Std.HashMap Name ConstantInfo := {}
    for (n, ci) in (constsOf[o]?).getD {} do
      if env.contains n || seen.contains n then
        match ci with
        | .thmInfo info =>
          let fresh := copyName n fun x => env.contains x || declared.contains x || used.contains x
          used := used.insert fresh
          mine := mine.insert fresh (.thmInfo { info with name := fresh })
          copies := copies + 1
        | _ => mine := mine.insert n ci
      else
        mine := mine.insert n ci
    for (n, _) in (constsOf[o]?).getD {} do
      seen := seen.insert n
    out := out.insert o mine
  return (out, copies)

/-- Kernel replay of every owner module (see the module docstring). Initializers of imported modules never run.
The shared environment's `.olean` regions are not freed: the process exits after the replay, and freeing them while
the environment is still referenced would leave dangling objects. -/
unsafe def replayOwners (owners : Array Name) (budget : Nat) : IO Report := do
  let mut partsOf : Std.HashMap Name (Array (ModuleData × CompactedRegion)) := {}
  let mut ownerImports : Std.HashMap Name (Array Name) := {}
  let mut constsOf : Std.HashMap Name (Std.HashMap Name ConstantInfo) := {}
  let mut imports : Array Import := #[]
  for o in owners do
    let parts ← readParts o
    partsOf := partsOf.insert o parts
    ownerImports := ownerImports.insert o ((firstData parts).imports.map fun (i : Import) => i.module)
    constsOf := constsOf.insert o (ownConstants parts)
    for i in (firstData parts).imports do
      if !owners.contains i.module && !imports.contains i then imports := imports.push i
  let order ← match importOrder owners fun o => (ownerImports[o]?).getD #[] with
    | .ok order => pure order
    | .error e => throw (IO.userError e)
  let t0 ← IO.monoMsNow
  let (_, s) ← importModulesCore imports |>.run
  let background ← finalizeImport s imports {} 0 false false (isModule := true)
  let t1 ← IO.monoMsNow
  if let some reason := scopeProblem background order ownerImports constsOf then
    let mut report : Report := { mode := "per_module", fallbackReason := reason, importMs := t1 - t0, budget }
    for o in owners do
      let (row, error, overBudget) ← replayInChild o budget
      report := { report with modules := report.modules.push row }
      if let some e := error then
        return { report with failure := some (o, e), overBudget }
    return report
  let (toReplay, copies) := renameCopies background order constsOf
  let mut env := background
  let mut report : Report := { mode := "shared", importMs := t1 - t0, budget, duplicateCopies := copies }
  for o in order do
    let consts := (toReplay[o]?).getD {}
    let started ← IO.monoMsNow
    let out ← BudgetReplay.replay consts env budget
    let row : ModuleReport := { module := o, ms := (← IO.monoMsNow) - started, constants := consts.size,
                                usage := out.usage }
    report := { report with modules := report.modules.push row }
    match out.env with
    | .ok env' => env := env'
    | .error e => return { report with failure := some (o, e), overBudget := out.usage.overBudget }
  return report

def Report.toJson (r : Report) : Json :=
  Json.mkObj [("mode", Lean.toJson r.mode), ("fallback_reason", Lean.toJson r.fallbackReason),
    ("duplicate_copies_checked", Lean.toJson r.duplicateCopies),
    ("import_ms", Lean.toJson r.importMs),
    ("modules", Json.arr (r.modules.map ModuleReport.toJson)),
    ("kernel_heartbeats_budget", Lean.toJson r.budget),
    ("over_budget", match r.overBudget with | some n => Lean.toJson n | none => Json.null),
    ("ok", Lean.toJson r.failure.isNone),
    ("failed_module", match r.failure with | some (m, _) => Lean.toJson m.toString | none => Json.null),
    ("error", match r.failure with | some (_, e) => Lean.toJson e | none => Json.null)]

end LcbenchTools.Replay
