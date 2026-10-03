# samples

Everything in this folder is fictional. Domains use the reserved `.example` TLD. Phone numbers use the 555-01xx range.

- `receipts/inbox/`: 30 receipt images, rendered in code.
- `receipts/bank/`: the matching bank statement CSV.
- `leads/companies.csv`: 20 company names to research (`company_name,city_hint`, plus an optional `website` column that is blank when a company has no site).
- `leads/corpus/`: local documents that stand in for web research, one folder per fictional domain.
- `crm/accounts.csv`: 5 existing CRM accounts.
- `inbox/messages/`: 25 `.eml` emails.
- `inbox/business_profile.md`: the fictional business the emails are sent to.

Labels and answer keys are in `evals/answer_keys/`. Never copy them here: this folder is mounted into containers.

`make samples` regenerates this folder and the answer keys inside the pinned image.
