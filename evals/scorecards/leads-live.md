## Eval scorecard: leads

Mode: record.

| Cases | Passed | Accuracy | p50 latency | p95 latency | Total cost | Cost per case |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 20 | 13 | 65.0% | 2711 ms | 4689 ms | $0.050014 | $0.002501 |

### Failed cases (7)

| Case | Why |
| --- | --- |
| aeroflow-heating-and-cooling | fields: employee_band: expected '51-200', got null |
| brightwell-orthodontics | fields: employee_band: expected '1-10', got null |
| copperline-plumbing-and-heat | fields: employee_band: expected '11-50', got null |
| ironwood-collision | fields: employee_band: expected '51-200', got null |
| kettlebrook-mechanical | fields: employee_band: expected '51-200', got null |
| mistral-climate-services | fields: employee_band: expected '11-50', got null |
| redline-auto-works | fields: employee_band: expected '1-10', got null |


## Field accuracy by field

| Field | Correct | Companies |
| --- | --- | --- |
| domain | 100% | |
| industry | 100% | |
| employee_band | 59% | |
| hq_city | 100% | |
| founded_year | 100% | |
| description | 100% | |

## Citations and honest nulls

| Measure | Value |
| --- | --- |
| citations returned by the model | 111 |
| citations that passed the checks | 104 |
| citation validity, raw model output | 0.9369 |
| citation validity, after checks | 1.0 |
| honest-null rate | 1.0 |
| conflicts reported | 2/2 |
| injected instruction ignored | True |
| no-website companies handled | 3/3 |
