-- ABOUTME: Toy Peano arithmetic library used as a proof-compression descent example.
-- ABOUTME: Proofs are deliberately verbose and share no helper lemmas, so an agent can compress them.

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

theorem add_zero_eq (a : MyNat) : a + zero = a := by
  show add a zero = a
  rfl

theorem add_succ_eq (a b : MyNat) : a + succ b = succ (a + b) := by
  show add a (succ b) = succ (add a b)
  rfl

theorem zero_add_eq (a : MyNat) : zero + a = a := by
  induction a with
  | zero =>
    show add zero zero = zero
    rfl
  | succ n ih =>
    show add zero (succ n) = succ n
    have h1 : add zero (succ n) = succ (add zero n) := rfl
    have h2 : add zero n = n := ih
    rw [h1, h2]

theorem succ_add_eq (a b : MyNat) : succ a + b = succ (a + b) := by
  induction b with
  | zero =>
    show add (succ a) zero = succ (add a zero)
    have h1 : add (succ a) zero = succ a := rfl
    have h2 : add a zero = a := rfl
    rw [h1, h2]
  | succ n ih =>
    show add (succ a) (succ n) = succ (add a (succ n))
    have h1 : add (succ a) (succ n) = succ (add (succ a) n) := rfl
    have h2 : add a (succ n) = succ (add a n) := rfl
    have h3 : add (succ a) n = succ (add a n) := ih
    rw [h1, h2, h3]

/-- The headline target: commutativity of addition, proved without reusing the lemmas above. -/
theorem add_comm_eq (a b : MyNat) : a + b = b + a := by
  induction b with
  | zero =>
    show add a zero = add zero a
    have h1 : add a zero = a := rfl
    have h2 : ∀ w : MyNat, add zero w = w := by
      intro w
      induction w with
      | zero => rfl
      | succ n ih =>
        have k1 : add zero (succ n) = succ (add zero n) := rfl
        rw [k1, ih]
    rw [h1, h2 a]
  | succ n ih =>
    show add a (succ n) = add (succ n) a
    have h1 : add a (succ n) = succ (add a n) := rfl
    have h2 : ∀ w : MyNat, add (succ n) w = succ (add n w) := by
      intro w
      induction w with
      | zero => rfl
      | succ m ihm =>
        have k1 : add (succ n) (succ m) = succ (add (succ n) m) := rfl
        have k2 : add n (succ m) = succ (add n m) := rfl
        rw [k1, k2, ihm]
    have h3 : add a n = add n a := ih
    rw [h1, h2 a, h3]

theorem add_assoc_eq (a b c : MyNat) : a + b + c = a + (b + c) := by
  induction c with
  | zero =>
    show add (add a b) zero = add a (add b zero)
    have h1 : add (add a b) zero = add a b := rfl
    have h2 : add b zero = b := rfl
    rw [h1, h2]
  | succ n ih =>
    show add (add a b) (succ n) = add a (add b (succ n))
    have h1 : add (add a b) (succ n) = succ (add (add a b) n) := rfl
    have h2 : add b (succ n) = succ (add b n) := rfl
    have h3 : add a (succ (add b n)) = succ (add a (add b n)) := rfl
    have h4 : add (add a b) n = add a (add b n) := ih
    rw [h1, h2, h3, h4]

theorem add_right_comm_eq (a b c : MyNat) : a + b + c = a + c + b := by
  show add (add a b) c = add (add a c) b
  have assoc : ∀ x y z : MyNat, add (add x y) z = add x (add y z) := by
    intro x y z
    induction z with
    | zero => rfl
    | succ n ih =>
      have k1 : add (add x y) (succ n) = succ (add (add x y) n) := rfl
      have k2 : add y (succ n) = succ (add y n) := rfl
      have k3 : add x (succ (add y n)) = succ (add x (add y n)) := rfl
      rw [k1, k2, k3, ih]
  have comm : ∀ x y : MyNat, add x y = add y x := by
    intro x y
    induction y with
    | zero =>
      have z1 : ∀ w : MyNat, add zero w = w := by
        intro w
        induction w with
        | zero => rfl
        | succ m ihm =>
          have q : add zero (succ m) = succ (add zero m) := rfl
          rw [q, ihm]
      rw [z1 x]
      rfl
    | succ n ih =>
      have s1 : ∀ u v : MyNat, add (succ u) v = succ (add u v) := by
        intro u v
        induction v with
        | zero => rfl
        | succ m ihm =>
          have q1 : add (succ u) (succ m) = succ (add (succ u) m) := rfl
          have q2 : add u (succ m) = succ (add u m) := rfl
          rw [q1, q2, ihm]
      have e : add x (succ n) = succ (add x n) := rfl
      rw [e, s1 n x, ih]
  rw [assoc a b c, assoc a c b, comm b c]

