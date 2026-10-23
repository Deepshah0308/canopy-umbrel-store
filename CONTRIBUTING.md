# Contributing to Canopy

Thanks for helping. Whether it's a new app, a bug report, a clearer sentence on the YAMS dashboard or a new feature, contributions are welcome. You don't need to be an expert: plenty of useful changes are a few lines.

## Ways to help

- **Report a problem.** [Open an issue](https://github.com/Deepshah0308/canopy-umbrel-store/issues/new/choose) with the app, its version and its logs (YAMS: the dashboard's **Logs** button; other apps: right-click the app in Umbrel → Troubleshoot).
- **Add an app.** See [Add an app](#add-an-app) below.
- **Suggest an improvement.** The best ideas start with "I keep having to…".
- **Improve the words.** The dashboard is written for people who've never run a media server. If something confused you, it will confuse others.
- **Fix or build something.** Look for issues labelled `good first issue`, or comment on one to say you're taking it.

## Add an app

Canopy carries apps that aren't in [Umbrel's official store](https://github.com/getumbrel/umbrel-apps), or that other community stores ship badly out of date. Check the official store first.

1. **Make a folder** named `canopy-<app>` (lowercase, dashes). Copy a similar app as a starting point: `canopy-linkding` is a single container with its own login, `canopy-miniflux` has a database, `canopy-jdownloader` uses the Downloads folder.
2. **`docker-compose.yml`.** Pin the image to a release tag **and** its digest: `image: publisher/app:1.2.3@sha256:…`. `python3 -c "import sys; sys.path.insert(0,'tools'); import registry; print(registry.digest('publisher/app:1.2.3'), registry.architectures('publisher/app:1.2.3'))"` prints both, and the architectures must include `linux/amd64` and `linux/arm64`. `app_proxy`'s `APP_HOST` is `canopy-<app>_<service>_1`. Keep data under `${APP_DATA_DIR}/data`. If the app has its own login, add `PROXY_AUTH_ADD: "false"` so people don't sign in twice; if it needs a password, use `${APP_PASSWORD}` and set `deterministicPassword: true` in the manifest so Umbrel shows it.
3. **`umbrel-app.yml`.** `id` matches the folder, `version` matches the image tag (without a leading `v`), and pick a `port` no other app uses (the checks will tell you if one clashes). Quote the `tagline`. Write the description for someone who's never heard of the app.
4. **Test it like umbrelOS installs it:**
   ```sh
   python3 tools/umbrel_sim.py canopy-<app>            # install, check, remove
   python3 tools/umbrel_sim.py canopy-<app> --keep     # leave it running to try it
   python3 tools/umbrel_sim.py canopy-<app> --down
   ```
   It needs Docker and runs as root (it sets folder ownership the way umbrelOS does).
5. **Run the checks:** `pytest -q tests` (offline rules) and `python3 tools/lint_store.py canopy-<app>` (architectures, ports, duplicates), then `python3 tools/build_site.py` to add it to the website.
6. **Open a pull request.** CI runs all of the above again, including the install test. Once merged, the update bot keeps the app current from then on.

## How the project fits together

```
umbrel-app-store.yml          store id "canopy"
canopy-<app>/                 one folder per app
canopy-yams/
  umbrel-app.yml              the App Store listing, version and home-screen widgets
  docker-compose.yml          every container, including the Extras
  hooks/yams/                 all of YAMS's own code (see "Rules" for why it lives here)
    setup/seed.py             first boot: keys, passwords, starter configs, folders
    dashboard/server.py       dashboard API, app wiring, quick fixes, widgets, Docker controls
    dashboard/extras.py       Extras: switches, their settings files, Seerr's setup
    dashboard/updater.py      "Update apps": registry checks, image pulls, container recreation
    dashboard/backups.py      backup, restore and the backup schedule
    dashboard/index.html      the dashboard page
    extras/gate.sh            keeps an Extra waiting until it's switched on
    vpn/vpn-wrapper.sh        starts gluetun once a VPN file exists
    qbittorrent/*.sh          the VPN kill switch for qBittorrent
    Caddyfile                 routes each app's web UI behind the Umbrel login
tools/                        install simulator, update bot, store checks, website builder
docs/                         the GitHub Pages site
tests/                        automated tests (run on every pull request)
```

## Run the tests

You need Python 3.12 or newer.

```sh
pip install pytest pyyaml
pytest -q tests
```

The tests use fake versions of Docker, Sonarr, Radarr and Prowlarr, so they run anywhere in about twenty seconds, with no Umbrel needed. They also check every app against the store rules and that the website lists every app. Every pull request runs them automatically, plus the install test for each app it changes.

## Try your change on your own Umbrel

1. **Fork** this repository on GitHub and push your change to your fork.
2. Raise the app's `version` in its `umbrel-app.yml` (for YAMS, for example `1.4.0` → `1.4.1-test`, and set `YAMS_VERSION` in `docker-compose.yml` to the same value).
3. In Umbrel, go to **App Store → ⋯ → Community App Stores**. Remove this store and add your fork's URL instead. Both use the store id `canopy`, so only one can be added at a time.
4. YAMS shows an **Update** button (or install it fresh). The dashboard header shows your test version once it's running.
5. When you're done, switch back to `https://github.com/Deepshah0308/canopy-umbrel-store`.

Umbrel re-copies `hooks/yams/` on every update, so code changes reach your Umbrel whenever you bump the version.

## Rules that keep YAMS safe and working

- **Keep code in `hooks/yams/`.** umbrelOS only re-copies certain files when an app updates, and the `hooks` folder is one of them. Code elsewhere would reach new installs but never existing ones. Never name a file after a hook (`pre-start`, `post-update`, …) because umbrelOS would run it.
- **Standard library only.** The dashboard runs on the stock `python:alpine` image, so there's nothing to `pip install`. This keeps installs fast and nothing to build.
- **qBittorrent only ever uses the VPN.** It shares gluetun's network, waits for the tunnel before starting, and stops if the tunnel goes. Any change near this needs a test showing downloads can't leave outside the VPN.
- **Only touch YAMS's own containers.** Anything using the Docker socket filters by the `com.docker.compose.project=canopy-yams` label.
- **Check what an app accepted.** When wiring apps together, start from the app's own schema (`…/schema` endpoints), then confirm the change worked before marking a step done. Otherwise failures go silent.
- **Write for newcomers.** Messages say what happened and what to do next, in plain words. "Can't reach qBittorrent right now. YAMS checks again every 10 minutes." beats "Connection refused (111)".
- **Add a test** for any behaviour you change. The fakes in `tests/fakes.py` make most things easy to test.

## Releasing a version

App updates are shipped by the daily update bot (see the README). Major versions arrive as pull requests: read the app's release notes for breaking changes, then merge.

For YAMS itself, maintainers release by merging to `main` with the version bumped in both `canopy-yams/umbrel-app.yml` and `YAMS_VERSION` in `canopy-yams/docker-compose.yml` (a test checks they match), plus a line in `CHANGELOG.md`. Umbrel then offers the update to everyone.

## Be kind

Treat everyone with respect. Assume good intent, explain rather than dismiss, and remember many people here are running a server for the first time. Harassment or personal attacks aren't welcome and will be removed. If something bothers you, open an issue or contact the maintainer.

## License

Contributions are shared under the same license as this repository (see `LICENSE`).
