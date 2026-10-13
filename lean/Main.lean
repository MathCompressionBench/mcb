-- ABOUTME: `lcbench-metric REQUEST.json`: compiled organizer exe that imports the task modules (initializers are never
-- ABOUTME: executed) and prints A and dag_nodes_v1 of the targets as one JSON object.
import LcbenchTools.Request

open Lean LcbenchTools.Request

def usage : String :=
  "usage: lcbench-metric REQUEST.json   (REQUEST: {\"modules\", \"targets\", \"background\": {\"prefixes\", " ++
  "\"modules\", \"import_closure_of\"}, optional \"export\", \"per_declaration\"}; " ++
  "see docs/METRIC.md)"

def run (requestPath : String) : IO Json := do
  let text ← if requestPath == "-" then (← IO.getStdin).readToEnd else IO.FS.readFile requestPath
  let request ← IO.ofExcept (Json.parse text)
  let modules ← IO.ofExcept (modules request)
  initSearchPath (← findSysroot)
  let t0 ← IO.monoMsNow
  -- A compiled executable never enables initializer execution, so imported `initialize` code does not run.
  let env ← importModules (modules.map fun m => {module := m.toName}) {} (loadExts := false) (level := .private)
  let t1 ← IO.monoMsNow
  measureRequest request env (t1 - t0)

def main (args : List String) : IO UInt32 := do
  match args with
  | [path] =>
    try
      IO.println (← run path).compress
      return 0
    catch e =>
      IO.println (errorJson e.toString).compress
      return 1
  | _ =>
    IO.eprintln usage
    return 2
