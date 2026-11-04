# Who does AI recommend? 60 local businesses vs an AI assistant (Veilvex, November 2026)

Aggregate data from a small study by [Veilvex](https://veilvex.com). Full write-up with method, tables and FAQ: **[veilvex.com/blog/ai-recommendations-local-business-study-2026](https://veilvex.com/blog/ai-recommendations-local-business-study-2026/)**.

## What we did

- 5 cities (Toronto, London, Sydney, Chicago, Dublin) × 6 niches (dentist, beauty salon, auto repair, lawyer, restaurant, gym).
- In each city and niche: the business in 1st place on Google Maps (once the 2nd) and the one in 6th place, 60 businesses in total.
- On 3 November 2026 we asked Claude with web search 30 plain-English questions, one per niche and city: *"What is the best &lt;niche&gt; in &lt;city&gt;? Recommend a few."*
- Answers were matched to our list by website address and name.
- For 23 of the businesses we also had an answer from Gemini reading Google results (2 November 2026).

One answer per question, one day, small groups: read it as a snapshot, not a ranking.

## Key results

| Group | Businesses | Named by AI |
|---|---|---|
| All map leaders | 60 | 9 (15%) |
| 1st (once 2nd) on Google Maps | 30 | 5 (17%) |
| 6th on Google Maps | 30 | 4 (13%) |
| In Google's top 10 for "&lt;niche&gt; &lt;city&gt;" | 18 | 5 (28%) |
| Not in that top 10 | 42 | 4 (10%) |
| Gyms | 10 | 6 (60%) |
| Lawyers / restaurants | 10 / 10 | 0 / 0 |

Two assistants, two lists: of the 23 businesses checked with both, Gemini named 6, Claude named 3, and none was named by both.

## Files

- [`ai-recommendations-local-business-2026.csv`](ai-recommendations-local-business-2026.csv): every group in the study (`group_type`, `group`, `businesses`, `named_by_ai`, `share_named_pct`). The same file is published at https://veilvex.com/data/ai-recommendations-local-business-2026.csv.

## License

[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Please link to the [study page](https://veilvex.com/blog/ai-recommendations-local-business-study-2026/) when you reuse it.

> Veilvex (2026). *Who does AI recommend? AI assistant answers vs 60 Google Maps leaders in 5 cities* [Data set]. https://veilvex.com/blog/ai-recommendations-local-business-study-2026/
