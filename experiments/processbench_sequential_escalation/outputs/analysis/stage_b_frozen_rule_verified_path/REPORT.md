# Stage B results - REAL MODEL RESULTS

> ORACLE STATUS: Stage B uses two oracle components - (1) the gold answer decides whether Stage 1 failed and therefore whether escalation is triggered; (2) L_true/N_true use the expert-annotated first-error step. This is NOT a deployable system: it tests whether diagnostic escalation has potential value IF failure detection and localisation were available.

> LIMITATION (possible training contamination): items come from the GSM8K-test and MATH-test source problems, which Qwen3-4B may have seen in training. Absolute accuracy/repair rates are NOT uncontaminated capability estimates; the quantities of interest are paired within-item differences between feedback conditions.

- Pool: 265 items; Stage-1 V repair rate 0.7132; Stage-1 failures 76 (gate >= 50: passed); excluded by blinded annotation review 15; primary Stage-2 set 61 (L_false on 61)
- Rescue rate by Stage-2 condition: VV 0.0656, N_true 0.1148, L_true 0.1639, L_false 0.1311
- Final repair rate by policy (analysis population): V (one-shot) 0.756, V -> V 0.772, V -> N_true 0.784, V -> L_true 0.796, V -> L_false 0.788
- PRIMARY V->L_true vs V->V (n=61): both 3, L_true only 7, V retry only 1, neither 50; Delta_rescue 0.0984 (95% CI 0.0164 to 0.1803), exact McNemar p 0.0703; Delta_final 0.024
- Diagnostic-content contrast V->L_true vs V->L_false (n=61): Delta 0.0328 (95% CI -0.0492 to 0.1148), McNemar p 0.6875
- L_false validity basis: Pass-1-implied false-step validity (A6; not independently Pass-2-validated) (61)
- Pass-2 waiver (A6): A separate Pass-2 human recheck was waived for feasibility (amendment A6) because it would duplicate the same reviewer's earlier Pass-1 judgment: a Pass-1 CONFIRM of Step k as the earliest substantive error implies that every preceding step, including the false-location candidate f = k-1, contains no earlier substantive error.
- Interpretation label (frozen precision rule): INCONCLUSIVE

