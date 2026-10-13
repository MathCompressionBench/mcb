-- ABOUTME: The metric counter's JSON request (modules, targets, background, export, per_declaration) and result, shared
-- ABOUTME: by the lcbench-metric and lcbench-check executables so that both print the same lcbench.metric_result.v1.
import LcbenchTools.Metric

open Lean LcbenchTools.Metric

namespace LcbenchTools.Request

def field? (j : Json) (key : String) : Option Json :=
  match j.getObjVal? key with
  | .ok Json.null => none
  | .ok v => some v
  | .error _ => none

def strings (j : Json) (key : String) : Except String (Array String) :=
  match field? j key with
  | some v => fromJson? v
  | none => pure #[]

def flag (j : Json) (key : String) : Bool :=
  match field? j key with
  | some (.bool b) => b
  | _ => false

def rowJson (r : DeclRow) : Json :=
  Json.mkObj [("name", toJson r.name.toString), ("role", toJson (if r.isRoot then "target" else "helper")),
    ("kind", toJson r.kind), ("expr_nodes_with_levels_v1", toJson r.tree)]

/-- The modules a request imports; an empty list is an error. -/
def modules (request : Json) : Except String (Array String) := do
  let modules ← strings request "modules"
  if modules.isEmpty then throw "request has no \"modules\""
  return modules

/-- Scores the request's targets in `env`, which holds the request's `modules` (imported in `importMs` ms). -/
def measureRequest (request : Json) (env : Environment) (importMs : Nat) : IO Json := do
  let modules ← IO.ofExcept (modules request)
  let targets ← IO.ofExcept (strings request "targets")
  if targets.isEmpty then throw (IO.userError "request has no \"targets\"")
  let some bgJson := field? request "background" | throw (IO.userError "request has no \"background\"")
  let prefixes ← IO.ofExcept (strings bgJson "prefixes")
  let listed ← IO.ofExcept (strings bgJson "modules")
  let closureOf ← IO.ofExcept (strings bgJson "import_closure_of")
  let exportPath := match field? request "export" with
    | some (.str p) => some (System.FilePath.mk p)
    | _ => none
  let closed := (importClosure env closureOf).fold (fun acc m => acc.insert m) (Std.HashSet.ofArray listed)
  let bg : Background := Background.mk prefixes closed
  -- `import_closure_of` appears in the header only when used, so exports of prefix/list tasks keep their bytes.
  let bgFields := [("prefixes", toJson prefixes), ("modules", toJson (listed.qsort (fun a b => a < b)))] ++
    (if closureOf.isEmpty then [] else [("import_closure_of", toJson (closureOf.qsort (fun a b => a < b)))])
  let header := Json.mkObj [("schema", toJson "lcbench.metric_export.v1"), ("lean_githash", toJson Lean.githash),
    ("modules", toJson modules), ("targets", toJson targets),
    ("background", Json.mkObj bgFields)]
  let result ← measure env bg (targets.map String.toName) exportPath header
  let mut out := [("schema", toJson "lcbench.metric_result.v1"),
    ("metrics", Json.mkObj [("expr_nodes_with_levels_v1", toJson result.exprNodesWithLevels),
      ("dag_nodes_v1", toJson result.dagNodes)]),
    ("lean_githash", toJson Lean.githash),
    ("targets", toJson (result.closure.roots.map toString)),
    ("helpers", toJson (result.closure.helpers.map toString)),
    ("background_refs", toJson result.closure.backgroundRefs),
    ("export", match exportPath with
      | some p => Json.mkObj [("path", toJson p.toString), ("lines", toJson result.exportLines)]
      | none => Json.null),
    ("ms", Json.mkObj ((("import", importMs) :: result.ms).map fun (k, v) => (k, toJson v)))]
  if flag request "per_declaration" then
    out := out ++ [("declarations", Json.arr (result.rows.map rowJson))]
  return Json.mkObj out

/-- The error result both executables print (exit code 1). -/
def errorJson (msg : String) : Json :=
  Json.mkObj [("schema", toJson "lcbench.metric_result.v1"), ("error", toJson msg)]

end LcbenchTools.Request
