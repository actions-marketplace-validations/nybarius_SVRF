import BraidedTrain.Braid

/-!
# BraidedTrain.Union — the line-level union merge as a model step

`unionLines theirs ours` keeps the base side's lines in order and appends each line of the
pull request's side that is not already there, once: the union of two line lists, as a
model. `git.RealGit.union_step` applies a union only on configured union paths, and only
when every path git's merge reports as conflicted is a union path; any other conflicted
path keeps the two heads apart. The code's union is git's own (`merge=union`), declared
for those paths through `svrf.rules.union_attributes`; see the modelling boundary below.
Finite lists, no Mathlib.

1. `count_unionLines`: the count of a line after a union is its count on the base side,
   plus one exactly when the base side lacks it and the pull request's side has it. Every
   other statement here follows from this formula.
2. `unionLines_append`, `unionLines_assoc`: grouping does not matter, exactly (as lists).
   Folding the additions of a family one at a time is folding their union.
3. `unionLines_comm`: two unions onto the same base in either order give the same lines
   with the same counts (`SameCounts`, i.e. `List.Perm`). They are not equal as lists in general
   (`union_order_visible`): the order of appended lines is the fold order. A git tree is
   byte identity, so fold order still decides which tree is gated; the train fixes the fold
   order once and replays it at landing, and the landed-tree check compares bytes.
4. The path level. A change is a list of edits, each a whole-content replacement of a path
   or a union of lines onto a path. `change_comm`: two changes applied in either order give
   trees that agree path by path up to line order (`SameLines`), under exactly the
   hypothesis the union repair enforces (`UnionOnlyOverlap`): every path both changes edit
   is edited by union on both sides. `family_perm`: the same holds for any reordering of a
   family whose changes are pairwise so compatible, so fold order within a family changes
   at most the order of union-merged lines.

Modelling boundary: git merges a non-union file edited on both sides hunk by hunk; this
model treats such a path as a conflict (the coarser, path-level reading used by
`apply_comm_of_disjoint`), and it treats every edit of a union path as a union of lines.
git's union merge is also hunk by hunk: inside each conflicting hunk it keeps the pull
request's lines and then the base side's, outside them it is git's ordinary merge, and it
neither drops repeated lines nor restores lines one side deleted. `unionLines` is the
line-set reading of that merge, not its exact byte order; the train never relies on that
order, because it fixes the fold order once, replays it at landing, and compares the
landed tree with the gated tree byte for byte.
-/

set_option linter.unusedSimpArgs false

namespace BraidedTrain

section Lines
variable {Line : Type} [DecidableEq Line]

/-- Append a line unless it is already present. -/
def addLine (acc : List Line) (l : Line) : List Line :=
  if l ∈ acc then acc else acc ++ [l]

/-- The line-set union: the base side's lines, then each new line of ours, once. -/
def unionLines (theirs ours : List Line) : List Line :=
  ours.foldl addLine theirs

theorem mem_addLine {acc : List Line} {l x : Line} : x ∈ addLine acc l ↔ x ∈ acc ∨ x = l := by
  unfold addLine
  by_cases h : l ∈ acc
  · simp only [h, if_true]
    constructor
    · exact Or.inl
    · intro hx; rcases hx with hx | rfl
      · exact hx
      · exact h
  · simp [h]

theorem mem_unionLines : ∀ {t : List Line} {ours : List Line} {x : Line},
    x ∈ unionLines t ours ↔ x ∈ t ∨ x ∈ ours
  | t, [], x => by simp [unionLines]
  | t, l :: ls, x => by
    show x ∈ unionLines (addLine t l) ls ↔ _
    rw [mem_unionLines, mem_addLine]
    simp only [List.mem_cons]
    constructor
    · rintro ((h | h) | h)
      · exact Or.inl h
      · exact Or.inr (Or.inl h)
      · exact Or.inr (Or.inr h)
    · rintro (h | h | h)
      · exact Or.inl (Or.inl h)
      · exact Or.inl (Or.inr h)
      · exact Or.inr h

theorem count_addLine (acc : List Line) (l x : Line) :
    (addLine acc l).count x = acc.count x + (if x ∉ acc ∧ x = l then 1 else 0) := by
  unfold addLine
  by_cases h : l ∈ acc
  · have : ¬ (x ∉ acc ∧ x = l) := fun ⟨hx, e⟩ => hx (e ▸ h)
    simp [h, this]
  · simp only [h, if_false, List.count_append, List.count_singleton]
    by_cases e : x = l
    · subst e; simp [h]
    · have : ¬ (l == x) = true := by simpa using Ne.symm e
      simp [e, this]

