## What this changes

<!-- One or two sentences, and link the issue if there is one: Fixes #123 -->

## How I tested it

<!-- For example: "python3 tools/umbrel_sim.py canopy-miniflux passes" or "installed from my fork on an Umbrel Home". -->

## Checklist

- [ ] `pytest -q tests` passes
- [ ] For a new or changed app: `python3 tools/umbrel_sim.py <app-id>` passes
- [ ] Images are pinned to a release tag and digest, and run on amd64 and arm64
- [ ] Anything a user reads is in plain English (no jargon, says what to do next)
- [ ] No real passwords, keys or personal files anywhere in the change
- [ ] For YAMS releases: bumped `version` in `canopy-yams/umbrel-app.yml` **and** `YAMS_VERSION` in `canopy-yams/docker-compose.yml`, and added a line to `CHANGELOG.md`
