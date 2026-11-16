# MG Business API

ASP.NET Core 10 API foundation. This is intentionally a small, secure starting point rather than a fake production backend.

## Requirements
- .NET 10 SDK
- SQLite for local development (default), or PostgreSQL for hosted environments

## Run locally
```bash
dotnet run --project src/MG.Business.Api
```

The API exposes:
- `GET /health/live`: process liveness
- `GET /health/ready`: database connectivity readiness

Set `Database__Provider=PostgreSql` and `ConnectionStrings__Business` using environment variables for PostgreSQL. Never commit production credentials.

## Structure
- `MG.Business.Domain`: core business entities and invariants
- `MG.Business.Infrastructure`: EF Core persistence and provider configuration
- `MG.Business.Api`: HTTP composition root and health endpoints
- `MG.Business.Domain.Tests`: domain invariant tests

## Important security boundary
Tenant and branch entities are only foundational data models. No tenant-scoped business endpoints are exposed until authentication, authorization, tenant resolution, and isolation tests are implemented together. A caller-supplied tenant header is not an authorization mechanism.

## Database evolution
Use reviewed EF Core migrations for schema changes. Do not call `EnsureCreated` in production. Backups, migration rollback strategy, secrets management, rate limiting, audit logging, and observability are required before production deployment.
