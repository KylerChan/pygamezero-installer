# Pygame Zero Installer

One command that sets up everything you need to make games with **Pygame** and **Pygame Zero** on Windows, macOS or Linux. Made for our school Game Development CCA.

## What are Pygame and Pygame Zero?

- **[Pygame](https://www.pygame.org/)** is a Python library for making games: it draws pictures, plays sounds and reads the keyboard and mouse.
- **[Pygame Zero](https://pygame-zero.readthedocs.io/)** is built on top of Pygame and makes it much simpler. You write a few functions (`draw()`, `update()`) and run your game with the `pgzrun` command.

## Supported computers

| System | Supported | Notes |
|---|---|---|
| Windows 10 / 11 (64-bit) | Yes | Windows on ARM uses the x64 version of Python (Pygame has no ARM build for Windows) |
| macOS (Apple Silicon and Intel) | Yes | |
| Linux: Ubuntu, Debian, Fedora, Arch, openSUSE and similar | Yes | x86-64 and ARM64. Alpine Linux is **not** supported |

You need **Python 3.9 to 3.13** (3.13 is recommended). **Python 3.14 and newer will not work yet**, because Pygame 2.6.1 (the newest version) has no ready-made packages for them. You don't need to work this out yourself: the installer finds a suitable Python on your computer, or tells you exactly how to install one.

## Install

Open a terminal and paste **one** of these commands:

Step-by-step instructions with copy buttons: **https://kylerchan.github.io/pygamezero-installer/**

**macOS or Linux** (open the *Terminal* app):

```bash
curl -fsSL https://kylerchan.github.io/pygamezero-installer/install.sh | bash
```

**Windows** (open *PowerShell* from the Start menu, not "Command Prompt"):

```powershell
irm https://kylerchan.github.io/pygamezero-installer/install.ps1 | iex
```

The website always serves the latest release. To pin this exact version instead, use `https://raw.githubusercontent.com/KylerChan/pygamezero-installer/v1.0.0/install.sh` (or `install.ps1`).

You should see something like this:

```text
Pygame Zero Installer (v1.0.0)

[1/7] Detecting operating system... Windows 11, x86_64
[2/7] Checking Python... Python 3.13.5 (C:\Users\you\AppData\Local\Programs\Python\Python313\python.exe)
[3/7] Preparing environment... OK (created with Python 3.13.5)
[4/7] Installing Pygame... OK (pygame 2.6.1)
[5/7] Installing Pygame Zero... OK (pgzero 1.2.1)
[6/7] Adding the pgzrun command... OK (...)
[7/7] Verifying installation... OK (pygame 2.6.1, pgzero 1.2.1)

Installation complete!

Open a NEW terminal window first, so it can find pgzrun.

Run your first game:
    pgzrun game.py
```

**Close the terminal and open a new one** before you use `pgzrun`. New terminals pick up the new command, but the old window won't.

If you don't have a suitable Python yet, the installer explains what to do. On Windows (with `winget`) and on macOS (with Homebrew) it can install Python for you, but only after asking and getting a "yes". It never uses administrator rights and never asks for your password.

## Make your first game

1. Make a folder for your game, for example `my-game`.
2. Inside it, create a file called `game.py` with this code:

   ```python
   WIDTH = 500
   HEIGHT = 400

   x = 0

   def draw():
       screen.fill((30, 30, 60))
       screen.draw.filled_circle((x, 200), 30, "orange")
       screen.draw.text("Hello, Pygame Zero!", (20, 20), fontsize=40)

   def update():
       global x
       x = (x + 3) % WIDTH
   ```

3. In a terminal, go into the folder and run the game:

   ```bash
   cd my-game
   pgzrun game.py
   ```

A window opens with a moving orange ball. Close the window to stop the game.

Put images in an `images` folder and sounds in a `sounds` folder next to `game.py`. The [Pygame Zero tutorial](https://pygame-zero.readthedocs.io/en/stable/introduction.html) shows what you can do next.

### Using an editor (VS Code, Thonny, Mu)

The installer finishes by printing a line like **"Python for your editor"**. Choose that Python as the interpreter in your editor, and plain Pygame programs (`import pygame`) will work there too:

| System | Path |
|---|---|
| Windows | `%LOCALAPPDATA%\pygamezero\venv\Scripts\python.exe` |
| macOS / Linux | `~/.local/share/pygamezero/venv/bin/python` |

## What the installer does

1. Checks your operating system and processor type.
2. Looks for every Python on your computer (`python3`, `python`, `python3.X`, the Windows `py` launcher and the usual install folders) and picks the newest one that works with Pygame. It skips Pythons that are too new, too old, free-threaded builds, ones that belong to another virtual environment, or ones missing `venv` support.
3. Creates a private Python environment (a "virtual environment") just for Pygame Zero. **Your other Python installations are not changed**: nothing is installed into them, and `--break-system-packages` is never used.
4. Installs `pygame==2.6.1`, then `pgzero==1.2.1`, using ready-made packages only (it never tries to compile Pygame).
5. Adds a `pgzrun` command and makes sure new terminals can find it.
6. Proves the setup works by running a tiny Pygame Zero game invisibly. "Installation complete!" only appears if this succeeds.

Running the installer again is safe. It reuses what is already there and repairs anything missing (`--recreate` rebuilds the environment from scratch).

**Where things go:**

| | Windows | macOS / Linux |
|---|---|---|
| Environment | `%LOCALAPPDATA%\pygamezero\venv` | `~/.local/share/pygamezero/venv` (or `$XDG_DATA_HOME/pygamezero`) |
| `pgzrun` command | `%LOCALAPPDATA%\pygamezero\bin\pgzrun.exe` | `~/.local/bin/pgzrun` (a small script) |
| PATH change | Adds the `bin` folder to your **user** PATH | Adds a marked block to `~/.zshrc` (zsh), `~/.bashrc` (bash; also `~/.bash_profile` on macOS), `~/.config/fish/conf.d/pygamezero.fish` (fish) or `~/.profile` |

If a `pgzrun` that this installer didn't create already exists in `~/.local/bin`, it is left untouched and the installer tells you the full path to use instead.

## Install without `curl | bash` (inspect first)

Piping a script from the internet straight into a shell runs it before you can read it. If you'd rather check it first:

**macOS / Linux**

```bash
curl -fsSLO https://raw.githubusercontent.com/KylerChan/pygamezero-installer/v1.0.0/install.sh
curl -fsSLO https://raw.githubusercontent.com/KylerChan/pygamezero-installer/v1.0.0/install.py
less install.sh install.py       # read them
bash install.sh                  # uses the install.py next to it
```

**Windows**

```powershell
irm https://raw.githubusercontent.com/KylerChan/pygamezero-installer/v1.0.0/install.ps1 -OutFile install.ps1
irm https://raw.githubusercontent.com/KylerChan/pygamezero-installer/v1.0.0/install.py -OutFile install.py
notepad install.ps1              # read them
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Or skip the bootstrappers entirely. `install.py` is the whole installer and only needs Python:

```bash
python3 install.py          # macOS / Linux
py install.py               # Windows
```

### Fully manual install

If you prefer to do everything yourself (replace `python3` with `py -3.13` on Windows):

```bash
python3 -m venv ~/pgzero-env
~/pgzero-env/bin/python -m pip install pygame==2.6.1 pgzero==1.2.1
~/pgzero-env/bin/pgzrun game.py
```

(On Windows the programs are in `pgzero-env\Scripts\` instead of `pgzero-env/bin/`.)

## Options

Add these after `install.py`, or after `bash -s --` when using curl (for example `curl -fsSL .../install.sh | bash -s -- --uninstall`):

| `install.py` / `install.sh` | `install.ps1` | What it does |
|---|---|---|
| `--uninstall` | `-Uninstall` | Remove everything the installer created |
| `--recreate` | `-Recreate` | Delete and rebuild the environment |
| `--no-modify-path` | `-NoModifyPath` | Don't change PATH; print instructions instead |
| `--yes` | `-Yes` | Accept the offer to install Python with winget/Homebrew |
| `--python PATH` | | Use a specific Python |

To pass options with `irm` on Windows, use: `& ([scriptblock]::Create((irm https://raw.githubusercontent.com/KylerChan/pygamezero-installer/v1.0.0/install.ps1))) -Uninstall`

Environment variables `PGZERO_HOME` (where the environment goes) and `PGZERO_BIN_DIR` (where `pgzrun` goes) change the default folders.

## Troubleshooting

| Problem | What to do |
|---|---|
| `pgzrun: command not found` / `pgzrun is not recognized` | Close the terminal and open a **new** one. Still not working? Use the full path from the table under "Where things go", or run the installer again and read the last lines it prints. |
| "No compatible Python found" / "Python 3.14 is too new" | Install Python 3.13 using the command the installer shows (for example `winget install --id Python.Python.3.13 -e --scope user`, or `brew install python@3.13`), open a new terminal, and run the installer again. You can keep your other Python. |
| "Virtual environment support is missing" (Ubuntu/Debian) | Run the `sudo apt install python3.X-venv` command the installer shows, then run the installer again. |
| "Pygame needs the system library ..." (Linux) | Minimal Linux installs can lack a library Pygame uses (usually GLib). Run the `sudo apt install ...` (or `dnf`/`pacman`) command the installer prints, then run it again. |
| "Could not download packages" | You're offline, or a school proxy/firewall blocks `pypi.org`. Try another network or ask IT to allow `pypi.org` and `files.pythonhosted.org`. |
| "Permission denied" | Close any running games and editors, then try again. **Don't** use `sudo` or "Run as administrator". |
| Windows: "running scripts is disabled on this system" | Use the `irm ... \| iex` command, or run a downloaded script with `powershell -ExecutionPolicy Bypass -File .\install.ps1`. |
| macOS: a pop-up asks to install "command line developer tools" | That's macOS offering its own Python. You can cancel it and install Python 3.13 from python.org instead. |
| The game window doesn't open on Linux | You need a desktop session; it won't open over plain SSH. |

**Exit codes** (useful for teachers and scripts): `0` success, `1` unexpected error, `2` unsupported system, `3` no compatible Python, `4` environment problem, `5` package install failed, `6` network problem, `7` permission problem, `8` verification failed.

## Uninstall

macOS / Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/KylerChan/pygamezero-installer/v1.0.0/install.sh | bash -s -- --uninstall
```

Windows:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/KylerChan/pygamezero-installer/v1.0.0/install.ps1))) -Uninstall
```

This removes the environment, the `pgzrun` command and the PATH changes it made. It does not touch your games, your other Python installations, or a `pgzrun` it didn't create. If you installed Python itself through the installer's offer, remove it the normal way (Windows *Settings → Apps*, or `brew uninstall python@3.13`).

## Security

This installer runs code downloaded from the internet, so here's exactly what it does and doesn't do:

- **Pinned and verified.** The commands above point at a fixed release tag (`v1.0.0`), not a moving branch. `install.sh` and `install.ps1` download `install.py` over HTTPS only (plain `http://` is refused) and check its **SHA-256 fingerprint** against the value written into the bootstrapper. If they don't match, nothing is run.
- **The remaining risk:** with `curl | bash` or `irm | iex` you trust whoever controls the GitHub repository and the HTTPS connection at the moment you run it. If you'd rather not, use [Install without `curl | bash`](#install-without-curl--bash-inspect-first) and read the scripts first. They're plain text and short.
- **No administrator rights.** Everything goes into your own user folders. The installer never runs `sudo` and never asks for a password or any credentials. On Linux it only *prints* the `sudo apt ...` style commands you might need.
- **Asks before installing anything system-wide.** Python is only installed through winget (`--scope user`, the official python.org package) or Homebrew, and only after you type `y`.
- **Packages come from PyPI** through pip, pinned to exact versions, as pre-built wheels only.
- **Careful with your files.** It only deletes folders that carry its own marker file, only edits text between its own `# >>> pygamezero-installer >>>` markers in shell startup files, and never overwrites a `pgzrun` it didn't create. Paths with spaces or non-English characters are handled. No shell command is built from downloaded text.

---

## For maintainers

### Project layout

```text
install.sh      macOS/Linux bootstrapper: finds any Python 3, fetches + verifies install.py
install.ps1     Windows bootstrapper: same, for PowerShell 5.1 and 7+
install.py      the actual installer (standard library only; written for Python 3.6+, tested on 3.9 and 3.13-3.15)
tools/release.py  writes repo, tag and install.py's SHA-256 into the bootstrappers + README
website/       the GitHub Pages site (deployed with install.sh/install.ps1 on each tag)
tests/          unit tests (mocked failures) and an opt-in real install test
.github/workflows/test.yml  CI on Windows, macOS (ARM + Intel) and Linux (x64 + ARM)
```

All the decisions (which Python, venv, pip, PATH, verification) live in `install.py`, and the bootstrappers stay small. The only logic they repeat is a short "Python is missing" message, because without Python, `install.py` can't run.

### Publishing a release

1. Push this folder to a GitHub repository (public, so `raw.githubusercontent.com` can serve it).
2. Write your repository and tag into the scripts and this README:

   ```bash
   python3 tools/release.py --repo YOUR-USER/pygamezero-installer --version v1.0.0
   ```

   Keep `INSTALLER_VERSION` in `install.py` equal to the tag without the `v` (a test checks this).
3. Commit, push, and wait for CI to go green.
4. Create the tag: `git tag v1.0.0 && git push origin v1.0.0`.
5. Share the commands from the **Install** section.

Whenever `install.py` changes, run `python3 tools/release.py` again (it refreshes the hash; CI fails if you forget) and release under a **new** tag. Never move an existing tag.

**Website.** `.github/workflows/pages.yml` publishes `website/index.html`, `install.sh` and `install.ps1` to GitHub Pages whenever a `v*` tag is pushed, then runs the public `curl | bash` and `irm | iex` commands against the live site on Linux, macOS and Windows. The served scripts still download `install.py` from their pinned tag and verify its hash.

### Running the tests

```bash
python3 -m unittest discover -s tests -v                    # fast unit tests, no network
PGZ_INTEGRATION=1 python3 -m unittest tests.test_installer.RealInstallTests -v   # real install into a temp folder
python3 tools/release.py --check                            # bootstrapper hashes up to date
```

The unit tests cover OS/architecture detection, interpreter selection and ranking, missing Python, missing `venv`, failed package installs (including the partial-install report), network and permission errors, PATH editing (shell files and a fake Windows registry), launcher safety, and the bootstrapper's integrity pin.

CI (`.github/workflows/test.yml`) additionally runs, for real:

- the unit tests on Windows, macOS and Ubuntu with Python 3.9, 3.13 and 3.14;
- a full install, repeat install, game launch from a folder with spaces and Unicode, and uninstall on Ubuntu (x64 and ARM), macOS (Apple Silicon and Intel) and Windows;
- `install.sh` and `install.ps1` (PowerShell 7 and Windows PowerShell 5.1) end to end, including a new-terminal `pgzrun game.py` check, `curl | bash` and `irm | iex` against the commit being tested, rejection of a tampered download, an unreachable package index (exit 6) and a read-only install folder (exit 7);
- Linux containers: Ubuntu 24.04 and Debian 12 system Python (succeeds), Ubuntu without `python3-venv` (exit 3 with the right `apt` command), a Python-3.14-only image (exit 3, "too new") and an image with no Python (exit 3).
