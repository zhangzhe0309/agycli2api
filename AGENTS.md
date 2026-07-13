# AGENTS

This project is AI-Agent friendly. This document contains guidelines and information for AI programming agents working on this codebase.

## Project Overview

`agycli2api` is a lightweight, single-user API proxy for Antigravity. It exposes a standard Gemini API (specifically `/v1beta/models/:model:generateContent`) but proxies requests to Google's Cloud Code infrastructure (`daily-cloudcode-pa.googleapis.com`). It handles token refresh natively using tokens from `antigravity-cli`, and simulates IDE telemetry/session behaviors to avoid bans.

## Codebase Structure

The source code is written in TypeScript and resides in the `src` directory:

- **`src/index.ts`**: The entry point. It configures the Express server, sets up the optional API key authentication middleware, and defines the main routing logic to the proxy handler.
- **`src/proxy.ts`**: The core proxying logic. It retrieves and maps the account's available models for the Gemini-compatible model listing endpoint, manages session correlation (via content hashing), constructs proper payloads with labels and identifiers matching the official Antigravity plugin, handles the upstream requests to Cloud Code endpoints, and passes back responses (including SSE streams).
- **`src/auth.ts`**: Handles authentication and token lifecycle. It reads the local OAuth token (`~/.gemini/antigravity-cli/antigravity-oauth-token`), checks for expiration, and automatically refreshes the token when necessary.
- **`src/config.ts`**: Contains static configuration, environment variable fallback definitions, endpoints, and the simulated Antigravity `User-Agent` generator.

## Development Guidelines

- **Formatting & Linting**: This project uses [Biome](https://biomejs.dev/) for formatting and linting. Run `npm run format` and `npm run lint` to format or check the code. Always execute `npm run pre-commit` before committing your changes.
- **Running locally**: Use `npm run dev` to start a development server with `nodemon` and `tsx` that auto-reloads on changes.
- **TypeScript**: The project uses TypeScript. Ensure proper types or interfaces are defined when adding new functionality.
- **Streaming Support**: `proxy.ts` handles Server-Sent Events (SSE). Be careful when modifying response handling in `handleGenerateContent` so that streaming functionality remains unbroken.

## Docker Support

The `docker` directory contains files for building and running the project in a containerized environment:

- **`docker/Dockerfile`**: A multi-stage build that compiles the TypeScript code and sets up a production Node environment. It also installs the official `antigravity-cli` inside the container to dynamically extract and export the latest `ANTIGRAVITY_VERSION`.
- **`docker/docker-compose.yml`**: Configures the service, binds port `3403`, and uses a named volume `gemini-data` mapped to `/root/.gemini` to persist the OAuth tokens and conversation states.
- **`docker/entrypoint.sh`**: A startup script that sources the `ANTIGRAVITY_VERSION` and extracts a valid `ANTIGRAVITY_SESSION_ID` directly from the mounted SQLite databases before executing the Node application.
