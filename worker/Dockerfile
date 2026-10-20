# syntax=docker/dockerfile:1.7
# cFlow worker image (Temporal). Includes the .NET SDK for the C# code node.
#
# The build context is the REPOSITORY ROOT (the worker installs `core/` from a sibling path):
#
#   docker build -f worker/Dockerfile -t ghcr.io/fernandomarcius/cflow-worker .
#
# .NET 10: the 8 runtime has a bug in Parquet.Net (BitPackedEncoder.Decode8ValuesLE "Destination is
# too short") when reading RLE_DICTIONARY written by DuckDB with more than ~50k rows; runtime 10
# reads the same Parquet.Net 4.25 files correctly.
FROM mcr.microsoft.com/dotnet/sdk:10.0 AS dotnet

FROM python:3.12-slim

# libicu: .NET with non-invariant globalization (culture-aware formatting) requires ICU.
COPY --from=dotnet /usr/share/dotnet /usr/share/dotnet
RUN ln -s /usr/share/dotnet/dotnet /usr/bin/dotnet \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      "$(apt-cache search --names-only '^libicu[0-9]+$' | sort -rV | head -n1 | cut -d' ' -f1)" \
 && rm -rf /var/lib/apt/lists/*
ENV DOTNET_ROOT=/usr/share/dotnet \
    NUGET_PACKAGES=/opt/nuget \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_NOLOGO=1

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY core ./core
COPY worker/pyproject.toml worker/uv.lock ./worker/
COPY worker/src ./worker/src
WORKDIR /app/worker

# Bytecode is compiled at build time: the root filesystem may be read-only at runtime.
ENV UV_COMPILE_BYTECODE=1
RUN uv sync --frozen --no-dev \
 && /app/worker/.venv/bin/python -m compileall -q /app/worker/src

# Runtime never resolves dependencies.
ENV UV_NO_SYNC=1

# Restore and warm the C# runner template: NuGet packages (Parquet.Net) are downloaded ONLY here, at
# image build time. At runtime the worker is air-gapped and compiles each node with `--no-restore`.
RUN RUNNER="$(uv run --no-sync python -c 'import pathlib, cflow_core.execucao as e; print(pathlib.Path(e.__file__).parent / "csharp_runner" / "Runner.csproj")')" \
 && dotnet restore "$RUNNER" \
 && dotnet build "$RUNNER" -c Release --no-restore -o /tmp/warm \
 && rm -rf /tmp/warm

# Health probe (see src/cflow_worker/saude.py).
EXPOSE 8080

# The venv interpreter directly: Python is PID 1, receives SIGTERM and DRAINS (see main.py).
CMD ["/app/worker/.venv/bin/python", "-m", "cflow_worker.main"]
