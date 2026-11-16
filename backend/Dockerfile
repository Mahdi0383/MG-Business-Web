# Build from the repository root:
# docker build -f backend/Dockerfile -t mg-business-api .
FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
WORKDIR /src

COPY backend/src/MG.Business.Domain/MG.Business.Domain.csproj backend/src/MG.Business.Domain/
COPY backend/src/MG.Business.Infrastructure/MG.Business.Infrastructure.csproj backend/src/MG.Business.Infrastructure/
COPY backend/src/MG.Business.Api/MG.Business.Api.csproj backend/src/MG.Business.Api/
RUN dotnet restore backend/src/MG.Business.Api/MG.Business.Api.csproj

COPY backend/src/ backend/src/
RUN dotnet publish backend/src/MG.Business.Api/MG.Business.Api.csproj \
    --configuration Release --no-restore --output /app/publish

FROM mcr.microsoft.com/dotnet/aspnet:10.0 AS runtime
WORKDIR /app
ENV ASPNETCORE_URLS=http://+:8080
ENV ASPNETCORE_HTTP_PORTS=8080
EXPOSE 8080
COPY --from=build /app/publish .
USER $APP_UID
ENTRYPOINT ["dotnet", "MG.Business.Api.dll"]
