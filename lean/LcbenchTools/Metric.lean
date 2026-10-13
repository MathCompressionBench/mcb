-- ABOUTME: lcbench metrics over the charged closure of a set of targets: expr_nodes_with_levels_v1 (A, memoized tree
-- ABOUTME: size) and dag_nodes_v1 (distinct nodes of the canonical hash-consed export), in the reference closure order.
import Lean

open Lean

namespace LcbenchTools.Metric

/-! ## Charged closure

The closure rule is the reference evaluator's closure collection with its `infoDependencies`,
`familyRecursors` and `dependencyExprNames` (copied below). The one change is how "background" is decided: a
constant is background iff its defining module is background under the task's rule (`Background.contains`) for
some module that contains it, instead of being looked up in a second, separately imported environment.
The traversal order is kept exactly: the helper order of the export follows it. -/

/-- A module is background iff it is listed explicitly, or equals / is prefixed (`P.`) by a listed prefix. -/
structure Background where
  prefixes : Array String
  modules : Std.HashSet String

def Background.contains (bg : Background) (module : Name) : Bool :=
  let m := module.toString
  bg.modules.contains m || bg.prefixes.any fun p => m == p || m.startsWith (p ++ ".")

-- Copied from the reference evaluator (d8d9c91b...): repeated nodes are visited once and the
-- stack order preserves the first-occurrence order of the recursive traversal.
private def collectSemanticExprNames (expr : Expr) (names : Array Name) : Array Name := Id.run do
  let mut pending := [expr]
  let mut seen : ExprSet := {}
  let mut names := names
  let mut known : NameSet := names.foldl NameSet.insert {}
  while !pending.isEmpty do
    let value := pending.head!
    pending := pending.tail!
    if seen.contains value then continue
    seen := seen.insert value
    match value with
    | .const name _ =>
      if !known.contains name then
        known := known.insert name
        names := names.push name
    | .app f a => pending := f :: a :: pending
    | .lam _ t b _ | .forallE _ t b _ => pending := t :: b :: pending
    | .letE _ t v b _ => pending := t :: v :: b :: pending
    | .mdata _ b => pending := b :: pending
    | .proj name _ b =>
      if !known.contains name then
        known := known.insert name
        names := names.push name
      pending := b :: pending
    | _ => pure ()
  return names

-- Copied from the reference evaluator's `dependencyExprNames`.
def dependencies (expr : Expr) : Array Name :=
  Id.run do
    let mut names := expr.getUsedConstants
    for name in collectSemanticExprNames expr #[] do
      if !names.contains name then
        names := names.push name
    return names

-- Copied from the reference evaluator's `infoDependencies`.
def infoDependencies (info : ConstantInfo) : Array Name := Id.run do
  let mut result := dependencies info.type ++
    ((info.value? (allowOpaque := true)).map dependencies).getD #[]
  match info with
  | .inductInfo value => result := result ++ value.all.toArray ++ value.ctors.toArray
  | .ctorInfo value => result := result.push value.induct
  | .recInfo value =>
    result := result ++ value.all.toArray
    for rule in value.rules do
      result := result.push rule.ctor ++ dependencies rule.rhs
  | _ => pure ()
  return result

