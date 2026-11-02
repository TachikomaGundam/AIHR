# Security Policy

## Reporting a vulnerability

Report security issues privately via
[GitHub security advisories](https://github.com/TachikomaGundam/AIHR/security/advisories/new).
Do not open a public issue for a vulnerability.

## Scope

AIHR is a command-line tool that talks to model-provider APIs and a
PostgreSQL database. In scope: credential handling (API keys, DSNs),
SQL injection surfaces, path traversal in config/tape handling, and
anything that could let a model provider's response execute locally.

## Version policy

Only the latest released version receives security fixes.

## Data handling notes

- Tracked configuration files ship no credentials or live endpoints.
- The cassette/record-replay layer stores provider responses in
  content-addressed files — treat a tape directory as containing
  everything your prompts and the providers said.
