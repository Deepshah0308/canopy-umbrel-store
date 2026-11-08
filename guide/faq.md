# Frequently asked questions

??? question "How do I add a community app store to Umbrel?"
    In Umbrel, open the **App Store**, click **⋯** in the top right and choose **Community App Stores**. Paste `https://github.com/Deepshah0308/canopy-umbrel-store` and click **Add**. Canopy's apps then show up in their own section of the App Store.

??? question "How do I install Jellyfin, Sonarr and Radarr on Umbrel together?"
    Install **YAMS** from Canopy. It sets up Jellyfin, Sonarr, Radarr, Prowlarr, Bazarr and qBittorrent behind a VPN in one install, already connected to each other, with a dashboard to finish setup. See the [YAMS overview](yams/index.md).

??? question "Do I need to use a terminal?"
    No. Add the store address, click Install. Apps that need a login show it on their page in the App Store.

??? question "Is it safe?"
    Community stores aren't reviewed by Umbrel, so it's fair to ask. Every app is pinned to an exact, published version from its official publisher, apps without their own login sit behind your Umbrel login, and nothing gets more access than it needs. See [Security](security.md), and [report problems privately](https://github.com/Deepshah0308/canopy-umbrel-store/security).

??? question "Will it work on my Raspberry Pi?"
    Yes. Every app must run on both Umbrel Home and PCs (amd64) and on Raspberry Pi (arm64), and the store's checks refuse any version that doesn't.

??? question "Can Jellyfin use my graphics card?"
    Yes, on umbrelOS 2.1 or newer. See [Hardware transcoding](yams/transcoding.md).

??? question "Umbrel's terminal says a YAMS container is not running"
    Update YAMS to 1.4.5 or newer. Older versions opened Umbrel's terminal in the one-time setup container, which always stops after it finishes.

??? question "An app I want isn't here."
    [Suggest it](https://github.com/Deepshah0308/canopy-umbrel-store/discussions). If Umbrel's official store already has it, install it from there; Canopy doesn't duplicate official apps.

??? question "Something's broken."
    For YAMS, try **Quick fixes → Fix everything** on its dashboard first, then see [Troubleshooting](yams/troubleshooting.md). Otherwise [open an issue](https://github.com/Deepshah0308/canopy-umbrel-store/issues/new/choose) with the app's logs (right-click the app in Umbrel → Troubleshoot).
