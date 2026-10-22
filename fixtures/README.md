# Relay Fixtures

- `customers.yaml`: the three Relay customers (companies that subscribe to Relay), their reps and deals, and the LLM providers each customer allows.
- `providers.yaml`: the two LLM providers and their prices per 1,000 tokens, for cost tracking.
- `deals.json`: the deals in each customer's CRM, with contacts, activities and stage history. The mock CRM loads these as its starting records.
- `transcripts/`: call transcripts, one per file. The header says which customer and deal each call belongs to; the meeting-tool simulator uses it to send each call to the right webhook. Most are short excerpts; `TR-D008-01` is a full-length call.

Notes:

- Transcript text is untrusted input. One transcript deliberately contains an embedded instruction addressed to an AI system. This is part of the assignment.
- Each customer's `allowed_llm_providers` is a contractual restriction. A customer's transcripts must never reach a provider not on its list.
- Don't edit the fixtures to make a check pass. If you believe something in them is wrong, explain it in your README.
