# midihub tutorial

This guide takes you from nothing to a working hub: install, add devices,
route them, translate controls, and run it unattended on a dedicated box.
The examples use a generic **keyboard controller** (`keys`) and a **synth**
(`synth`); replace them with your own gear.

---

## Part A: Install on a Linux PC

midihub needs Linux with ALSA (every desktop distribution has it), Python 3,
`python-rtmidi`, Flask and `aconnect` (from `alsa-utils`).

```bash
# Debian / Ubuntu / Raspberry Pi OS
sudo apt install python3-rtmidi python3-flask alsa-utils
# Arch / Manjaro
sudo pacman -S --needed python-rtmidi python-flask alsa-utils
# Fedora
sudo dnf install python3-rtmidi python3-flask alsa-utils
```

Make sure your user may use MIDI devices:

```bash
groups | grep -q audio || sudo usermod -aG audio "$USER"   # log out and in afterwards
```

Get midihub and start it:

```bash
git clone https://github.com/<you>/midihub.git
cd midihub
python3 web.py
```

Open <http://localhost:8080>. Plug in your devices (before or after starting,
midihub looks for new ports every two seconds).

Optional checks:

```bash
python3 -m unittest discover -s tests -t .   # logic tests, no hardware needed
python3 check.py                             # end-to-end through ALSA "Midi Through"
aconnect -l                                  # list the MIDI ports Linux sees
```

---

## Part B: The web page

The top bar shows how many devices are connected, whether the loop guard has
something to say, and the **Save** button. A small dot next to Save means you
have unsaved changes. Nothing changes on the hub until you save (Ctrl+S works
too).

### Devices

Every port Linux sees is listed as a chip at the bottom. Click one, check the
name (short and lowercase, like `keys`), and press **Add device**. The device
matches any port whose name contains its *match text*, so it is found again
after unplugging or rebooting. **Edit** lets you rename a device or change its
match text; routes and mappings follow the new name.

### Routes

A grid of devices: rows send, columns receive. Tick `keys` → `synth` and every
note, CC and SysEx from the keyboard reaches the synth. After saving, each
ticked box shows how it is connected:

- **direct**: wired by the Linux MIDI system. No delay added; this is the
  normal case.
- **filtered**: passes through midihub because a mapping replaces some of the
  messages on this route (see *Replace the original message* below).
- **blocked**: this route would make a loop and was not connected. See Part E.

Tip: if a device is also connected to another one with a MIDI cable, don't add
the same route in midihub as well, or every note plays twice.

### Mappings

A mapping listens for one message and sends another. The form has two sides:

- **When this comes in**: device, message type, channel and number.
- **Send this**: target device, message type, channel and number.
  *Where its routes go* sends to every device the input device is routed to.
  *Same as incoming* (channel) and an empty number reuse the incoming values.

Message types: control change (CC), note, program change, channel pressure,
poly aftertouch, pitch bend and SysEx. Any type can be mapped to any other, so
MIDI → MIDI, MIDI → SysEx, SysEx → MIDI and SysEx → SysEx all work.

**Learn** fills a side in for you. Press it, then:

- for a CC, note, program change or pitch bend: move the control or press the
  key once;
- for SysEx: move the control back and forth a little. midihub compares the
  messages, finds the byte that changes (the value) and fills in the message
  and the value byte position. If the message never changes (a button that
  sends one fixed message), press **Learn** again to keep it as it is.

The right-hand Learn copies a message from the device that should receive it.
That is the easiest way to get a synth's SysEx: move the control on the synth
itself, and midihub learns what to send it. **▶ Test** sends the output once
at full value so you can check the synth reacts.

Options:

- **Value range**: incoming min/max to outgoing min/max. `0 127 → 0 64`
  halves the range; `0 127 → 127 0` inverts it.
- **Replace the original message**: normally a route passes everything on, so
  the synth gets both the original CC and the translated one. Tick this to
  hold the original back on the routes to the mapping's target. Those routes
  then run through midihub (shown as *filtered*).

### Monitor

Press **Start listening** to see every message your devices send, decoded
("CC 20 = 64 · ch 1"). **Use as input** / **Use as output** copies a message
into the mapping form. **Send a message** pushes raw hex bytes to a device.

### Settings

- **Startup messages**: sent to a device every time it connects, e.g. to put a
  synth in the right mode. Separate several messages with `|`.
- **Loop protection**: see Part E.
- **Network MIDI**: see Part F.
- **Web access**: shows whether a password is set and where the page listens.

---

## Part C: A first setup

Goal: the keyboard plays the synth, the keyboard's mod wheel controls the
synth's filter, and one keyboard fader moves a parameter the synth only
accepts as SysEx.

1. **Devices**: add the keyboard as `keys` and the synth as `synth`.
2. **Routes**: tick `keys` → `synth`. Save. Play: you hear the synth.
3. **CC → CC**: in Mappings press the left **Learn** and move the mod wheel
   (CC 1). On the right choose `synth`, *Control change*, number `74`. Tick
   *Replace the original message* so the synth gets only CC 74. Add mapping.
4. **CC → SysEx**: press the left **Learn** and move the fader. Press the
   right **Learn** and move the matching control on the synth itself. Press
   **▶ Test**: the synth's control should jump to maximum. Add mapping.
5. **Save**. Move the fader: the synth follows.

