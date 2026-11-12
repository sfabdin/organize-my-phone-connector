# Organize My Phone (connector)

The part of [Organize My Phone](https://organizemyphone.com) that runs on your
Windows PC and talks to your iPhone over USB. It is open source so you can see
exactly what it does to your phone before you run it.

Works with an ordinary iPhone. No jailbreak, no profile to install.

**Status:** beta. Windows only.

## Use it

Download `OrganizeMyPhone.exe` from the Releases page and run it. Your browser
opens the editor; plug in the iPhone, unlock it and tap Trust.

You need [Apple Devices](https://apps.microsoft.com/detail/9np83lwlpz9k) from
the Microsoft Store, and a USB cable that carries data.

To close the app, use Quit in the editor.

## Run from source

Python 3.12 (3.13 cannot build one dependency).

```powershell
py -3.12 -m pip install -r requirements.txt
py -3.12 run.py
```

Or double-click `run.bat`.

## What it does to your phone

1. Reads the Home Screen and saves a snapshot.
2. Checks any new layout: Dock size, page capacity, folder rules, and that
   nothing would silently disappear.
3. Saves a rollback snapshot, then writes the layout.
4. Reads the phone back and compares it object for object.
5. Restores the previous layout automatically if anything went missing.

Snapshots are plain JSON in `%LOCALAPPDATA%\OrganizeMyPhone\snapshots`.

**Uninstall is the one thing a snapshot cannot undo.** It deletes the app and
its data. The connector only does it after a preview request, a single-use
confirmation that expires in three minutes, and the typed word DELETE.

Your layout is not uploaded anywhere. The app does download the editor page
from organizemyphone.com on first start and keeps a copy.

## Who can talk to it

It listens on `127.0.0.1:8777` only, and additionally:

- refuses requests not addressed to localhost (blocks DNS rebinding)
- refuses requests from websites not on its allowed list
- requires a token on every route that reads or changes the phone
- gives that token only to the page it serves itself, or to an allowed website
  after you type a six-digit code shown on your own screen

To forget every paired website, delete
`%LOCALAPPDATA%\OrganizeMyPhone\connector.json` and restart.

## What iOS does not allow

- Hiding an app in the App Library. An app dropped from the layout is put
  straight back by iOS. The editor offers an Archive folder instead.
- Adding, resizing or removing widgets (they can be moved).
- Keeping empty gaps on a page.

This uses a device service that is not a public Apple interface. An iOS update
can change it. Please include your iOS version in bug reports.

## Tests

```powershell
python tests/test_layout_ops.py
python tests/test_connector_api.py   # needs fastapi and httpx
```

Neither needs a phone.

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE) and [NOTICE.md](NOTICE.md). The editor
page served from organizemyphone.com is a separate work and is not covered.

Not affiliated with, endorsed by or sponsored by Apple Inc. iPhone and iOS are
trademarks of Apple Inc.
