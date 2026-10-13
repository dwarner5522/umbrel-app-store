# dwarner5522 App Store for umbrelOS

A community app store for [umbrelOS](https://umbrel.com) with three apps. The first two are listed under **Verus Coin**, the third under **Mining**:

- **Verus Node** (`dwarner-verus`) – a Verus (VRSC) full node running the official `verusd`, together with an Electrum server for lite wallets such as Verus Desktop. One dashboard shows sync progress, peers, the Electrum index and the connection details. It runs two images: [dwarner5522/umbrel-verus](https://github.com/dwarner5522/umbrel-verus) (node and dashboard) and [dwarner5522/umbrel-verus-electrum](https://github.com/dwarner5522/umbrel-verus-electrum) (ElectrumX).

- **Verus Datum** (`dwarner-verus-datum`) – a stratum server for solo mining Verus with your own miners, using block templates from Verus Node. Block rewards are paid straight to the R-address you enter. It requires Verus Node. The image is built from [dwarner5522/umbrel-verus-datum](https://github.com/dwarner5522/umbrel-verus-datum), which runs [verus-datum-cli](https://github.com/dwarner5522/verus-datum-cli).
- **Dualminer Control** (`dwarner-dualminer`) – a control panel for Orange Pi 5 boards running [dualminer](https://github.com/dwarner5522/dual-cpu-miner-orangepi5), the Monero and Verus dual CPU miner. It shows every board on your network and switches one or all of them between mining both coins, Monero only or Verus only. It needs neither of the other apps. The panel's code is in this repository, in `dwarner-dualminer/app`, and runs on the stock `python` image.

The apps are written to work on current umbrelOS and on Umbrel 0.5.4. On 0.5.4 the home screen widgets are not shown and the Electrum server's memory ceiling is not applied.

## Add it to your Umbrel

1. Open the App Store on your Umbrel.
2. Click the `…` menu in the top right and choose **Community App Stores**.
3. Paste `https://github.com/dwarner5522/umbrel-app-store` and click **Add**.
4. Open the **dwarner5522 app store** and install **Verus Node**, listed under **Verus Coin**.

## Ports

| Port | Use |
| --- | --- |
| 27485 | Verus P2P. Forward it on your router to accept incoming peers. |
| 27486 | `verusd` RPC, for miners and other software on your network. |
| 17485 | Electrum server (plain TCP), for lite wallets on your network. Do not forward it. |
| 9999 | Verus Datum stratum server (plain TCP), for miners on your network. Do not forward it. |

## Connect Verus Desktop

Open Verus Node on your Umbrel and copy the **Server address** from the Electrum server card, e.g. `umbrel.local:17485:tcp`. In Verus Desktop, enable custom Electrum servers and plain TCP in the settings, set that address as the Electrum server for VRSC, and add VRSC in lite mode.

## Solo mining with Verus Datum

Install **Verus Datum** once Verus Node has synced. Open it, enter the Verus R-address that should receive block rewards, and point your miners at the stratum address it shows, e.g.:

```sh
ccminer -a verus -o stratum+tcp://umbrel.local:9999 -u solo.miner1 -p x
```

The user name is free text and only tells your miners apart. Rewards always go to the address entered in the app: it asks Verus Node to pay that address and never hands out work that pays anywhere else. Use this node for one payout address only; other mining software asking the same node for work with a different address can get in its way.

## Controlling your Orange Pi miners with Dualminer Control

1. On each Orange Pi, add a `remote` block to dualminer's `config.json`, with the same token on every board, and restart dualminer:

   ```json
   "remote": { "port": 8377, "token": "a-long-random-secret" },
   ```

2. Install **Dualminer Control**, open it and save that token under **Shared token**.
3. Add each board by IP address, or enter your network range (e.g. `192.168.1.0/24`) under **Find miners on the network**.

Use the buttons on a board's card to switch that board, or the **All miners** row to switch every board at once. Use IP addresses rather than `.local` names, and give the boards fixed addresses in your router so they stay in the list. The panel reaches the boards over plain HTTP on port 8377; do not forward that port.

## Merge mining PBaaS chains

Verus Node can run the vARRR, vDEX and CHIPS chains next to VRSC. Both apps show the same list with
two switches per chain:

- **Run chain** starts that chain's own node inside the Verus Node app. It syncs from the network
  first; the status column shows the progress.
- **Merge mine** has the chain mined together with VRSC. It switches the chain on if needed and
  takes effect once the chain and VRSC are both synced.

Miners need no change: they keep mining at `stratum+tcp://umbrel.local:9999`. Rewards of a
merge-mined chain are paid in that chain's coin to the payout address shown next to the switches.
Saving a payout address in Verus Datum sets it; it can also be changed in Verus Node. To see the
rewards, add the chain in a wallet that holds that address.

Each running chain uses about 1 to 2 GB of memory and a few gigabytes of storage, so switch on only
what your Umbrel has room for. Switching a chain off keeps its downloaded blockchain.

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