/-- The count of a line after a union: base count, plus one when only ours has it. -/
theorem count_unionLines : ∀ (t ours : List Line) (x : Line),
    (unionLines t ours).count x = t.count x + (if x ∉ t ∧ x ∈ ours then 1 else 0)
  | t, [], x => by simp [unionLines]
  | t, l :: ls, x => by
    show (unionLines (addLine t l) ls).count x = _
    rw [count_unionLines, count_addLine]
    by_cases ht : x ∈ t
    · have h1 : x ∈ addLine t l := mem_addLine.mpr (Or.inl ht)
      simp [ht, h1]
    · by_cases e : x = l
      · subst e
        have h1 : x ∈ addLine t x := mem_addLine.mpr (Or.inr rfl)
        simp [ht, h1]
      · have h1 : x ∉ addLine t l := fun hm => (mem_addLine.mp hm).elim ht e
        simp [ht, h1, e]

/-- Grouping does not matter: folding two additions is folding their concatenation. -/
theorem unionLines_append (t a b : List Line) :
    unionLines t (a ++ b) = unionLines (unionLines t a) b := by
  simp [unionLines, List.foldl_append]

/-- Union is associative, exactly as lists. -/
theorem unionLines_assoc (t : List Line) : ∀ (a b : List Line),
    unionLines (unionLines t a) b = unionLines t (unionLines a b)
  | a, [] => rfl
  | a, x :: xs => by
    show unionLines (addLine (unionLines t a) x) xs = unionLines t (unionLines (addLine a x) xs)
    rw [← unionLines_assoc t (addLine a x) xs]
    congr 1
    by_cases hx : x ∈ a
    · have hx' : x ∈ unionLines t a := mem_unionLines.mpr (Or.inr hx)
      simp only [addLine, hx, hx', if_true]
    · simp only [addLine, hx, if_false]
      rw [unionLines_append]
      rfl

/-- Two lists of lines hold the same lines with the same counts: the same multiset of
lines, which is `List.Perm` by `List.perm_iff_count` (stated with counts here so the proof
needs no choice principle). -/
def SameCounts (l l' : List Line) : Prop :=
  ∀ x, l.count x = l'.count x

@[simp] theorem sameCounts_refl (l : List Line) : SameCounts l l := fun _ => rfl

theorem sameCounts_trans {l₁ l₂ l₃ : List Line} (h₁ : SameCounts l₁ l₂) (h₂ : SameCounts l₂ l₃) :
    SameCounts l₁ l₃ := fun x => (h₁ x).trans (h₂ x)

theorem sameCounts_mem_iff {l l' : List Line} (h : SameCounts l l') (x : Line) : x ∈ l ↔ x ∈ l' := by
  rw [← List.count_pos_iff, ← List.count_pos_iff, h x]

/-- Two unions onto one base give the same lines with the same counts, in either order. -/
theorem unionLines_comm (t a b : List Line) :
    SameCounts (unionLines (unionLines t a) b) (unionLines (unionLines t b) a) := by
  intro x
  rw [count_unionLines, count_unionLines, count_unionLines, count_unionLines]
  by_cases ht : x ∈ t
  · have h1 : x ∈ unionLines t a := mem_unionLines.mpr (Or.inl ht)
    have h2 : x ∈ unionLines t b := mem_unionLines.mpr (Or.inl ht)
    simp [ht, h1, h2]
  · by_cases ha : x ∈ a
    · have h1 : x ∈ unionLines t a := mem_unionLines.mpr (Or.inr ha)
      by_cases hb : x ∈ b
      · have h2 : x ∈ unionLines t b := mem_unionLines.mpr (Or.inr hb)
        simp [ht, ha, hb, h1, h2]
      · have h2 : x ∉ unionLines t b := fun hm => (mem_unionLines.mp hm).elim ht hb
        simp [ht, ha, hb, h1, h2]
    · have h1 : x ∉ unionLines t a := fun hm => (mem_unionLines.mp hm).elim ht ha
      by_cases hb : x ∈ b
      · have h2 : x ∈ unionLines t b := mem_unionLines.mpr (Or.inr hb)
        simp [ht, ha, hb, h1, h2]
      · have h2 : x ∉ unionLines t b := fun hm => (mem_unionLines.mp hm).elim ht hb
        simp [ht, ha, hb, h1, h2]

/-- A union depends on the base side only up to line order and counts. -/
theorem unionLines_congr {t t' : List Line} (h : SameCounts t t') (ours : List Line) :
    SameCounts (unionLines t ours) (unionLines t' ours) := by
  intro x
  rw [count_unionLines, count_unionLines, h x]
  have hm : x ∈ t ↔ x ∈ t' := sameCounts_mem_iff h x
  by_cases hx : x ∈ t
  · simp [hx, hm.mp hx]
  · have hx' : x ∉ t' := fun h' => hx (hm.mpr h')
    simp [hx, hx']

/-- The order of appended lines is the fold order: unions are not equal as lists. -/
theorem union_order_visible : unionLines (unionLines ([] : List Nat) [1]) [2] ≠
    unionLines (unionLines ([] : List Nat) [2]) [1] := by
  decide

end Lines

/-! ## The path level -/

section Paths
variable {Path Line : Type} [DecidableEq Path] [DecidableEq Line]

/-- One edit of a pull request: replace a path's content, or union lines onto it. -/
inductive Edit (Path Line : Type)
  | replace (p : Path) (content : List Line)
  | union (p : Path) (ours : List Line)

/-- The path an edit touches. -/
def Edit.path {Path Line : Type} : Edit Path Line → Path
  | .replace p _ => p
  | .union p _ => p

/-- Whether an edit is a union of lines. -/
def Edit.isUnion {Path Line : Type} : Edit Path Line → Bool
  | .replace _ _ => false
  | .union _ _ => true

/-- Apply one edit to a tree of files (each a list of lines). -/
def runEdit (t : Path → List Line) : Edit Path Line → Path → List Line
  | .replace p c => fun q => if q = p then c else t q
  | .union p ours => fun q => if q = p then unionLines (t p) ours else t q

/-- Apply a change (a pull request's edits, in order). -/
def change (t : Path → List Line) (es : List (Edit Path Line)) : Path → List Line :=
  es.foldl runEdit t

/-- Two trees hold the same lines with the same counts at every path. -/
def SameLines (t t' : Path → List Line) : Prop :=
  ∀ p, SameCounts (t p) (t' p)

/-- The union repair's hypothesis: every path both changes edit is a union edit on both sides. -/
def UnionOnlyOverlap (a b : List (Edit Path Line)) : Prop :=
  ∀ e ∈ a, ∀ f ∈ b, e.path = f.path → e.isUnion = true ∧ f.isUnion = true

omit [DecidableEq Path] in
theorem sameLines_refl (t : Path → List Line) : SameLines t t := fun p => sameCounts_refl (t p)

omit [DecidableEq Path] in
theorem sameLines_trans {t₁ t₂ t₃ : Path → List Line} (h₁ : SameLines t₁ t₂)
    (h₂ : SameLines t₂ t₃) : SameLines t₁ t₃ := fun p => sameCounts_trans (h₁ p) (h₂ p)

omit [DecidableEq Path] [DecidableEq Line] in
theorem unionOnlyOverlap_symm {a b : List (Edit Path Line)} (h : UnionOnlyOverlap a b) :
    UnionOnlyOverlap b a :=
  fun f hf e he hp => (h e he f hf hp.symm).symm

theorem runEdit_congr {t t' : Path → List Line} (h : SameLines t t') (e : Edit Path Line) :
    SameLines (runEdit t e) (runEdit t' e) := by
  intro q
  cases e with
  | replace p c =>
    simp only [runEdit]
    by_cases hq : q = p
    · simp only [hq, if_true]; exact sameCounts_refl _
    · simp only [hq, if_false]; exact h q
  | union p ours =>
    simp only [runEdit]
    by_cases hq : q = p
    · simp only [hq, if_true]; exact unionLines_congr (h p) ours
    · simp only [hq, if_false]; exact h q

theorem change_congr : ∀ {t t' : Path → List Line}, SameLines t t' → ∀ (es : List (Edit Path Line)),
    SameLines (change t es) (change t' es)
  | _, _, h, [] => h
  | _, _, h, e :: es => change_congr (runEdit_congr h e) es

/-- Two edits commute up to line order when they touch different paths or both are unions. -/
theorem runEdit_comm (t : Path → List Line) (e f : Edit Path Line)
    (hef : e.path = f.path → e.isUnion = true ∧ f.isUnion = true) :
    SameLines (runEdit (runEdit t e) f) (runEdit (runEdit t f) e) := by
  intro q
  cases e with
  | replace p c =>
    cases f with
    | replace p' c' =>
      have hne : p ≠ p' := fun hp => by simpa [Edit.isUnion] using (hef hp).1
      simp only [runEdit]
      by_cases h1 : q = p'
      · have h2 : q ≠ p := fun h => hne (h ▸ h1)
        simp [h1, h2, Ne.symm hne]
      · by_cases h2 : q = p
        · simp [h1, h2, hne, Ne.symm hne]
        · simp [h1, h2, hne, Ne.symm hne]
    | union p' ours =>
      have hne : p ≠ p' := fun hp => by simpa [Edit.isUnion] using (hef hp).1
      simp only [runEdit]
      by_cases h1 : q = p'
      · have h2 : q ≠ p := fun h => hne (h ▸ h1)
        simp [h1, h2, Ne.symm hne]
      · by_cases h2 : q = p
        · simp [h1, h2, hne, Ne.symm hne]
        · simp [h1, h2, hne, Ne.symm hne]
  | union p ours =>
    cases f with
    | replace p' c' =>
      have hne : p ≠ p' := fun hp => by simpa [Edit.isUnion] using (hef hp).2
      simp only [runEdit]
      by_cases h1 : q = p'
      · have h2 : q ≠ p := fun h => hne (h ▸ h1)
        simp [h1, h2, hne, Ne.symm hne]
      · by_cases h2 : q = p
        · simp [h1, h2, hne, Ne.symm hne]
        · simp [h1, h2, hne, Ne.symm hne]
    | union p' ours' =>
      simp only [runEdit]
      by_cases hpp : p = p'
      · subst hpp
        by_cases h1 : q = p
        · simp only [h1, if_true]; exact unionLines_comm (t p) ours ours'
        · simp [h1]
      · by_cases h1 : q = p'
        · have h2 : q ≠ p := fun h => hpp (h ▸ h1)
          simp [h1, h2, Ne.symm hpp]
        · by_cases h2 : q = p
          · simp [h1, h2, hpp]
          · simp [h1, h2]

theorem change_edit_comm (e : Edit Path Line) : ∀ (t : Path → List Line) (b : List (Edit Path Line)),
    (∀ f ∈ b, e.path = f.path → e.isUnion = true ∧ f.isUnion = true) →
      SameLines (change (runEdit t e) b) (runEdit (change t b) e)
  | t, [], _ => sameLines_refl _
  | t, f :: fs, h => by
    show SameLines (change (runEdit (runEdit t e) f) fs) (runEdit (change (runEdit t f) fs) e)
    exact sameLines_trans
      (change_congr (runEdit_comm t e f (h f List.mem_cons_self)) fs)
      (change_edit_comm e (runEdit t f) fs (fun g hg => h g (List.mem_cons_of_mem _ hg)))

/-- Two changes whose only shared paths are union edits on both sides commute, up to the
order of union-merged lines. -/
theorem change_comm : ∀ (t : Path → List Line) (a b : List (Edit Path Line)),
    UnionOnlyOverlap a b → SameLines (change (change t a) b) (change (change t b) a)
  | t, [], b, _ => sameLines_refl _
  | t, e :: es, b, h => by
    show SameLines (change (change (runEdit t e) es) b) (change (runEdit (change t b) e) es)
    exact sameLines_trans
      (change_comm (runEdit t e) es b (fun e' he' f hf => h e' (List.mem_cons_of_mem _ he') f hf))
      (change_congr (change_edit_comm e t b (fun f hf => h e List.mem_cons_self f hf)) es)

/-- Applying a family is folding its changes. -/
def applyFamily (t : Path → List Line) (fam : List (List (Edit Path Line))) : Path → List Line :=
  fam.foldl change t

theorem applyFamily_append (t : Path → List Line) (f g : List (List (Edit Path Line))) :
    applyFamily t (f ++ g) = applyFamily (applyFamily t f) g := by
  simp [applyFamily, List.foldl_append]

theorem applyFamily_congr : ∀ {t t' : Path → List Line}, SameLines t t' →
    ∀ (fam : List (List (Edit Path Line))), SameLines (applyFamily t fam) (applyFamily t' fam)
  | _, _, h, [] => h
  | _, _, h, c :: cs => applyFamily_congr (change_congr h c) cs

/-- Fold order within a family whose changes pairwise overlap only on union edits changes
at most the order of union-merged lines. -/
theorem family_perm {fam fam' : List (List (Edit Path Line))} (hp : fam.Perm fam')
    (hc : fam.Pairwise UnionOnlyOverlap) (t : Path → List Line) :
    SameLines (applyFamily t fam) (applyFamily t fam') := by
  induction hp generalizing t with
  | nil => exact sameLines_refl _
  | cons x _ ih =>
    exact ih (List.pairwise_cons.mp hc).2 (change t x)
  | swap x y l =>
    have hyx : UnionOnlyOverlap y x := (List.pairwise_cons.mp hc).1 x List.mem_cons_self
    show SameLines (applyFamily (change (change t y) x) l) (applyFamily (change (change t x) y) l)
    exact applyFamily_congr (change_comm t y x hyx) l
  | trans h₁ _ ih₁ ih₂ =>
    have hc' : _ := (h₁.pairwise_iff (fun {a b} h => unionOnlyOverlap_symm h)).mp hc
    exact sameLines_trans (ih₁ hc t) (ih₂ hc' t)

end Paths

end BraidedTrain