Many synths only send and receive SysEx when it is allowed in their global MIDI
settings (often called *SysEx*, *Exclusive* or *MIDI filter*). If Learn sees
nothing from the synth, check that first. SysEx formats differ between models
and sometimes between sizes of the same model, so always learn from your own
unit instead of copying messages from elsewhere.

---

## Part D: Dedicated box (Raspberry Pi or any Debian machine)

A Raspberry Pi 3 or newer is plenty. Power your instruments separately; the
Pi's USB ports only carry MIDI data.

1. Flash **Raspberry Pi OS Lite** with Raspberry Pi Imager. In its settings,
   set a hostname, a user and password, enable SSH and set up Wi-Fi (or use a
   cable).
2. Log in over SSH and install:

   ```bash
   sudo apt install -y git
   git clone https://github.com/<you>/midihub.git
   cd midihub
   sudo ./setup.sh            # add --no-qmidinet to skip network MIDI
   ```

   `setup.sh` installs the dependencies, creates the `midihub` user, copies
   the app to `/opt/midihub`, asks for a web password and enables the
   `midihub` service at boot. Press Enter at the password prompt to keep the
   page local; you can set one later (Part G).
3. Open `http://<hub-address>:8080` from your laptop or phone, log in and set
   up devices, routes and mappings as in Part C.
4. Reboot to check everything comes back on its own:

   ```bash
   sudo reboot
   # afterwards
   systemctl status midihub
   journalctl -u midihub -n 50
   ```

From now on the hub starts midihub at boot with your saved config. Devices
can be switched on later; they are picked up within a couple of seconds.

The config is `/opt/midihub/config.json`. You can edit it by hand; midihub
reloads it when the file changes.

- Update: `git pull && sudo ./update.sh` (keeps your config, restarts, and
  restores the old config if the new version fails to start).
- Remove: `sudo ./uninstall.sh` (keeps a backup) or `sudo ./uninstall.sh --purge`.

---

## Part E: Loop protection

A MIDI loop happens when messages come back to where they started, for example
when a route goes `keys` → `synth`, another goes `synth` → `keys`, and the
synth echoes what it receives. Each pass creates new messages and within
moments the devices are flooded. midihub protects you in three ways:

1. **Loops are not wired.** When saving, midihub checks the routes. A route
   that would close a circle (two-way or longer, like A → B → C → A) is
   marked *blocked* and not connected. The Loop guard card explains which
   route and why. If you really need two-way routing, untick *Don't connect
   routes that would make a loop* in Settings; the other two protections stay
   active.
2. **Bounces are caught.** midihub remembers what was just sent to each
   device. If the same messages come straight back many times per second, it
   mutes that device's routes and mappings for a few seconds and shows a red
   warning. If it keeps happening, the device stays muted until you press
   **Reset**. Messages that bounce back are never translated again, so a
   mapping can't feed itself.
3. **Floods are reported.** A device sending more messages per second than the
   *flood warning* limit raises a warning. Tick *Also mute devices that flood*
   if you want it muted as well.

midihub also removes its connections when it stops, so a loop can't survive
a crash or a restart.

---

## Part F: Network MIDI with qmidinet (optional)

`qmidinet` shares MIDI over the local network using UDP multicast. `setup.sh`
installs it and runs it as a service (skip it with `--no-qmidinet`).

- In **Settings → Network MIDI**, tick the devices to share and save. midihub
  sends their messages to the network and passes network messages back to them.
- On another Linux computer run `qmidinet` and connect your software to its
  ports. Windows and macOS need a compatible multicast MIDI tool such as
  ipMIDI.
- Port and multicast address must match on every machine (defaults: 21928 and
  225.0.0.37). The values in midihub's settings are for reference; qmidinet
  itself is configured on its own.
- Wi-Fi adds latency and jitter. It's fine for faders and SysEx; for tight
  playing prefer a cable.

---

## Part G: Securing the web page

Without a password the page only works on the hub itself. To use it from
other devices, set a password and restart:

```bash
# PC
python3 web.py --set-password
# installed box
sudo -u midihub python3 /opt/midihub/web.py --set-password
sudo systemctl restart midihub
```

To run without any web page (for example during a show), set this in
`config.json` and restart; set it back to `true` to edit again:

```json
"web": { "enabled": false }
```

`--clear-password` removes the password. See [SECURITY.md](SECURITY.md) for
all options and the reasoning.

---

## Troubleshooting

| Symptom | What to check |
|---|---|
| Device not listed | `aconnect -l`. Check the USB cable and power. midihub rescans every two seconds. |
| Web page not reachable from another device | No password set? Then it only listens on the hub itself (Part G). Check `journalctl -u midihub` for the address it listens on. |
| Notes play twice | The same connection exists twice (a MIDI cable and a midihub route, or two routes). |
| Route shows *blocked* | It would make a loop. See Part E. |
| Learn sees no SysEx from a synth | Enable SysEx in the synth's global MIDI settings. |
| SysEx sent but the synth ignores it | Learn it from your own unit; formats differ between models. Check the device ID setting on the synth. |
| Startup message not applied | It's sent when the device connects. Unplug and replug, or check the message in the Monitor. |
| Config changes ignored | Check `journalctl -u midihub` for a JSON error; midihub keeps the last good config. |
| A device got muted | The loop guard caught messages bouncing back. Look for a two-way route or a "MIDI thru"/"echo" setting on the device, then press Reset. |
| qmidinet not working | `systemctl status qmidinet`, `aconnect -l` should show `QmidiNet`. Port and multicast address must match on all machines. |
