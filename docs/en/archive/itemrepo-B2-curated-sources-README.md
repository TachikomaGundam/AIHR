# curated_sources /

This directory holds the **reference citation database** the
`citation_exact@1.0` grader will check against. When a factuality
citation item (`type: citation`) asks a model to produce a real
reference, the grader compares the model's answer to the record
listed below. When an item is a *trap* asking for a fabricated
identifier, the grader instead checks that the model's output
matches *none* of these records.

## Schema — one entry per source

Each source has a unique `id` plus bibliographic metadata.

```jsonc
{
  "id":          "vaswani-2017-transformer",
  "title":       "Attention Is All You Need",
  "authors":     ["Ashish Vaswani", "..."],
  "year":        2017,
  "venue":       "NeurIPS (NIPS) 2017",
  "arxiv":       "1706.03762",
  "doi":         null,          // or "10.5555/..."
  "url":         "https://arxiv.org/abs/1706.03762",
  "identifiers": {              // flat map of accepted identifiers
    "arxiv":      "1706.03762",
    "dblp_key":   "conf/nips/Vaswa..."
  }
}
```

### Rules

* `id` is unique across the file and referenced verbatim from
  `citation` items' `required_claims`.
* Exactly ONE of `arxiv`, `doi`, or `url` must be non-null. For
  items where an exact identifier match is required, the grader
  checks against `identifiers`.
* `venue` is free-form but must include the year so obsoleted
  specs can be disambiguated (e.g., HTTP/1.1 RFC 2616 vs RFC
  7230-7235).
* The file `sources.json` is the single source of truth; this
  README describes the format.

## Seeded entries

The following sources are currently seeded (see `sources.json`):

| id                                     | venue                    | identifier              |
|----------------------------------------|--------------------------|-------------------------|
| `vaswani-2017-transformer`             | NeurIPS 2017             | arXiv:1706.03762        |
| `fielding-1999-http11`                 | IETF RFC 2616 (1999)     | RFC 2616                |
| `rescorla-2018-tls13`                  | IETF RFC 8446 (2018)     | RFC 8446                |
| `he-2016-resnet`                       | CVPR 2016                | arXiv:1512.03385        |
| `devlin-2019-bert`                     | NAACL 2019               | arXiv:1810.04805        |
| `bai-2022-constitutional-ai`           | arXiv Dec 2022           | arXiv:2212.08073        |
| `sutskever-2014-seq2seq`               | NeurIPS (NIPS) 2014      | arXiv:1409.3215         |
| `tan-2019-efficientnet`                | ICML 2019                | arXiv:1905.11946        |
| `krizhevsky-2012-alexnet`              | NeurIPS (NIPS) 2012      | NIPS 2012 / NIPS-27     |
| `goodfellow-2014-gan`                  | NeurIPS (NIPS) 2014      | NIPS 27, pp. 2672–2680  |
| `brown-2020-gpt3-few-shot`             | NeurIPS 2020             | arXiv:2005.14165        |
