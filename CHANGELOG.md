# Changelog

## Canopy (October 2026)
The YAMS store became **Canopy**, a store for apps Umbrel doesn't have yet, kept on their latest versions.
- **10 new apps:** Netdata, JDownloader 2, openHAB, SiYuan (all badly out of date elsewhere), plus linkding, Miniflux, NZBGet, Feishin, ByteStash and Mylar3 (on no other Umbrel store).
- **Daily update bot:** finds new releases, checks they run on amd64 and arm64, installs each one the way umbrelOS does, and ships it, or opens a pull request for major versions and failures.
- **Store checks** on every pull request: store rules, pinned images, both CPU architectures, no port clashes with Umbrel's official store or popular community stores, no duplicates of official apps, and an install test for each changed app.
- New website with every app's current version.

## YAMS 1.4.0
- **Extras:** Seerr, Lidarr, Recyclarr, Unpackerr and Decluttarr, each switched on from the dashboard and connected automatically. Off, they use almost no memory.
- **Fixed:** Sonarr and Radarr refused to save qBittorrent while the VPN wasn't set up yet, so new installs could end up without a download client. It's now saved switched off and switched on as soon as qBittorrent runs.
- Restoring a backup from the old `yams-media` app re-points Bazarr, Prowlarr's FlareSolverr and every other connection at the new containers.
- First-run setup, the 10-minute checks and Quick fixes no longer race each other into duplicate entries.
- App id is now `canopy-yams` (store id `canopy`). Back up, uninstall the old YAMS, install from Canopy and restore.

## 1.3.0
- **Security hardening.** The media apps moved to a private network other Umbrel apps can't reach. The dashboard's controls are only served to the router (behind your Umbrel login), and every command must come from the dashboard page, blocking cross-site requests. Security headers on every page.
- **Change password** updates Jellyfin, qBittorrent, Sonarr and Radarr together. The password is hidden on the dashboard until you press **Show**.
- A secret scan in the test suite fails the build if a password, VPN key or API key is ever committed.
- **Update apps yourself.** The dashboard checks Docker Hub for newer versions and updates every app with one click, or automatically each week. It backs up your settings first and rolls back any app that won't start.
- **Quick fixes:** connect Prowlarr or qBittorrent to Sonarr and Radarr, fix library folders, rescan Jellyfin, search for missing episodes and movies, reconnect the VPN, or fix everything at once.
- Prowlarr is now linked to Radarr as well as Sonarr, using each app's own form, and the links are re-checked every 10 minutes.
- GitHub Pages install guide, contributing guide, automated tests and CI.

## 1.2.2
- Sonarr and Radarr reliably get qBittorrent as their download client, re-checked and repaired every 10 minutes.

## 1.2.1
- Fixed qBittorrent failing to start ("unable to exec /app/qbittorrent-nox").

## 1.2.0
- Backups, like `yams backup`: on demand or automatic, with one-click restore.

## 1.1.0
- Logs, restart and VPN check for every app; update check; download list; home-screen widgets.
- qBittorrent waits for the VPN tunnel itself rather than gluetun's health check.

## 1.0.1
- New app icon. qBittorrent is pulled from Docker Hub.

## 1.0.0
- First release.
