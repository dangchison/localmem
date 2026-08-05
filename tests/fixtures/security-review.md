# Security review checklist

Run this before merging anything that touches input handling or authentication.

## Input

- Every SQL statement is parameterized; no string interpolation of user values.
- No `shell=True`, and every subprocess call uses a fixed argument vector.
- Uploaded filenames are never used as paths without normalization.

## Secrets

- No credentials, tokens or keys in source, fixtures or logs.
- Error messages name the file, never its contents.

## Dependencies

- New runtime dependency? Justify it in the pull request.
