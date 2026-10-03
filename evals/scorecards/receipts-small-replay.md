## Eval scorecard: receipts-extraction

Mode: replay. Responses came from recordings, so no latency is reported; costs are what the recorded calls cost.

| Cases | Passed | Accuracy | p50 latency | p95 latency | Total cost | Cost per case |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 30 | 30 | 100.0% | n/a | n/a | $0.082633 | $0.002754 |

No failed cases.


## Field accuracy (small tier)

| Scorer | Accuracy |
| --- | --- |
| field:vendor_name | 100% |
| field:receipt_date | 100% |
| field:subtotal_cents | 100% |
| field:tax_cents | 100% |
| field:tip_cents | 100% |
| field:total_cents | 100% |
| field:card_last4 | 100% |
| honest_nulls | 100% |

## Reconciliation on the answer key's true fields (the matcher alone)

| Flag | Expected | Predicted | Precision | Recall |
| --- | --- | --- | --- | --- |
| matched | 21 | 21 | 100% | 100% |
| amount_mismatch | 3 | 3 | 100% | 100% |
| date_drift | 2 | 2 | 100% | 100% |
| missing_in_bank | 3 | 3 | 100% | 100% |
| duplicate_receipt | 1 | 1 | 100% | 100% |
| duplicate_charge | 2 | 2 | 100% | 100% |
| unreceipted_charge | 3 | 3 | 100% | 100% |
| out_of_scope | 8 | 8 | 100% | 100% |

## Reconciliation on the extracted fields (end to end)

| Flag | Expected | Predicted | Precision | Recall |
| --- | --- | --- | --- | --- |
| matched | 21 | 21 | 100% | 100% |
| amount_mismatch | 3 | 3 | 100% | 100% |
| date_drift | 2 | 2 | 100% | 100% |
| missing_in_bank | 3 | 3 | 100% | 100% |
| duplicate_receipt | 1 | 1 | 100% | 100% |
| duplicate_charge | 2 | 2 | 100% | 100% |
| unreceipted_charge | 3 | 3 | 100% | 100% |
| out_of_scope | 8 | 8 | 100% | 100% |
