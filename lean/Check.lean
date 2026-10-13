-- ABOUTME: `lcbench-check`: compiled organizer exe for the verify stages after the build: `replay OWNER...` (kernel
-- ABOUTME: replay of every owner module; `replay-one` for one) and `facts REQUEST.json` (facts and metrics, one import).
import LcbenchTools.Replay
import LcbenchTools.Extract
import LcbenchTools.Request

open Lean LcbenchTools

def usage : String :=
  "usage: lcbench-check replay [--kernel-heartbeats N] OWNER_MODULE...   (N per declaration; 0 = no limit)\n" ++
  "       lcbench-check replay-one [--kernel-heartbeats N] OWNER_MODULE   (stock leanchecker's check of one " ++
  "module; the replay's per-module mode runs it in a child process for each owner)\n" ++
  "       lcbench-check facts REQUEST.json   (the lcbench-metric request plus \"extract\": " ++
  "{\"roots\", \"frozen\", \"out\"} and an optional \"result\" file; see docs/VERIFY.md)"

/-- Replays every owner module through the kernel and prints the report; exit 1 when the kernel rejects one. -/
unsafe def replayMain (owners : List String) (budget : Nat) : IO UInt32 := do
  initSearchPath (← findSysroot)
  let report ← Replay.replayOwners (owners.toArray.map String.toName) budget
  IO.println report.toJson.compress
  return if report.failure.isNone then 0 else 1

/-- Stock `leanchecker`'s check of one owner module, printed as one report row; exit 1 when the kernel rejects it. -/
unsafe def replayOneMain (owner : String) (budget : Nat) : IO UInt32 := do
  initSearchPath (← findSysroot)
  let row ← Replay.replayOneJson owner.toName budget
  IO.println row.compress
  match row.getObjValAs? Bool "ok" with
  | .ok true => return 0
  | _ => return 1

/-- The metric result as one JSON line: into the request's `"result"` file when it names one, else on stdout. The
verifier keeps only the end of a command's output, and the result of a task with many declarations is one line
longer than that, so the verifier always names a file. -/
def emitResult (request : Json) (result : Json) : IO Unit :=
  match Request.field? request "result" with
  | some (.str path) => IO.FS.writeFile path (result.compress ++ "\n")
  | _ => IO.println result.compress

/-- One import of the owner modules serves both the extractor (its JSON goes to `extract.out`, byte for byte what the
interpreted extractor wrote) and the metric counter (its result is the line `lcbench-metric` prints, written by
`emitResult`). -/
def factsMain (requestPath : String) : IO UInt32 := do
  let request ← IO.ofExcept (Json.parse (← IO.FS.readFile requestPath))
  let modules ← IO.ofExcept (Request.modules request)
  let some ex := Request.field? request "extract" | throw (IO.userError "request has no \"extract\"")
  let roots ← IO.ofExcept (Request.strings ex "roots")
  let frozen ← IO.ofExcept (Request.strings ex "frozen")
  let some (.str outPath) := Request.field? ex "out" | throw (IO.userError "request has no \"extract\".\"out\"")
  initSearchPath (← findSysroot)
  let t0 ← IO.monoMsNow
  -- A compiled executable never enables initializer execution, so imported `initialize` code does not run.
  let env ← importModules (modules.map fun m => {module := m.toName}) {} (loadExts := false) (level := .private)
  let t1 ← IO.monoMsNow
  let (facts, summary) ← Extract.extract env
    { owners := modules.toList.map String.toName, roots := roots.map String.toName,
      frozen := frozen.toList.map String.toName }
  IO.FS.writeFile outPath facts.compress
  IO.eprintln summary
  try
    emitResult request (← Request.measureRequest request env (t1 - t0))
    return 0
  catch e =>
    emitResult request (Request.errorJson e.toString)
    return 1

unsafe def main (args : List String) : IO UInt32 := do
  match args with
  | "replay" :: "--kernel-heartbeats" :: n :: owners@(_ :: _) =>
    match n.toNat? with
    | some budget => replayMain owners budget
    | none =>
      IO.eprintln usage
      return 2
  | "replay" :: owners@(_ :: _) => replayMain owners 0
  | ["replay-one", "--kernel-heartbeats", n, owner] =>
    match n.toNat? with
    | some budget => replayOneMain owner budget
    | none =>
      IO.eprintln usage
      return 2
  | ["replay-one", owner] => replayOneMain owner 0
  | ["facts", path] => factsMain path
  | _ =>
    IO.eprintln usage
    return 2
