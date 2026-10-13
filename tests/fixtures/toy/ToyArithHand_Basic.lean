-- ABOUTME: Hand-written compressed version of the toy MyNat library, used only as a website illustration.
-- ABOUTME: Same definitions and statements as ToyArith.Basic; proofs share helper lemmas.

namespace ToyArith

inductive MyNat where
  | zero : MyNat
  | succ : MyNat → MyNat

namespace MyNat

def add : MyNat → MyNat → MyNat
  | a, zero => a
  | a, succ b => succ (add a b)

def mul : MyNat → MyNat → MyNat
  | _, zero => zero
  | a, succ b => add (mul a b) a

instance : Add MyNat := ⟨add⟩
instance : Mul MyNat := ⟨mul⟩

theorem add_zero_eq (a : MyNat) : a + zero = a := rfl

theorem add_succ_eq (a b : MyNat) : a + succ b = succ (a + b) := rfl

theorem zero_add_eq (a : MyNat) : zero + a = a := by
  induction a with
  | zero => rfl
  | succ n ih => exact congrArg succ ih

theorem succ_add_eq (a b : MyNat) : succ a + b = succ (a + b) := by
  induction b with
  | zero => rfl
  | succ n ih => exact congrArg succ ih

/-- The headline target: commutativity of addition. -/
theorem add_comm_eq (a b : MyNat) : a + b = b + a := by
  induction b with
  | zero => exact (zero_add_eq a).symm
  | succ n ih => exact (congrArg succ ih).trans (succ_add_eq n a).symm

theorem add_assoc_eq (a b c : MyNat) : a + b + c = a + (b + c) := by
  induction c with
  | zero => rfl
  | succ n ih => exact congrArg succ ih

theorem add_right_comm_eq (a b c : MyNat) : a + b + c = a + c + b := by
  rw [add_assoc_eq, add_comm_eq b c, ← add_assoc_eq]

theorem mul_zero_eq (a : MyNat) : a * zero = zero := rfl

theorem zero_mul_eq (a : MyNat) : zero * a = zero := by
  induction a with
  | zero => rfl
  | succ n ih => exact ih

theorem mul_one_eq (a : MyNat) : a * succ zero = a := zero_add_eq a

theorem left_distrib_eq (a b c : MyNat) : a * (b + c) = a * b + a * c := by
  induction c with
  | zero => rfl
  | succ n ih =>
    show a * (b + n) + a = a * b + (a * n + a)
    rw [ih, add_assoc_eq]

/-- New helper introduced by the compression; not a target. -/
theorem succ_mul_eq (a b : MyNat) : succ a * b = a * b + b := by
  induction b with
  | zero => rfl
  | succ n ih =>
    show succ a * n + succ a = a * n + a + succ n
    rw [ih]
    exact congrArg succ (add_right_comm_eq _ _ _)

theorem mul_comm_eq (a b : MyNat) : a * b = b * a := by
  induction b with
  | zero => exact (zero_mul_eq a).symm
  | succ n ih =>
    show a * n + a = succ n * a
    rw [ih, succ_mul_eq]

end MyNat

end ToyArith
