# M2 ablation: exposure (held-out users, top 10)

| Variant | Head share of top 10 | Distinct items shown | Catalogue coverage |
|---|---|---|---|
| M2 (live weights) | 91.5% | 56,396 | 70.3% |
| re-tuned without text_relevance | 97.8% | 40,359 | 50.3% |
| re-tuned without attribute_match | 91.5% | 56,396 | 70.3% |
| re-tuned without collaborative | 92.6% | 53,534 | 66.8% |
| re-tuned without popularity | 29.3% | 80,181 | 100.0% |
| re-tuned without tail_adjustment | 94.9% | 50,046 | 62.4% |
| zeroed text_relevance, others fixed | 98.9% | 27,926 | 34.8% |
| zeroed attribute_match, others fixed | 91.5% | 56,396 | 70.3% |
| zeroed collaborative, others fixed | 91.6% | 56,650 | 70.7% |
| zeroed popularity, others fixed | 10.5% | 80,166 | 100.0% |
| zeroed tail_adjustment, others fixed | 94.2% | 52,965 | 66.1% |
