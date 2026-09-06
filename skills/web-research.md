---
name: Web Research
tags: [research, web-search, docs, verification, sources]
dependencies: []
conflicts: []
version_hint: n/a
project_detect: []
summary: When your internal knowledge might be stale or wrong, RESEARCH — don't guess. Decompose the question, search authoritative sources, distinguish documentation from speculation, verify with primary sources, preserve citations, and be honest about uncertainty. Prefer official docs and version-current material over blog posts. When two sources conflict, name the conflict and follow the primary source.
---

# Web Research

The rule: **if you're not sure and a fresh answer matters, look it up**. Do not paper over
uncertainty with confident prose. Do not invent function signatures, API endpoints, or version
numbers. Every mistake here is a compounding cost — a wrong dependency name gets copy-pasted
into a Dockerfile that gets committed to a repo that ships to production.

Model knowledge has a training cut-off. Framework APIs change quarterly. Security advisories
land daily. When you're operating in territory where "current" matters, RESEARCH FIRST.

## When to research

Do research when ANY of these apply:

* The task mentions a version (Laravel 12, Next.js 15, Tailwind 4, Python 3.14) — versions
  matter and internal knowledge is likely 6-24 months stale.
* The task asks about "the current recommended way", "the modern approach", "the latest
  documentation".
* You're about to name a package, a function, an environment variable, a CLI flag — verify it
  exists in the target version.
* You're about to describe a limit, a quota, a pricing tier, a rate limit — these change.
* The task involves security (CVE, advisory, hardened config) — never guess.
* You're stuck: the obvious approach doesn't work and you're about to try harder. Research
  first; a well-crafted search often shows you WHY.

Do NOT research when:

* The task is straightforward implementation of a stable pattern (write a for loop, sort a list).
* The information is on the local machine (read the file rather than search for how similar
  files usually look).
* You've researched this exact thing in the current session (cite what you already found).

## Decomposing the question

A good query is specific. A bad query is a paragraph.

* Bad: "how do I make a Laravel API endpoint that authenticates using JWT and returns paginated
  results from PostgreSQL with proper error handling"
* Good (three separate searches): "Laravel 11 Sanctum API token", "Laravel Eloquent paginate
  json response", "Laravel 11 exception rendering json api"

Break a compound question into 3-5 sub-questions BEFORE searching. Each sub-question is a single
searchable fact.

## Source hierarchy — trust in this order

1. **Official documentation** for the specific version. `laravel.com/docs/11.x`, `nextjs.org/docs`,
   `docs.python.org/3.14`, `docs.docker.com`. These are the source of truth.
2. **Official GitHub repositories** — READMEs, CHANGELOGs, source code, official examples. When
   docs are incomplete, code is unambiguous.
3. **Author-maintained blog posts + conference talks** by framework maintainers. Second-tier
   authority.
4. **Well-known community sources** — StackOverflow answers with high vote counts + a recent
   edit date. StackOverflow answers with dates older than a major release are suspect.
5. **Generic tutorials, blog aggregators, "top 10" listicles** — bottom of the pile. Often
   wrong, often outdated, sometimes AI-generated slop citing hallucinated features.

If sources 1-3 disagree with 4-5, sources 1-3 win.

## Verifying technical claims

Never trust a single source on a specific claim. Cross-check:

* Function signature: docs + source code + at least one live example.
* Environment variable name: docs + the actual source file that reads it.
* CLI flag: `--help` output from the actual binary (or docs from the correct version).
* Package name on npm/pip/composer: check the registry page exists; check the download count is
  non-trivial; check the last publish date is recent.
* Configuration key: docs + a working example repo.

If ANY of these differ across sources, the claim is UNCERTAIN — say so, and prefer the source
closest to code (docs of the specific version > blog > tutorial).

## Detecting outdated information

Signals a source is outdated:

* Publish date is older than the current major release of the technology.
* The article mentions APIs marked deprecated in the current docs.
* Comments on the article report "this doesn't work any more".
* The article contradicts current official docs.

When in doubt: **check the current docs' migration guide** — every major framework publishes one.

