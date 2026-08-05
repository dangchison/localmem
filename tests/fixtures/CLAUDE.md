# Project conventions

This project uses a pnpm workspace and a single Postgres instance.
Every service reads its config from the environment.

## Build commands

- use pnpm, not npm
  - `pnpm install` at the repo root
  - `pnpm -r build` builds every package
- run migrations before the test suite
- never commit a lockfile conflict

## Shell recipe

```bash
# this comment is not a heading
- this dash is not a bullet
pnpm -r test --filter ./services/api
```

## Ghi chú tiếng Việt

Dùng pnpm thay vì npm khi cài phụ thuộc.

---
