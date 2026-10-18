# Securing the midihub web page

The web page can change every route and mapping. On stage you don't want
someone else on the same Wi-Fi (a venue network, a band member's phone, a
curious guest) to open it and break your setup mid-song. This page compares
the options and explains what midihub does by default.

## What midihub does

1. **No password, no network.** Without a password the web page only listens
   on `127.0.0.1`, the machine midihub runs on. Nobody else can reach it, even
   if the config says `"host": "0.0.0.0"` (midihub refuses and says so in the
   log, unless you explicitly set `"allow_no_password": true`).
2. **Password login.** Run `python3 web.py --set-password` (on an installed
   box: `sudo -u midihub python3 /opt/midihub/web.py --set-password`) and
   restart. The page then listens on your network and asks for the password.
   A login lasts 30 days on that browser, so your phone or laptop stays
   logged in. Changing the password logs everyone out.
3. **Web off switch.** Set `"web": {"enabled": false}` in `config.json` (or
   start with `python3 web.py --no-web`). midihub runs from the config file
   and there is no web server at all. Turn it back on when you want to edit.

Hardening that comes with the login: the password is stored as a salted hash
(never in plain text), five wrong attempts lock that address out for a minute,
the session cookie is signed with a random key kept next to the config file,
and every change request must carry a custom header so another website can't
make your browser change settings behind your back. Security settings can't
be changed from the web page itself.

## All options compared

| Option | Stops people on your LAN | Effort | Downsides |
|---|---|---|---|
| **Password login** (built in) | Yes | One command | Plain HTTP: someone sniffing the same Wi-Fi could read the password as you type it. Fine against curious people, not against a determined attacker. |
| **Localhost only + SSH tunnel** (built-in default without password) | Yes | `ssh -L 8080:localhost:8080 user@hub` each time | Needs SSH on every device you edit from; awkward on a phone. |
| **Web off** (built-in switch) | Yes, nothing is listening | Edit one line | You can't change anything until you turn it back on. |
| Secret token in the URL | Mostly | Small | The token ends up in browser history and can be shared by accident. |
| Read-only / lock toggle | Partly | Medium | Still needs a password to unlock, so it adds little over a login. |
| Firewall allow-list (`ufw allow from <your laptop>`) | Yes | Medium | Breaks when your devices get new addresses; easy to lock yourself out. |
| Reverse proxy with HTTPS (Caddy, nginx) | Yes, and encrypted | High | Certificates on a LAN are fiddly; another service to maintain. |
| Hub runs its own Wi-Fi hotspot | Yes, if the hotspot has a strong key | High | Your devices must join the hub's network to edit. |

## Recommendation

Use the **password login** and, if you want to be completely sure during a
show, the **web off** switch. Together they cover the real risk (people on
the same network poking at the page) with one command and one line of config,
need no outside services, and work from any phone or laptop.

If you need protection against someone actively eavesdropping on the network,
put midihub behind a reverse proxy with HTTPS, or keep it on localhost and use
an SSH tunnel. Both work with midihub as it is.
