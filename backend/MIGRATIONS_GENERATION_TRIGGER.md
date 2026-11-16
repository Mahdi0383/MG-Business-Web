This commit intentionally triggers the repository's controlled migration-scaffolding workflow.
The workflow generates a PostgreSQL EF Core migration and model snapshot as an artifact for review.

Retry after adding an explicit backend restore step and pinning .NET SDK 10.
