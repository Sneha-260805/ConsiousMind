# ProcessBench inspection (Stage B)

Source: Qwen/ProcessBench (Hugging Face dataset) @ `3bdcd5371ed567559a78f559c01c13a6deee7604` (arXiv:2412.06559). Files sha256-verified; git blob OIDs match the Hub tree listing.

Schema (every record validated): `id`: str, `generator`: str, `problem`: str, `steps`: list, `final_answer_correct`: bool, `label`: int

- label = 0-indexed earliest erroneous step; -1 = no error
- NO reference-answer field in any split
- steps are pre-segmented strings; shown to the solver as 'Step i:' with i = index + 1

Total records: **3400**

| split | records | distinct problems | error annotated | fac=False & error | label 0 | ref found | ref numeric |
|---|---|---|---|---|---|---|---|
| gsm8k | 400 | 375 | 207 | 200 | 37 | 400 | 400 |
| math | 1000 | 955 | 594 | 500 | 115 | 1000 | 661 |
| olympiadbench | 1000 | 561 | 661 | 500 | 48 | - | - |
| omnimath | 1000 | 951 | 759 | 500 | 72 | - | - |

Label distributions (0-indexed) and other details: `processbench_inspection.json`.
