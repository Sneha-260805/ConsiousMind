# Primary pilot results — supervisor view

These files contain only the five main conditions over the same 40 pilot items:

- L0
- V
- L_false_near (the original `L_false` primary condition)
- L
- A

That is **40 x 5 = 200 primary requests**.

## Results

| Condition | Correct | N | Repair rate |
|---|---:|---:|---:|
| L0 | 20 | 40 | 50.0% |
| V | 32 | 40 | 80.0% |
| L_false_near | 22 | 40 | 55.0% |
| L | 33 | 40 | 82.5% |
| A | 37 | 40 | 92.5% |

## Simple contrasts

- V - L0 = +30.0 percentage points
- L - V = +2.5 points
- L_false_near - V = -25.0 points
- L - L_false_near = +27.5 points

## Files

- `raw_outputs_PRIMARY_200.jsonl`: exact Qwen response text plus request metadata and exact prompt.
- `parsed_outputs_PRIMARY_200.jsonl`: parser output for each request.
- `scored_outputs_PRIMARY_200.jsonl`: correctness flags after comparison to the GSM8K gold answer.
- `paired_primary.csv`: one row per item with the five primary binary outcomes side by side.
- `PRIMARY_RESULTS_SUMMARY.csv`: compact condition-level summary.
- `original_full_run_summary.json`: unchanged summary from the original 225-request run. The additional 25 requests were a separate far-false sensitivity subset and are not part of the main 40 x 5 table above.
