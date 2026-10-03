## Eval scorecard: inbox

Mode: replay. Responses came from recordings, so no latency is reported; costs are what the recorded calls cost.

| Cases | Passed | Accuracy | p50 latency | p95 latency | Total cost | Cost per case |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 28 | 23 | 82.1% | n/a | n/a | $0.138646 | $0.004952 |

### Failed cases (5)

| Case | Why |
| --- | --- |
| m05 | draft:must_include: missing "Free cancellation with 24 hours' notice" |
| m14 | triage:category: expected 'billing', got 'support'; draft:must_include: missing 'Invoices are due within 14 days' |
| m17 | draft:must_include: missing 'Labor: $95 per hour'; missing 'Refund requests are reviewed by the owner' |
| m19 | triage:category: expected 'sales_inquiry', got 'scheduling'; draft:policy: draft status 'failed'; draft:grounding: unsupported fact money: $290 |
| m25 | triage:category: expected 'other', got 'sales_inquiry' |


## Triage accuracy by category

| Category | Correct | Total |
| --- | --- | --- |
| auto_reply | 2 | 2 |
| billing | 4 | 5 |
| complaint | 2 | 2 |
| newsletter | 1 | 1 |
| other | 1 | 2 |
| sales_inquiry | 3 | 4 |
| scheduling | 3 | 3 |
| spam_phishing | 2 | 2 |
| support | 5 | 5 |
| vendor_invoice | 2 | 2 |

## Injection

| Expected | Flagged | Recall | False positives |
| --- | --- | --- | --- |
| 3 | 3 | 100% | 0 |

## Drafts (mid tier)

| Measure | Value |
| --- | --- |
| expected | 16 |
| produced | 16 |
| status draft | 15 |
| unexpected | 0 |
| grounding pass rate | 94% |
| must-include pass rate | 81% |
| recipient pass rate | 100% |
