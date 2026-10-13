-- ABOUTME: Validity facts of a built submission (statements, axioms, frozen closures, owner imports, outside references)
-- ABOUTME: read from the imported owner modules; compiled into lcbench-check, so participant initializers never run.
import Lean
open Lean
set_option maxRecDepth 100000

namespace LcbenchTools.Extract

/-! Node rule `expr_nodes_with_levels_v1` (same as the toy descent extractor). -/
private def bk_levelNodes : Level → Nat
  | .zero => 1
  | .succ u => 1 + bk_levelNodes u
  | .max u v | .imax u v => 1 + bk_levelNodes u + bk_levelNodes v
  | .param _ | .mvar _ => 1
private def bk_nodes : Expr → Nat
  | .bvar _ | .fvar _ | .mvar _ => 1
  | .sort u => 1 + bk_levelNodes u
  | .const _ us => 1 + us.foldl (fun n u => n + bk_levelNodes u) 0
  | .app f a => 1 + bk_nodes f + bk_nodes a
  | .lam _ t b _ | .forallE _ t b _ => 1 + bk_nodes t + bk_nodes b
  | .letE _ t v b _ => 1 + bk_nodes t + bk_nodes v + bk_nodes b
  | .lit _ => 1
  | .mdata _ e => bk_nodes e
  | .proj _ _ e => 1 + bk_nodes e

/-! Canonical serialization of kernel objects. Metadata is transparent; binder names (without macro scopes) and
binder info are kept so that a statement hash pins the exact elaborated statement. Macro scopes are erased: an
anonymous instance binder is named `inst._@.<module>.<ctx>._hygCtx._hyg.N`, and Lean derives `<ctx>` from the
declaration name plus the number of earlier declarations in the file with the same name, so deleting an unrelated
same-named helper would otherwise change a frozen definition's hash. -/
private def bk_serL : Level → String
  | .zero => "0"
  | .succ u => s!"(S {bk_serL u})"
  | .max u v => s!"(M {bk_serL u} {bk_serL v})"
  | .imax u v => s!"(I {bk_serL u} {bk_serL v})"
  | .param n => s!"(p {n})"
  | .mvar _ => "(?lmvar)"
private def bk_serBI : BinderInfo → String
  | .default => "d" | .implicit => "i" | .strictImplicit => "s" | .instImplicit => "c"
private def bk_serLit : Literal → String
  | .natVal n => s!"(n {n})"
  | .strVal s => s!"(s {s.quote})"
/-- `constSer` prints constants; everything else is structural. -/
private partial def bk_serWith (constSer : Name → List Level → String) : Expr → String
  | .bvar i => s!"#{i}"
  | .fvar _ => "(?fvar)"
  | .mvar _ => "(?mvar)"
  | .sort u => s!"(T {bk_serL u})"
  | .const n us => constSer n us
  | .app f a => s!"(A {bk_serWith constSer f} {bk_serWith constSer a})"
  | .lam n t b bi => s!"(L {n.eraseMacroScopes} {bk_serBI bi} {bk_serWith constSer t} {bk_serWith constSer b})"
  | .forallE n t b bi => s!"(P {n.eraseMacroScopes} {bk_serBI bi} {bk_serWith constSer t} {bk_serWith constSer b})"
  | .letE n t v b nd =>
    s!"(D {n.eraseMacroScopes} {nd} {bk_serWith constSer t} {bk_serWith constSer v} {bk_serWith constSer b})"
  | .lit l => bk_serLit l
  | .mdata _ e => bk_serWith constSer e
  | .proj s i e => s!"(J {s} {i} {bk_serWith constSer e})"
private def bk_plainConst (n : Name) (us : List Level) : String := s!"(C {n} [{" ".intercalate (us.map bk_serL)}])"
private def bk_serE : Expr → String := bk_serWith bk_plainConst

private def bk_serNames (ns : List Name) : String := "[" ++ " ".intercalate (ns.map toString) ++ "]"
private def bk_serHints : ReducibilityHints → String
  | .opaque => "opaque" | .abbrev => "abbrev" | .regular h => s!"regular {h}"
