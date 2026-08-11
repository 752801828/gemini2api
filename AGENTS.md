# Repository maintenance requirements

## Required project documentation

Every code, configuration, API, UI, data-schema, container, or deployment change must update `docs/PROJECT_OVERVIEW_AND_CHANGELOG.md` in the same commit.

The update must include:

- the affected feature or architecture section when behavior changes;
- a dated changelog entry describing user-visible behavior and compatibility impact;
- relevant verification or migration/deployment notes;
- no secrets, cookies, API keys, proxy passwords, webhook URLs, or signing secrets.

Do not mark a change complete or publish it without this documentation update.
