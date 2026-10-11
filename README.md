# Verus Community App Store for umbrelOS

A community app store for [umbrelOS](https://umbrel.com) with one app:

- **Verus Node** (`dwarner-verus`) – a Verus (VRSC) full node running the official `verusd`, together with an Electrum server for lite wallets such as Verus Desktop. One dashboard shows sync progress, peers, the Electrum index and the connection details. It runs two images: [dwarner5522/umbrel-verus](https://github.com/dwarner5522/umbrel-verus) (node and dashboard) and [dwarner5522/umbrel-verus-electrum](https://github.com/dwarner5522/umbrel-verus-electrum) (ElectrumX).

## Add it to your Umbrel

1. Open the App Store on your Umbrel.
2. Click the `…` menu in the top right and choose **Community App Stores**.
3. Paste `https://github.com/dwarner5522/umbrel-verus-app-store` and click **Add**.
4. Open the **Verus Community App Store** and install **Verus Node**.

## Ports

| Port | Use |
| --- | --- |
| 27485 | Verus P2P. Forward it on your router to accept incoming peers. |
| 27486 | `verusd` RPC, for miners and other software on your network. |
| 17485 | Electrum server (plain TCP), for lite wallets on your network. Do not forward it. |

## Connect Verus Desktop

Open Verus Node on your Umbrel and copy the **Server address** from the Electrum server card, e.g. `umbrel.local:17485:tcp`. In Verus Desktop, enable custom Electrum servers and plain TCP in the settings, set that address as the Electrum server for VRSC, and add VRSC in lite mode.

## Memory use

`dwarner-verus/docker-compose.yml` sets three values that trade speed for memory. Edit them there if your Umbrel has room to spare:

- `VERUSD_EXTRA_ARGS: "-maxconnections=48,-dbcache=300"` – `verusd` defaults to 384 peers and a 450 MB cache.
- `CACHE_MB: "400"` – ElectrumX's indexing cache. Higher makes the first index faster.
- `mem_limit: 2g` on the Electrum server – a ceiling, not a reservation.

## Moving from the separate Verus Electrum Server app

Earlier versions of this store had a separate **Verus Electrum Server** app (`dwarner-verus-electrum`). Uninstall it before updating Verus Node, because both use port 17485. Wallets keep using the same address.

The index is rebuilt after the update. To keep the existing one instead, stop both apps and, before uninstalling the old app, move it over SSH:

```sh
sudo mv ~/umbrel/app-data/dwarner-verus-electrum/data/electrumx ~/umbrel/app-data/dwarner-verus/data/electrumx
```

This is a community package and is not maintained by the Verus Coin Foundation or Umbrel.