-- Copied from the reference evaluator's `familyRecursors`.
def familyRecursors (constants : Array ConstantInfo) : Std.HashMap Name (Array Name) := Id.run do
  let mut result : Std.HashMap Name (Array Name) := {}
  for info in constants do
    if let .recInfo value := info then
      for member in value.all do
        result := result.insert member ((result[member]?).getD #[] |>.push value.name)
  return result

/-- `roots` and every module they import, transitively (the historical evaluator's background environment). -/
def importClosure (env : Environment) (roots : Array String) : Std.HashSet String := Id.run do
  let mut seen : Std.HashSet String := {}
  let mut stack := roots.toList.map String.toName
  while !stack.isEmpty do
    let m := stack.head!
    stack := stack.tail!
    if seen.contains m.toString then continue
    seen := seen.insert m.toString
    if let some idx := env.getModuleIdx? m then
      stack := (env.header.moduleData[idx.toNat]!.imports.map (·.module)).toList ++ stack
  return seen

def moduleOf (env : Environment) (name : Name) : Option Name :=
  (env.getModuleIdxFor? name).map fun idx => env.header.moduleNames[idx.toNat]!

structure Closure where
  /-- Targets, sorted by their string form (the evaluator's order under explicit-background policies). -/
  roots : Array Name
  /-- Every other charged declaration, in first-reached order. -/
  helpers : Array Name
  ledger : Std.HashMap Name ConstantInfo
  /-- Number of distinct background constants referenced from the charged declarations. -/
  backgroundRefs : Nat

/-- The charged closure of `targets`: the reference evaluator's closure collection with module-based background. -/
def collect (env : Environment) (bg : Background) (targets : Array Name) : Except String Closure := do
  let roots := targets.qsort (fun a b => a.toString < b.toString)
  let recursors := familyRecursors (env.constants.toList.toArray.map Prod.snd)
  -- A constant is background iff some background module contains it. Lean may store one realized constant
  -- (an equation lemma, say) in several modules and maps its name to the first one imported, which can be a
  -- charged module; the reference evaluator looked the name up in the background environment instead.
  let mut backgroundNames : NameHashSet := {}
  for h : idx in [:env.header.moduleNames.size] do
    if bg.contains env.header.moduleNames[idx] then
      for name in env.header.moduleData[idx]!.constNames do
        backgroundNames := backgroundNames.insert name
  let isBackground := fun (name : Name) => backgroundNames.contains name
  let mut queue := roots
  let mut ledger : Std.HashMap Name ConstantInfo := {}
  let mut helpers := #[]
  let mut outside : NameSet := {}
  let mut index := 0
  while index < queue.size do
    let name := queue[index]!
    index := index + 1
    let some info := env.find? name | throw s!"CHARGED_DEPENDENCY_MISSING {name}"
    if isBackground name then
      if roots.contains name then throw s!"TARGET_IN_BACKGROUND {name}"
      outside := outside.insert name
    else if !ledger.contains name then
      match info with
      | .thmInfo _ | .defnInfo _ | .opaqueInfo _ | .inductInfo _ | .ctorInfo _ | .recInfo _ => pure ()
      | _ => throw s!"UNSUPPORTED_CHARGED_DECLARATION {name}"
      if info.isUnsafe || info.isPartial || info.type.hasSorry ||
          (info.value? (allowOpaque := true)).any Expr.hasSorry then
        throw s!"FORBIDDEN_CHARGED_DECLARATION {name}"
      ledger := ledger.insert name info
      if !roots.contains name then helpers := helpers.push name
      queue := queue ++ infoDependencies info
      if let .inductInfo family := info then
        for member in family.all do
          queue := queue ++ ((recursors[member]?).getD #[]).qsort (fun a b => a.toString < b.toString)
  return { roots := roots, helpers := helpers, ledger := ledger, backgroundRefs := outside.size }

/-! ## expr_nodes_with_levels_v1 (A) -/

def levelNodes : Level → Nat
  | .zero => 1
  | .succ u => 1 + levelNodes u
  | .max u v => 1 + levelNodes u + levelNodes v
  | .imax u v => 1 + levelNodes u + levelNodes v
  | .param _ => 1
  | .mvar _ => 1

/-- Tree size with universe levels. `mdata` is free; every other constructor is one node. Memoized on `Expr`
equality (alpha-equivalence), under which the tree size is invariant. -/
partial def treeNodes (e : Expr) : StateM (Std.HashMap Expr Nat) Nat := do
  if let some n := (← get)[e]? then return n
  let n ← match e with
    | .bvar _ | .fvar _ | .mvar _ | .lit _ => pure 1
    | .sort u => pure (1 + levelNodes u)
    | .const _ us => pure (1 + us.foldl (fun acc u => acc + levelNodes u) 0)
    | .app f a => pure <| 1 + (← treeNodes f) + (← treeNodes a)
    | .lam _ t b _ | .forallE _ t b _ => pure <| 1 + (← treeNodes t) + (← treeNodes b)
    | .letE _ t v b _ => pure <| 1 + (← treeNodes t) + (← treeNodes v) + (← treeNodes b)
    | .mdata _ b => treeNodes b
    | .proj _ _ b => pure <| 1 + (← treeNodes b)
  modify fun cache => cache.insert e n
  return n

/-- The charged expressions of a declaration: a target pays its value; any other charged declaration pays its
type, its value (if any) and its recursor rule right-hand sides. -/
def chargedExprs (info : ConstantInfo) (isRoot : Bool) : Array Expr :=
  let value := (info.value? (allowOpaque := true)).toArray
  let rules := match info with
    | .recInfo v => v.rules.toArray.map (·.rhs)
    | _ => #[]
  if isRoot then value else #[info.type] ++ value ++ rules

/-! ## Canonical export and dag_nodes_v1

Every charged expression is hash-consed into tables of names, levels and expression nodes. Metadata is erased,
binder names are dropped; binder info, `let` non-dependency flags, literals, projection indices and levels are
kept. `dag_nodes_v1` is the number of distinct expression nodes reachable from the charged expressions (target
types are exported too, after them, but are not charged). Children are always written before their parents, so
a reader can process the file in one pass. -/

inductive LKey where
  | zero | succ (a : Nat) | max (a b : Nat) | imax (a b : Nat) | param (n : Nat)
  deriving BEq, Hashable

inductive EKey where
  | bvar (i : Nat) | sort (u : Nat) | const (n : Nat) (us : List Nat) | app (f a : Nat)
  | lam (t b : Nat) (bi : Nat) | pi (t b : Nat) (bi : Nat) | letE (t v b : Nat) (nonDep : Bool)
  | natLit (v : Nat) | strLit (v : String) | proj (s i x : Nat)
  deriving BEq, Hashable

structure ExportState where
  names : Std.HashMap Name Nat := {}
  levels : Std.HashMap LKey Nat := {}
  exprs : Std.HashMap EKey Nat := {}
  memo : Std.HashMap ExprStructEq Nat := {}
  lines : Nat := 0

abbrev ExportM := StateT ExportState (ReaderT (Option IO.FS.Handle) IO)

private def emit (j : Json) : ExportM Unit := do
  if let some h ← read then h.putStrLn j.compress
  modify fun s => {s with lines := s.lines + 1}

partial def internName (n : Name) : ExportM Nat := do
  if n.isAnonymous then return 0
  if let some i := (← get).names[n]? then return i
  let (pre, field) ← match n with
    | .str p s => pure ((← internName p), ("s", toJson s))
    | .num p k => pure ((← internName p), ("i", toJson k))
    | .anonymous => unreachable!
  let id := (← get).names.size + 1
  modify fun s => {s with names := s.names.insert n id}
  emit (Json.mkObj [("n", toJson id), ("p", toJson pre), field])
  return id

private def levelLine (id : Nat) : LKey → Json
  | .zero => Json.mkObj [("l", toJson id), ("k", "z")]
  | .succ a => Json.mkObj [("l", toJson id), ("k", "s"), ("a", toJson a)]
  | .max a b => Json.mkObj [("l", toJson id), ("k", "m"), ("a", toJson a), ("b", toJson b)]
  | .imax a b => Json.mkObj [("l", toJson id), ("k", "im"), ("a", toJson a), ("b", toJson b)]
  | .param n => Json.mkObj [("l", toJson id), ("k", "p"), ("n", toJson n)]

partial def internLevel (u : Level) : ExportM Nat := do
  let key : LKey ← match u with
    | .zero => pure .zero
    | .succ a => pure (.succ (← internLevel a))
    | .max a b => pure (.max (← internLevel a) (← internLevel b))
    | .imax a b => pure (.imax (← internLevel a) (← internLevel b))
    | .param n => pure (.param (← internName n))
    | .mvar _ => throw (IO.userError "universe metavariable in a charged declaration")
  if let some i := (← get).levels[key]? then return i
  let id := (← get).levels.size
  modify fun s => {s with levels := s.levels.insert key id}
  emit (levelLine id key)
  return id

private def binderCode : BinderInfo → Nat
  | .default => 0 | .implicit => 1 | .strictImplicit => 2 | .instImplicit => 3

private def exprLine (id : Nat) : EKey → Json
  | .bvar i => Json.mkObj [("e", toJson id), ("k", "bv"), ("i", toJson i)]
  | .sort u => Json.mkObj [("e", toJson id), ("k", "so"), ("u", toJson u)]
  | .const n us => Json.mkObj [("e", toJson id), ("k", "c"), ("n", toJson n), ("us", toJson us)]
  | .app f a => Json.mkObj [("e", toJson id), ("k", "ap"), ("f", toJson f), ("a", toJson a)]
  | .lam t b bi => Json.mkObj [("e", toJson id), ("k", "la"), ("t", toJson t), ("b", toJson b), ("bi", toJson bi)]
  | .pi t b bi => Json.mkObj [("e", toJson id), ("k", "pi"), ("t", toJson t), ("b", toJson b), ("bi", toJson bi)]
  | .letE t v b nd => Json.mkObj [("e", toJson id), ("k", "le"), ("t", toJson t), ("v", toJson v), ("b", toJson b),
      ("nd", toJson nd)]
  | .natLit v => Json.mkObj [("e", toJson id), ("k", "nl"), ("v", toJson (toString v))]
  | .strLit v => Json.mkObj [("e", toJson id), ("k", "sl"), ("v", toJson v)]
  | .proj s i x => Json.mkObj [("e", toJson id), ("k", "pj"), ("s", toJson s), ("i", toJson i), ("x", toJson x)]

partial def internExpr (e : Expr) : ExportM Nat := do
  if let some i := (← get).memo[ExprStructEq.mk e]? then return i
  let id ← match e with
    | .mdata _ b => internExpr b  -- metadata is transparent
    | _ => do
      let key ← exprKey e
      match (← get).exprs[key]? with
      | some i => pure i
      | none => do
        let id := (← get).exprs.size
        modify fun s => {s with exprs := s.exprs.insert key id}
        emit (exprLine id key)
        pure id
  modify fun s => {s with memo := s.memo.insert (ExprStructEq.mk e) id}
  return id
where
  exprKey : Expr → ExportM EKey
    | .bvar i => pure (.bvar i)
    | .sort u => return .sort (← internLevel u)
    | .const n us => return .const (← internName n) (← us.mapM internLevel)
    | .app f a => return .app (← internExpr f) (← internExpr a)
    | .lam _ t b bi => return .lam (← internExpr t) (← internExpr b) (binderCode bi)
    | .forallE _ t b bi => return .pi (← internExpr t) (← internExpr b) (binderCode bi)
    | .letE _ t v b nd => return .letE (← internExpr t) (← internExpr v) (← internExpr b) nd
    | .lit (.natVal v) => pure (.natLit v)
    | .lit (.strVal v) => pure (.strLit v)
    | .proj s i x => return .proj (← internName s) i (← internExpr x)
    | .mdata _ b => exprKey b
    | .fvar _ | .mvar _ => throw (IO.userError "free or meta variable in a charged declaration")

def kindOf : ConstantInfo → String
  | .thmInfo _ => "theorem" | .defnInfo _ => "definition" | .opaqueInfo _ => "opaque"
  | .inductInfo _ => "inductive" | .ctorInfo _ => "constructor" | .recInfo _ => "recursor"
  | .axiomInfo _ => "axiom" | .quotInfo _ => "quotient"

/-- Declaration record: its charged expressions by id, plus the declaration's kernel fields. -/
def internDecl (env : Environment) (info : ConstantInfo) (isRoot : Bool) : ExportM Unit := do
  let nid ← internName info.name
  let lps ← info.levelParams.mapM internName
  let type ← internExpr info.type
  let value ← match info.value? (allowOpaque := true) with
    | some v => pure (some (← internExpr v))
    | none => pure none
  let names := fun (ns : List Name) => ns.mapM internName
  let (rules, fields) ← match info with
    | .recInfo v => do
      let rules ← v.rules.mapM fun r => do
        pure (Json.arr #[toJson (← internName r.ctor), toJson r.nfields, toJson (← internExpr r.rhs)])
      pure (rules, [("all", toJson (← names v.all)), ("numParams", toJson v.numParams),
        ("numIndices", toJson v.numIndices), ("numMotives", toJson v.numMotives),
        ("numMinors", toJson v.numMinors), ("k", toJson v.k), ("isUnsafe", toJson v.isUnsafe)])
    | .inductInfo v => pure ([], [("all", toJson (← names v.all)), ("ctors", toJson (← names v.ctors)),
        ("numParams", toJson v.numParams), ("numIndices", toJson v.numIndices), ("numNested", toJson v.numNested),
        ("isRec", toJson v.isRec), ("isUnsafe", toJson v.isUnsafe), ("isReflexive", toJson v.isReflexive)])
    | .ctorInfo v => pure ([], [("induct", toJson (← internName v.induct)), ("cidx", toJson v.cidx),
        ("numParams", toJson v.numParams), ("numFields", toJson v.numFields), ("isUnsafe", toJson v.isUnsafe)])
    | .defnInfo v =>
      let hints := match v.hints with
        | .opaque => Json.str "opaque" | .abbrev => Json.str "abbrev" | .regular h => toJson h.toNat
      let safety := match v.safety with | .unsafe => "unsafe" | .safe => "safe" | .partial => "partial"
      pure ([], [("hints", hints), ("safety", toJson safety)])
    | .opaqueInfo v => pure ([], [("isUnsafe", toJson v.isUnsafe)])
    | _ => pure ([], [])
  emit <| Json.mkObj ([("d", toJson nid), ("name", toJson info.name.toString), ("kind", toJson (kindOf info)),
    ("role", toJson (if isRoot then "target" else "helper")),
    ("module", toJson ((moduleOf env info.name).map toString)), ("lp", toJson lps),
    ("type", toJson type), ("value", toJson value), ("rules", Json.arr rules.toArray)] ++ fields)

/-! ## Results -/

structure DeclRow where
  name : Name
  isRoot : Bool
  kind : String
  tree : Nat

structure Result where
  closure : Closure
  rows : Array DeclRow
  exprNodesWithLevels : Nat
  dagNodes : Nat
  exportLines : Nat
  ms : List (String × Nat)

/-- Computes A and dag_nodes_v1 for `targets`. With `exportPath`, the canonical export is written there. -/
def measure (env : Environment) (bg : Background) (targets : Array Name) (exportPath : Option System.FilePath)
    (header : Json) : IO Result := do
  let t0 ← IO.monoMsNow
  let closure ← IO.ofExcept (collect env bg targets)
  let t1 ← IO.monoMsNow
  let decls := closure.roots.map (·, true) ++ closure.helpers.map (·, false)
  -- A: one cache for the whole closure (the tree size of an expression does not depend on its declaration).
  let mut cache : Std.HashMap Expr Nat := {}
  let mut trees : Array Nat := #[]
  for (name, isRoot) in decls do
    let mut t := 0
    for e in chargedExprs closure.ledger[name]! isRoot do
      let (n, c) := (treeNodes e).run cache
      cache := c
      t := t + n
    trees := trees.push t
  cache := {}
  let t2 ← IO.monoMsNow
  -- Canonical export and dag_nodes_v1.
  let handle ← match exportPath with
    | some p => pure (some (← IO.FS.Handle.mk p .write))
    | none => pure none
  -- Phase 1 interns exactly the charged expressions, so the table size after it is dag_nodes_v1. Phase 2 adds
  -- the uncharged target statements and writes one record per declaration.
  let body : ExportM Nat := do
    emit header
    for (name, isRoot) in decls do
      for e in chargedExprs closure.ledger[name]! isRoot do
        discard <| internExpr e
    let dag := (← get).exprs.size
    for (name, isRoot) in decls do
      internDecl env closure.ledger[name]! isRoot
    return dag
  let (dag, st) ← (body.run {}).run handle
  if let some h := handle then h.flush
  let t3 ← IO.monoMsNow
  let rows := (decls.zip trees).map fun ((name, isRoot), t) =>
    ({ name := name, isRoot := isRoot, kind := kindOf closure.ledger[name]!, tree := t } : DeclRow)
  let total := trees.foldl (· + ·) 0
  let ms := [("closure", t1 - t0), ("expr_nodes_with_levels_v1", t2 - t1), ("export_and_dag", t3 - t2)]
  return Result.mk closure rows total dag st.lines ms

end LcbenchTools.Metric