## API + library research

Before you use a library, confirm:

1. Its current name and canonical import path (packages get renamed — `axios` was, `styled-components`
   was, `next-themes` package name is `next-themes` not `nextjs-themes`).
2. Its current major version and any breaking changes between what you might remember and now.
3. Its maintenance status (last commit > 12 months = investigate; > 24 months = probably dead).
4. Its dependencies (a "small" library that pulls in 40 MB of transitive deps is not small).
5. Its licence (permissive for libraries; a copyleft library in a proprietary product is a legal
   problem).

## Documentation vs speculation

Documentation says "the API accepts X and returns Y". Speculation says "you might be able to
pass X, in which case it might return Y". If you can't find the former, DO NOT WRITE the latter
as if it were the former. Say "not documented; will verify by testing".

## Preserving citations

For every non-trivial claim, keep the source. Format:

    - `paginate($n)` returns a LengthAwarePaginator (docs: laravel.com/docs/11.x/pagination).
    - Next.js 15 changed `params` to a Promise in async pages (blog.vercel.com/…, 2024-10-21).

Citations do TWO things:
1. Let the reader verify (or update) the claim.
2. Force you to be honest — if you can't cite it, you might not actually know it.

## When sources conflict

Common pattern: docs say A, popular tutorial says B, StackOverflow answer says C.

* Follow the docs.
* Note the conflict explicitly: "The docs say A. Older tutorials show B, which broke in version
  N; do not follow those."
* If the conflict is genuinely open (docs are ambiguous, both patterns work), pick one and note
  the reasoning.

Never present one arm of a live conflict as settled truth.

## Iterative research

First query rarely nails it. Iterate:

1. Broad query → skim results → identify canonical source.
2. Read the canonical source's index / TOC → identify the specific page.
3. Read the specific page → identify what's still ambiguous.
4. Narrow query on the ambiguity.
5. If you land on GitHub issues, sort by "recently commented" — active issues show what's
   currently broken; closed issues show what was fixed.

Stop when you can state the answer WITHOUT hedging. If you're still hedging after 4-5 searches,
name the uncertainty rather than pretending it's resolved.

## Verifying VERSIONS

Almost every technical answer is version-specific. Before answering ANY framework question:

1. Inspect the target project (`composer.json`, `package.json`, `pyproject.toml`) for the
   installed version.
2. If no target project, ask the user which version.
3. If truly unknown, cite the latest STABLE version at time of writing (not "latest" — a
   moving target).

Never answer "Laravel does X" without specifying which Laravel.

## Uncertainty vocabulary

Precise words for imprecise knowledge:

* **"I know"**: You've verified it against a primary source in the current session or you've
  read the file.
* **"According to X, …"**: You have a citation.
* **"I believe"**: You have model prior + moderate confidence but no primary source.
* **"Possibly"**: Guess grounded in adjacent knowledge.
* **"Unknown"**: You haven't looked; if it matters, look.

Never use "definitely" without a citation. Never use "obviously" — nothing is obvious in
technical writing.

## Security advisories

* CVE database (cve.mitre.org, nvd.nist.gov) — canonical.
* GHSA (GitHub Security Advisories) — cross-referenced with CVE, often faster.
* Vendor advisories (Laravel Security, Django Security, PSFPU for Python) — highest signal for
  their ecosystem.
* When a dependency has a known CVE, check the vendor's fix version and upgrade to at least
  that. Do not roll your own workaround.

## Practical protocol

1. Decompose the question into 3-5 sub-questions.
2. For each sub-question: run a targeted search.
3. Land on the canonical source (docs or official repo).
4. Extract the specific fact.
5. Cite it in the answer.
6. Cross-check any claim that surprises you against a second source.
7. If two sources conflict, name the conflict and follow the primary source.
8. If nothing surprises you and every sub-question is answered, you're done.
9. If a sub-question resists 4-5 well-crafted queries, name the uncertainty rather than filling
   with plausible-sounding prose.

The goal is not to search a lot. The goal is to be CORRECT and HONEST.
