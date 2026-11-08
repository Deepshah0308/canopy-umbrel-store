# Hardware transcoding

Jellyfin can use your Umbrel's graphics card to convert video on the fly, which is much faster and quieter than using the CPU.

!!! note "Needs umbrelOS 2.1 or newer and YAMS 1.4.6 or newer"
    YAMS asks Umbrel for GPU access, and Umbrel passes in whatever graphics hardware it finds (Intel, AMD or NVIDIA). It's re-applied on every update, so you never edit a compose file. Older umbrelOS versions ignore the request.

## Turn it on in Jellyfin

Jellyfin has two menus called "Playback". The transcoding settings are in the **admin Dashboard**, not your user settings.

1. Open `http://umbrel.local:8097/web/#/dashboard/playback/transcoding`, or click **☰ → Dashboard**, then **☰ → Playback → Transcoding**. You need the admin login shown in the YAMS dashboard under **Your login**.
2. Set **Hardware acceleration**:
    - **NVIDIA NVENC** for NVIDIA cards
    - **Intel QuickSync (QSV)** or **Video Acceleration API (VAAPI)** for Intel and AMD graphics
3. Tick the codecs your card supports under **Enable hardware decoding for**, then **Save** at the bottom.

## Check it works

Play something and force a lower quality in the player. In **Dashboard → Activity**, the stream should say it's being transcoded with your hardware. If playback fails, switch Hardware acceleration back to **None**, save, and [open an issue](https://github.com/Deepshah0308/canopy-umbrel-store/issues/new/choose) with Jellyfin's transcode log.

See Jellyfin's own guides for [NVIDIA](https://jellyfin.org/docs/general/post-install/transcoding/hardware-acceleration/nvidia) and [Intel](https://jellyfin.org/docs/general/post-install/transcoding/hardware-acceleration/intel).