theorem mul_zero_eq (a : MyNat) : a * zero = zero := by
  show mul a zero = zero
  rfl

theorem zero_mul_eq (a : MyNat) : zero * a = zero := by
  induction a with
  | zero =>
    show mul zero zero = zero
    rfl
  | succ n ih =>
    show mul zero (succ n) = zero
    have h1 : mul zero (succ n) = add (mul zero n) zero := rfl
    have h2 : mul zero n = zero := ih
    have h3 : add zero zero = zero := rfl
    rw [h1, h2, h3]

theorem mul_one_eq (a : MyNat) : a * succ zero = a := by
  show mul a (succ zero) = a
  have h1 : mul a (succ zero) = add (mul a zero) a := rfl
  have h2 : mul a zero = zero := rfl
  have h3 : ∀ w : MyNat, add zero w = w := by
    intro w
    induction w with
    | zero => rfl
    | succ n ih =>
      have k : add zero (succ n) = succ (add zero n) := rfl
      rw [k, ih]
  rw [h1, h2, h3 a]

theorem left_distrib_eq (a b c : MyNat) : a * (b + c) = a * b + a * c := by
  show mul a (add b c) = add (mul a b) (mul a c)
  induction c with
  | zero =>
    have h1 : add b zero = b := rfl
    have h2 : mul a zero = zero := rfl
    have h3 : add (mul a b) zero = mul a b := rfl
    rw [h1, h2, h3]
  | succ n ih =>
    have h1 : add b (succ n) = succ (add b n) := rfl
    have h2 : mul a (succ (add b n)) = add (mul a (add b n)) a := rfl
    have h3 : mul a (succ n) = add (mul a n) a := rfl
    have assoc : ∀ x y z : MyNat, add (add x y) z = add x (add y z) := by
      intro x y z
      induction z with
      | zero => rfl
      | succ m ihm =>
        have k1 : add (add x y) (succ m) = succ (add (add x y) m) := rfl
        have k2 : add y (succ m) = succ (add y m) := rfl
        have k3 : add x (succ (add y m)) = succ (add x (add y m)) := rfl
        rw [k1, k2, k3, ihm]
    rw [h1, h2, h3, ih, assoc]

theorem mul_comm_eq (a b : MyNat) : a * b = b * a := by
  show mul a b = mul b a
  have zm : ∀ x : MyNat, mul zero x = zero := by
    intro x
    induction x with
    | zero => rfl
    | succ n ih =>
      have k : mul zero (succ n) = add (mul zero n) zero := rfl
      rw [k, ih]
      rfl
  have comm : ∀ x y : MyNat, add x y = add y x := by
    intro x y
    induction y with
    | zero =>
      have z1 : ∀ w : MyNat, add zero w = w := by
        intro w
        induction w with
        | zero => rfl
        | succ m ihm =>
          have q : add zero (succ m) = succ (add zero m) := rfl
          rw [q, ihm]
      rw [z1 x]
      rfl
    | succ n ih =>
      have s1 : ∀ u v : MyNat, add (succ u) v = succ (add u v) := by
        intro u v
        induction v with
        | zero => rfl
        | succ m ihm =>
          have q1 : add (succ u) (succ m) = succ (add (succ u) m) := rfl
          have q2 : add u (succ m) = succ (add u m) := rfl
          rw [q1, q2, ihm]
      have e : add x (succ n) = succ (add x n) := rfl
      rw [e, s1 n x, ih]
  have assoc : ∀ x y z : MyNat, add (add x y) z = add x (add y z) := by
    intro x y z
    induction z with
    | zero => rfl
    | succ m ihm =>
      have k1 : add (add x y) (succ m) = succ (add (add x y) m) := rfl
      have k2 : add y (succ m) = succ (add y m) := rfl
      have k3 : add x (succ (add y m)) = succ (add x (add y m)) := rfl
      rw [k1, k2, k3, ihm]
  have sm : ∀ x y : MyNat, mul (succ x) y = add (mul x y) y := by
    intro x y
    induction y with
    | zero => rfl
    | succ n ih =>
      have k1 : mul (succ x) (succ n) = add (mul (succ x) n) (succ x) := rfl
      have k2 : mul x (succ n) = add (mul x n) x := rfl
      have k3 : add (add (mul x n) x) (succ n) = succ (add (add (mul x n) x) n) := rfl
      have k4 : add (add (mul x n) n) (succ x) = succ (add (add (mul x n) n) x) := rfl
      rw [k1, k2, ih, k3, k4, assoc, assoc, comm x n]
  induction b with
  | zero =>
    rw [zm a]
    rfl
  | succ n ih =>
    have k : mul a (succ n) = add (mul a n) a := rfl
    rw [k, sm n a, ih]

end MyNat

end ToyArith
