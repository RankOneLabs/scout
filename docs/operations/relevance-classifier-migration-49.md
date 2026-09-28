# Relevance classifier migration 49 verification

Migration 49 was verified on Willie on 2026-09-27 against an online SQLite
backup of the production Scout database. The live database was read only by
SQLite's `.backup` operation. The backup was copied locally and the migration
ran against `/tmp/scout-production-copy-migration49-20260927.db` using
`StateManager` from commit `6cb84fd`. Production was at schema 47, so this
verification applied migrations 48 and 49 in order.

Before changing `classifier_of`, complete relevance phase runs were grouped by
their stored model and checked against the former `_EXPLICIT_LLM_ROUTE` regex
and built-in model resolver. The copy contained one distinct value,
`openrouter/google/gemini-2.5-flash`, across 5,509 rows. Zero distinct values
and zero rows fell outside the former resolver. Making `classifier_of` total
therefore does not change corpus admission for the production data captured by
this backup; explicit `zeroshot:` identities remain classified as zero-shot and
every other model value is classified as LLM.

The pre-migration promotion targets grouped by status were:

```text
promotion_status_counts=[('completed', 2)]
```

The verification output was:

```text
database_copy=/tmp/scout-production-copy-migration49-20260927.db
before_user_version=47
after_user_version=49
foreign_key_check=[]
integrity_check=[('ok',)]
```

The post-migration classifier/action distribution was:

```text
classifier_action_distribution=[('human', 'respond', 2), ('llm', None, 49550)]
```

Both completed human-positive promotion targets were backfilled as
`human`/`respond`. There were no pre-existing relevance holdout rows because
the production copy began at schema 47.
