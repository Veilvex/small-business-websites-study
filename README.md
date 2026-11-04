# Small business websites on the first page of Google (2026)

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.23809690.svg)](https://doi.org/10.5281/zenodo.23809690)

Open data from a study by [Veilvex](https://veilvex.com): in October 2026 we checked **178 small business websites** that rank on the first page of Google in **24 cities** and **15 niches** (dentists, plumbers, salons, lawyers, accountants, vets and more).

Full write-up: **[We checked 178 small business websites from the first page of Google. Here is what they miss](https://veilvex.com/blog/small-business-websites-study-2026/)**

## Key findings

Of the 157 websites we could open:

| Check | Share of sites |
|---|---|
| HTTPS | 99% |
| Mobile layout | 98% |
| Tap-to-call phone link | 85% |
| Contact or booking form | 65% |
| **No prices** on the home page and no link to a price page | **48%** |
| **No WhatsApp or Telegram link** | **52%** |
| **Block AI crawlers** (GPTBot, ClaudeBot, Google-Extended…) in robots.txt | **8%** |

- The technical basics are almost solved on page one. What is missing is simpler: prices, a fast way to write, and access for AI assistants.
- Dental clinics lead on messengers (22 of 23 link to WhatsApp or Telegram); none of the law firms and vet clinics in the sample do.
- 6 of 10 accounting firms block AI crawlers, so ChatGPT or Gemini have less to work with when someone asks them to recommend an accountant.

## Files

| File | What it is |
|---|---|
| [`data/small-business-websites-2026.csv`](data/small-business-websites-2026.csv) | One row per website (178), anonymized: city, niche and the result of every check |
| [`data/summary-by-niche.csv`](data/summary-by-niche.csv) | Shares per niche |
| [`data/summary-by-city.csv`](data/summary-by-city.csv) | Shares per city |
| [`scripts/check.py`](scripts/check.py) | The checker we ran (Python, `requests`) |

### Columns

`opened` is `no` when the site did not answer our automated check; the other fields are then empty. Yes/no fields: `https`, `mobile_viewport` (a viewport meta tag), `title_tag`, `meta_description`, `tap_to_call_link` (a `tel:` link), `whatsapp_link`, `telegram_link`, `contact_form`, `prices_on_home_page` (a currency amount on the home page), `price_page_link` (a link to a prices/pricing page), `structured_data_jsonld`, `localbusiness_schema`, `robots_blocks_ai_crawlers`. `ai_crawlers_blocked` lists the user agents disallowed from `/`. `load_seconds` and `home_page_kb` are the response time and the size of the home page HTML.

## Method and limits

We searched Google for typical local queries (for example a dental clinic in Dubai or a plumber in London) and took the businesses' own websites, skipping directories, media and review sites. Each home page was fetched once with a mobile browser user agent; robots.txt was read for AI crawler rules.

This is a snapshot of first-page results, not a random sample of all businesses: sites that already rank well are usually in better shape than average. The data is anonymized: names and addresses of the businesses are not published.

## Related research

- **[Who does AI recommend? We checked 60 local businesses](https://veilvex.com/blog/ai-recommendations-local-business-study-2026/)** (November 2026). We asked an AI assistant with web search for the best business in 6 niches and 5 cities and compared the answers with 60 Google Maps leaders. It named 9 of 60 (15%); a top-10 Google position for "niche + city" mattered more than the Maps position (28% vs 10%). Summary data: [CSV](https://veilvex.com/data/ai-recommendations-local-business-2026.csv), CC BY 4.0, also in this repo: [`ai-recommendations-2026/`](ai-recommendations-2026/).

## License and citation

Data: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Please link to the [study page](https://veilvex.com/blog/small-business-websites-study-2026/) when you reuse it. Code: MIT.

> Veilvex (2026). *Small business websites on the first page of Google, 2026* [Data set]. Zenodo. https://doi.org/10.5281/zenodo.23809690

---

[Veilvex](https://veilvex.com) builds websites, does SEO and GEO (visibility in ChatGPT and AI search), ads and AI bots for businesses worldwide.