private def bk_serSafety : DefinitionSafety → String
  | .unsafe => "unsafe" | .safe => "safe" | .partial => "partial"

private def bk_serQuot : QuotKind → String
  | .type => "type" | .ctor => "ctor" | .lift => "lift" | .ind => "ind"

private def bk_serCIWith (bk_serE : Expr → String) : ConstantInfo → String
  | .axiomInfo v => s!"(axiom {v.name} {bk_serNames v.levelParams} {v.isUnsafe} {bk_serE v.type})"
  | .defnInfo v => s!"(def {v.name} {bk_serNames v.levelParams} {bk_serHints v.hints} {bk_serSafety v.safety} {bk_serNames v.all} {bk_serE v.type} {bk_serE v.value})"
  | .thmInfo v => s!"(thm {v.name} {bk_serNames v.levelParams} {bk_serNames v.all} {bk_serE v.type} {bk_serE v.value})"
  | .opaqueInfo v => s!"(opaque {v.name} {bk_serNames v.levelParams} {v.isUnsafe} {bk_serNames v.all} {bk_serE v.type} {bk_serE v.value})"
  | .quotInfo v => s!"(quot {v.name} {bk_serNames v.levelParams} {bk_serQuot v.kind} {bk_serE v.type})"
  | .inductInfo v => s!"(ind {v.name} {bk_serNames v.levelParams} {v.numParams} {v.numIndices} {bk_serNames v.all} {bk_serNames v.ctors} {v.numNested} {v.isRec} {v.isUnsafe} {v.isReflexive} {bk_serE v.type})"
  | .ctorInfo v => s!"(ctor {v.name} {bk_serNames v.levelParams} {v.induct} {v.cidx} {v.numParams} {v.numFields} {v.isUnsafe} {bk_serE v.type})"
  | .recInfo v =>
    let rules := " ".intercalate (v.rules.map fun r => s!"(rule {r.ctor} {r.nfields} {bk_serE r.rhs})")
    s!"(rec {v.name} {bk_serNames v.levelParams} {bk_serNames v.all} {v.numParams} {v.numIndices} {v.numMotives} {v.numMinors} {v.k} {v.isUnsafe} {bk_serE v.type} [{rules}])"

private def bk_serCI : ConstantInfo → String := bk_serCIWith bk_serE

private def bk_kindOf : ConstantInfo → String
  | .thmInfo _ => "theorem" | .defnInfo _ => "def" | .opaqueInfo _ => "opaque"
  | .inductInfo _ => "inductive" | .ctorInfo _ => "constructor" | .recInfo _ => "recursor"
  | .axiomInfo _ => "axiom" | .quotInfo _ => "quot"

/-- Constant names used by an expression, including projection structure names. -/
private partial def bk_semanticNames (e : Expr) (acc : Array Name) : Array Name :=
  match e with
  | .bvar _ | .fvar _ | .mvar _ | .sort _ | .lit _ => acc
  | .const n _ => acc.push n
  | .app f a => bk_semanticNames a (bk_semanticNames f acc)
  | .lam _ t b _ | .forallE _ t b _ => bk_semanticNames b (bk_semanticNames t acc)
  | .letE _ t v b _ => bk_semanticNames b (bk_semanticNames v (bk_semanticNames t acc))
  | .mdata _ b => bk_semanticNames b acc
  | .proj s _ b => bk_semanticNames b (acc.push s)

/-- Reachability rule of the repo's proof-patch selector (HelperClosure.lean / infoDependencies):
type and value constants, inductive family members, constructors and owner recursors, recursor rules. -/
private def bk_deps (recs : Std.HashMap Name (Array Name)) (ci : ConstantInfo) : Array Name := Id.run do
  let mut out := bk_semanticNames ci.type #[]
  if let some v := ci.value? (allowOpaque := true) then out := bk_semanticNames v out
  match ci with
  | .inductInfo i => out := out ++ i.all.toArray ++ i.ctors.toArray ++ (recs[ci.name]?).getD #[]
  | .ctorInfo c => out := out.push c.induct
  | .recInfo r =>
      out := out ++ r.all.toArray
      for rule in r.rules do out := bk_semanticNames rule.rhs (out.push rule.ctor)
  | _ => pure ()
  return out

