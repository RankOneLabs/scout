# Relevance holdout migration 48 verification

Migration 48 was verified on Willie on 2026-09-27 against an online SQLite
backup of the production Scout database. The live database was read only by
SQLite's `.backup` operation. The migration ran against
`/tmp/scout-production-copy-migration48-20260927.db` using
`scout.storage.migrations._migrate_to_48` from commit `e28d906`.

The verification output was:

```text
database_copy=/tmp/scout-production-copy-migration48-20260927.db
before_user_version=47
after_user_version=48
relevance_holdouts_columns=19
foreign_key_check=[]
integrity_check=[('ok',)]
```
