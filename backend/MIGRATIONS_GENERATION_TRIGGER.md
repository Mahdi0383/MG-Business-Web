This commit intentionally triggers the repository's controlled migration-scaffolding workflow.
The workflow generates a PostgreSQL EF Core migration and model snapshot as an artifact for review.

Retry after adding an explicit backend restore step and pinning .NET SDK 10.

The startup project now references EF Core Design as required by dotnet-ef.

Generate a refreshed PostgreSQL migration snapshot including customers, POS sales, café kitchen tickets, game-net devices/sessions, branch administration, and current authorization models.

Commit the refreshed generated PostgreSQL migration sources and model snapshot into this feature branch.