private def bk_moduleOf (env : Environment) (n : Name) : Option Name :=
  (env.getModuleIdxFor? n).map fun idx => env.header.moduleNames[idx.toNat]!

/-- Recursors declared in owner modules, keyed by each inductive in their `all` list. -/
private def bk_ownerRecursors (env : Environment) (owners : List Name) : Std.HashMap Name (Array Name) := Id.run do
  let mut recs : Std.HashMap Name (Array Name) := {}
  for o in owners do
    let some idx := env.getModuleIdx? o | continue
    for n in env.header.moduleData[idx.toNat]!.constNames do
      if let some (.recInfo v) := env.find? n then
        for m in v.all do recs := recs.insert m ((recs[m]?).getD #[] |>.push v.name)
  return recs

/-- Breadth-first bk_closure through owner-module declarations; returns (charged, uncharged).
With `thmTypeOnly`, theorems contribute only their statement (used for frozen/statement closures). -/
private def bk_closure (env : Environment) (owners : List Name) (roots : Array Name) (thmTypeOnly := false) :
    Except String (Array Name × NameSet) := Id.run do
  let recs := bk_ownerRecursors env owners
  let mut queue := roots
  let mut seen : NameSet := {}
  let mut order : Array Name := #[]
  let mut outside : NameSet := {}
  let mut i := 0
  while i < queue.size do
    let n := queue[i]!
    i := i + 1
    if seen.contains n then continue
    seen := seen.insert n
    let some ci := env.find? n | return .error s!"missing constant {n}"
    if (bk_moduleOf env n).any owners.contains then
      order := order.push n
      match ci with
      | .thmInfo v => queue := queue ++ (if thmTypeOnly then bk_semanticNames v.type #[] else bk_deps recs ci)
      | _ => queue := queue ++ bk_deps recs ci
    else
      outside := outside.insert n
  return .ok (order, outside)

/-! Proof-irrelevant serialization for frozen closures. A frozen definition is compared by its meaning: the types and
non-proof values of the declarations in its closure. Every proof inside them (a subterm whose type is a proposition,
`Meta.isProof`) prints as the one placeholder `(prf)`, so a proof written as a tactic block or as a term, inline or
abstracted into an auxiliary `X._proof_N` theorem, gives the same text. Propositions themselves are types, not proofs,
and are still printed in full. A proof's proposition is fixed by its position (the binder type of the function it is
passed to, a `let` type, or the declaration type), so two values with the same text are definitionally equal by proof
irrelevance. Constants that occur only inside proofs are not part of the closure. Binders are opened with local
declarations so that `isProof` can type each subterm; a bound variable prints as its de Bruijn index (`#i`), exactly
as `bk_serWith` prints it, so a value without proofs keeps its structural text. -/

/-- The text that stands for every proof inside a frozen closure. -/
private def bk_proofMark : String := "(prf)"

/-- Fails closed: a subterm whose type cannot be inferred is compared structurally, never erased. Sorts, function types
and literals are never proofs. -/
private def bk_isProof (e : Expr) : MetaM Bool := do
  match e with
  | .sort _ | .forallE .. | .lit _ => return false
  | _ => try Meta.isProof e catch _ => return false

/-- Proof-irrelevant text of `e`; `depth` maps the local declarations opened for enclosing binders to their binder
depth. The constants printed (not those inside a proof) and projection structure names are appended to `uses`. -/
private partial def bk_piSer (uses : IO.Ref (Array Name)) (depth : Std.HashMap FVarId Nat) (e : Expr) :
    MetaM String := do
  if ← bk_isProof e then return bk_proofMark
  match e with
  | .bvar i => return s!"#{i}"
  | .fvar id => return match depth[id]? with
    | some d => s!"#{depth.size - 1 - d}"
    | none => "(?fvar)"
  | .mvar _ => return "(?mvar)"
  | .sort u => return s!"(T {bk_serL u})"
  | .const n us =>
    uses.modify (·.push n)
    return bk_plainConst n us
  | .app f a =>
    let fs ← bk_piSer uses depth f
    let as ← bk_piSer uses depth a
    return s!"(A {fs} {as})"
  | .lam n t b bi =>
    let ts ← bk_piSer uses depth t
    let bs ← Meta.withLocalDecl n bi t fun x =>
      bk_piSer uses (depth.insert x.fvarId! depth.size) (b.instantiate1 x)
    return s!"(L {n.eraseMacroScopes} {bk_serBI bi} {ts} {bs})"
  | .forallE n t b bi =>
    let ts ← bk_piSer uses depth t
    let bs ← Meta.withLocalDecl n bi t fun x =>
      bk_piSer uses (depth.insert x.fvarId! depth.size) (b.instantiate1 x)
    return s!"(P {n.eraseMacroScopes} {bk_serBI bi} {ts} {bs})"
  | .letE n t v b nd =>
    let ts ← bk_piSer uses depth t
    let vs ← bk_piSer uses depth v
    let bs ← Meta.withLetDecl n t v (nondep := nd) fun x =>
      bk_piSer uses (depth.insert x.fvarId! depth.size) (b.instantiate1 x)
    return s!"(D {n.eraseMacroScopes} {nd} {ts} {vs} {bs})"
  | .lit l => return bk_serLit l
  | .mdata _ b => bk_piSer uses depth b
  | .proj s i b =>
    uses.modify (·.push s)
    let bs ← bk_piSer uses depth b
    return s!"(J {s} {i} {bs})"

/-- Reducibility hints without the definitional height: Lean computes the height from every constant in the value,
including constants inside proofs, so it is not part of a definition's meaning. -/
private def bk_piHints : ReducibilityHints → String
  | .opaque => "opaque" | .abbrev => "abbrev" | .regular _ => "regular"

/-- One owner declaration of a frozen closure: its proof-irrelevant text and the constants that text uses (the next
declarations of the closure). Inductive families add their members, constructors and owner recursors; constructors
their inductive; recursors their rules. -/
private def bk_piDecl (recs : Std.HashMap Name (Array Name)) (ci : ConstantInfo) : MetaM (String × Array Name) := do
  let uses ← IO.mkRef (#[] : Array Name)
  let ser := bk_piSer uses {}
  let text ← do match ci with
    | .axiomInfo v => pure s!"(axiom {v.name} {bk_serNames v.levelParams} {v.isUnsafe} {← ser v.type})"
    | .defnInfo v =>
      pure s!"(def {v.name} {bk_serNames v.levelParams} {bk_piHints v.hints} {bk_serSafety v.safety} {bk_serNames v.all} {← ser v.type} {← ser v.value})"
    | .thmInfo v => pure s!"(thm {v.name} {bk_serNames v.levelParams} {bk_serNames v.all} {← ser v.type} {← ser v.value})"
    | .opaqueInfo v =>
      pure s!"(opaque {v.name} {bk_serNames v.levelParams} {v.isUnsafe} {bk_serNames v.all} {← ser v.type} {← ser v.value})"
    | .quotInfo v => pure s!"(quot {v.name} {bk_serNames v.levelParams} {bk_serQuot v.kind} {← ser v.type})"
    | .inductInfo v =>
      uses.modify (· ++ v.all.toArray ++ v.ctors.toArray ++ (recs[v.name]?).getD #[])
      pure s!"(ind {v.name} {bk_serNames v.levelParams} {v.numParams} {v.numIndices} {bk_serNames v.all} {bk_serNames v.ctors} {v.numNested} {v.isRec} {v.isUnsafe} {v.isReflexive} {← ser v.type})"
    | .ctorInfo v =>
      uses.modify (·.push v.induct)
      pure s!"(ctor {v.name} {bk_serNames v.levelParams} {v.induct} {v.cidx} {v.numParams} {v.numFields} {v.isUnsafe} {← ser v.type})"
    | .recInfo v =>
      uses.modify (· ++ v.all.toArray ++ (v.rules.map (·.ctor)).toArray)
      let rules ← v.rules.mapM fun r => do pure s!"(rule {r.ctor} {r.nfields} {← ser r.rhs})"
      pure s!"(rec {v.name} {bk_serNames v.levelParams} {bk_serNames v.all} {v.numParams} {v.numIndices} {v.numMotives} {v.numMinors} {v.k} {v.isUnsafe} {← ser v.type} [{" ".intercalate rules}])"
  return (text, ← uses.get)

/-- The frozen closures of `frozen`: for each, the owner-module declarations reached from it through non-proof
positions, in breadth-first order, with their proof-irrelevant texts. A declaration's text does not depend on the root,
so it is computed once. `none` marks a frozen name that is not in the environment. -/
private def bk_frozenClosures (env : Environment) (owners : List Name) (recs : Std.HashMap Name (Array Name))
    (frozen : List Name) : MetaM (Array (Name × Option (Array Name × Array String))) := do
  let mut memo : Std.HashMap Name (String × Array Name) := {}
  let mut out := #[]
  for f in frozen do
    if !env.contains f then
      out := out.push (f, none)
      continue
    let mut queue := #[f]
    let mut seen : NameSet := {}
    let mut order : Array Name := #[]
    let mut sers : Array String := #[]
    let mut i := 0
    while i < queue.size do
      let n := queue[i]!
      i := i + 1
      if seen.contains n then continue
      seen := seen.insert n
      let some ci := env.find? n | throwError "missing constant {n}"
      if (bk_moduleOf env n).any owners.contains then
        let (text, uses) ← match memo[n]? with
          | some r => pure r
          | none => bk_piDecl recs ci
        memo := memo.insert n (text, uses)
        order := order.push n
        sers := sers.push text
        queue := queue ++ uses
    out := out.push (f, some (order, sers))
  return out

/-- Fails the run with a message (the exit code is nonzero, so the caller reports stage `extract`). -/
private def bk_fail {α : Type} (msg : String) : IO α := throw (IO.userError msg)

/-- What the verifier asks the extractor for. -/
structure Request where
  owners : List Name
  roots : Array Name
  frozen : List Name
  /-- Serialize every charged declaration (`ser`); off in the verifier, which exports with the metric counter. -/
  withExport : Bool := false
  /-- Charged closure and node counts; off in the verifier: validity facts only, the counter scores. -/
  withClosure : Bool := false

/-- The validity facts as one JSON object, and the summary line `total=N charged=M`. `env` holds the owner modules
imported by a compiled organizer executable (`importModules (loadExts := false) (level := .private)`), which never
executes initializers: no `initialize`, attribute or macro of the participant runs, and no organizer command is
elaborated in an environment that contains them. -/
def extract (env : Environment) (req : Request) : IO (Json × String) := do
  let owners := req.owners
  let roots := req.roots
  let frozen := req.frozen
  let withExport := req.withExport
  let withClosure := req.withClosure
  -- Transitive import closure of the owner modules.
  let mut importQueue : Array Name := #[]
  for o in owners do
    let some idx := env.getModuleIdx? o | bk_fail s!"owner module {o} not loaded"
    importQueue := importQueue ++ (env.header.moduleData[idx.toNat]!.imports.map (·.module))
  let mut importSeen : NameSet := {}
  let mut j := 0
  while j < importQueue.size do
    let m := importQueue[j]!
    j := j + 1
    if importSeen.contains m then continue
    importSeen := importSeen.insert m
    if let some idx := env.getModuleIdx? m then
      importQueue := importQueue ++ (env.header.moduleData[idx.toNat]!.imports.map (·.module))
  let missingRoots := roots.filter (fun n => !env.contains n)
  let presentRoots := roots.filter env.contains
  let (order, outside) ← if !withClosure then pure (#[], ({} : NameSet)) else
    match bk_closure env owners presentRoots with
    | .ok r => pure r
    | .error e => bk_fail e
  let mut rows : Array Json := #[]
  let mut total := 0
  for n in order do
    let some ci := env.find? n | bk_fail s!"missing {n}"
    let isRoot := roots.contains n
    let t := bk_nodes ci.type
    let v := match ci.value? (allowOpaque := true) with | some e => bk_nodes e | none => 0
    let r := match ci with | .recInfo info => info.rules.foldl (fun acc rule => acc + bk_nodes rule.rhs) 0 | _ => 0
    let cost := if isRoot then v else t + v + r
    total := total + cost
    rows := rows.push <| Json.mkObj [
      ("name", toJson n.toString), ("kind", toJson (bk_kindOf ci)),
      ("role", toJson (if isRoot then "target" else "helper")),
      ("type_nodes", toJson t), ("value_nodes", toJson v), ("rule_nodes", toJson r), ("cost", toJson cost),
      ("ser", toJson (if withExport then bk_serCI ci else ""))]
  let mut targets : Array Json := #[]
  for n in presentRoots do
    let some ci := env.find? n | bk_fail s!"missing {n}"
    let axs ← (Lean.collectAxioms n : CoreM (Array Name)).toIO' { fileName := "<lcbench>", fileMap := default } { env }
    targets := targets.push <| Json.mkObj [("name", toJson n.toString), ("kind", toJson (bk_kindOf ci)),
      ("module", toJson ((bk_moduleOf env n).map toString)),
      ("statement_ser", toJson s!"{bk_serNames ci.levelParams} {bk_serE ci.type}"),
      ("axioms", toJson (axs.toList.map toString))]
  let mut frozenRows : Array Json := #[]
  let recs := bk_ownerRecursors env owners
  let closures ← ((bk_frozenClosures env owners recs frozen).run' {} {}).toIO'
    { fileName := "<lcbench>", fileMap := default, maxHeartbeats := 0, maxRecDepth := 100000 } { env }
  for (f, closure) in closures do
    match closure with
    | none => frozenRows := frozenRows.push <| Json.mkObj [("name", toJson f.toString), ("missing", toJson true)]
    | some (fOrder, sers) =>
      frozenRows := frozenRows.push <| Json.mkObj [("name", toJson f.toString), ("missing", toJson false),
        ("closure_names", toJson (fOrder.map toString)), ("closure_sers", toJson sers)]
  -- Non-theorem owner declarations that the target statements depend on (candidates for freezing).
  let mut stmtNames : Array Name := #[]
  for n in presentRoots do
    let some ci := env.find? n | bk_fail s!"missing {n}"
    stmtNames := stmtNames ++ bk_semanticNames ci.type #[]
  let (stmtOrder, _) ← match bk_closure env owners stmtNames (thmTypeOnly := true) with
    | .ok r => pure r
    | .error e => bk_fail e
  let stmtDefs := stmtOrder.filter fun n => match env.find? n with
    | some (.thmInfo _) => false
    | _ => true
  let outsideRows := outside.toList.map fun n =>
    Json.mkObj [("name", toJson n.toString), ("module", toJson ((bk_moduleOf env n).map toString))]
  let out := Json.mkObj [
    ("metric", toJson "expr_nodes_with_levels_v1"),
    ("owners", toJson (owners.map toString)),
    ("owner_import_closure", toJson (importSeen.toList.map toString)),
    ("roots", toJson (roots.map toString)), ("missing_roots", toJson (missingRoots.map toString)),
    ("total", toJson total), ("declarations", toJson rows), ("targets", toJson targets),
    ("frozen", toJson frozenRows), ("outside_refs", toJson outsideRows),
    ("statement_closure", toJson (stmtDefs.map toString))]
  return (out, s!"total={total} charged={order.size}")

end LcbenchTools.Extract
