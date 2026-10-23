# Security

## Reporting a problem

Please **don't open a public issue** for security problems. Use GitHub's private reporting instead: go to the repository's **Security** tab and choose **Report a vulnerability**. You'll get a reply within a few days, and a fix will be released as a new version of the affected app.

A problem in an app itself (say, a bug in Miniflux) belongs with that app's authors; Canopy ships their fix as soon as they release it.

## How every Canopy app is packaged

- **Exact, verified images.** Every image is pinned to a release tag and its digest, so what you install is byte-for-byte what was tested. Images come from each app's official publisher.
- **Updates are tested before they ship.** The daily update bot installs each new version the way umbrelOS does and only ships it if it starts and answers. Major versions wait for a person.
- **Least privilege.** No app gets the Docker socket, host folders or extra capabilities unless it needs them. Netdata, which needs to see your containers, gets a read-only proxy that can list them but can't start, stop, change or run anything in them.
- **Logins.** Apps with their own login skip Umbrel's login so you don't sign in twice; apps without one stay behind your Umbrel login. Generated passwords come from Umbrel and are shown on the app's page.

## How YAMS on Umbrel protects you

- **Downloads only use the VPN.** qBittorrent runs inside the VPN container's network, starts only when the VPN tunnel is up, and stops within seconds if it drops. gluetun's firewall blocks everything else.
- **Other Umbrel apps can't reach YAMS's apps.** Sonarr, Radarr, Prowlarr, Bazarr, qBittorrent, Jellyfin and the VPN live on a private network of their own. Only the router joins Umbrel's shared network, so every web page sits behind your Umbrel login.
- **The dashboard's controls aren't on any network.** They're served on a file socket that only the router can reach. On the network, the dashboard answers only the two read-only home-screen widgets.
- **Commands must come from the dashboard page.** Each one carries a custom header that other websites can't add, so a malicious page open in your browser can't send commands.
- **Secrets stay on your Umbrel.** Passwords, API keys and the VPN key are generated on first boot and stored in files only root can read. The dashboard hides the password until you press **Show**. **Change password** updates Jellyfin, qBittorrent, Sonarr and Radarr together.
- **Docker access is limited.** The dashboard uses Docker only for YAMS's own containers, identified by their Compose project label.
- **Extras keep secrets private.** Settings files for Recyclarr, Unpackerr and Decluttarr contain API keys and are readable only by their owner. Decluttarr never removes private-tracker torrents or torrents you added yourself.
- **Backups contain secrets.** They include the password, API keys and VPN key so a restore works. They're saved in Downloads → yams-backups with owner-only permissions. Keep downloaded copies somewhere private. If you share your Downloads folder on your network, anyone with access to that share can read them.

## For contributors

Never commit real passwords, API keys or VPN files, including in tests or examples. Use obvious placeholders like `not-a-real-password`. The test suite scans the repository for YAMS-style passwords, WireGuard private keys and API keys, and fails if it finds one.
