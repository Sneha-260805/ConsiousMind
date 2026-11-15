# Worked example: `m3p_01`

## Original GSM8K question

Cheyenne is a craftswoman making pots for sale. On a particular week, she made 80 clay pots. However, when she put them in the sun to dry, 2/5 of the pots cracked and could not be sold. How much money did Cheyenne make from selling the remaining items at $40 per clay pot?

## Original / gold reasoning

```text
Step 1: 2/5 * 80 = 32 pots cracked
Step 2: 80 - 32 = 48 sellable pots
Step 3: 40 * 48 = 1920 dollars
```

Gold final answer: **1920**

## Controlled M3 corruption

```text
Step 1: 2/5 * 80 = 32
Step 2: 80 - 32 = 47        <-- injected M3 calculation slip
Step 3: 40 * 47 = 1880      <-- recomputed downstream consequence
```

Corrupted final answer: **1880**

True first error: **Step 2**

Why Step 3 is not a second independent error: `40 * 47 = 1880` is mathematically correct given the corrupted Step-2 value. The pipeline therefore preserves a single causal first error.

## Five primary prompt treatments for the same corrupted trace

### L0 — no feedback

No feedback paragraph is inserted.

### V — verification only

```text
The final answer in the proposed solution is wrong.
```

### L — true location

```text
The final answer in the proposed solution is wrong.
The first mistake is in Step 2.
```

### L_false — false location

```text
The final answer in the proposed solution is wrong.
The first mistake is in Step 1.
```

Step 1 is a verified correct step, so this is a deliberately false diagnosis.

### A — answer supplied

```text
The correct final answer is 1920.
```

The full frozen record, including dependency metadata and automatic-validation fields, is in `m3_pilot_40.json`.
