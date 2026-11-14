# Install ATLAS on your own machine

ATLAS runs on one computer. It accepts connections only from that computer.

## What you need

| Tool | Version | Why |
|---|---|---|
| Python | 3.14 | Runs the server. |
| Node.js | 24 | Builds the user interface. |
| Docker | Any recent version | Runs the sandbox for the coding agent. |
| Git | Any recent version | Gets the code. |
| OpenSSL | Any recent version | Only for the site with sign-in. Git for Windows includes it. |

You also need:

- An Anthropic API key, for the AI model calls.
- A GitHub token, for live data. Create a fine-grained token with read-only access to public
  repositories. It needs no other permission.

## Get the code and install

```
git clone https://github.com/AnaghaP09/atlas.git
cd atlas
python -m venv .venv
.venv/Scripts/python -m pip install --require-hashes -r requirements.txt   # Windows
.venv/bin/python -m pip install --require-hashes -r requirements.txt       # macOS / Linux
```

Build the user interface:

```
cd ui
npm ci --ignore-scripts
npm run build
cd ..
```

Use the Python in `.venv` for every command below.

## Option 1: the simple local server

This option has no password. You choose a role with a switcher in the top bar.

1. Run setup. It asks a few questions and creates `.env`. It never shows a secret that you type.

   ```
   python -m atlas.setup
   ```

2. Open `.env`. Add your GitHub token after `GITHUB_TOKEN=`. Add your Anthropic key after
   `ANTHROPIC_API_KEY=` if you did not type it in setup. Never commit `.env`.

3. Check that everything is ready.

   ```
   python -m atlas.doctor
   ```

   Each line starts with OK, WARN or FAIL. Fix every FAIL. A WARN means that an optional
   feature is off.

4. If the doctor says the sandbox images are not prepared, get them once:

   ```
   python -m prep.images.fetch_images
   ```

5. Start ATLAS.

   ```
   python -m atlas.serve
   ```

6. Open <http://127.0.0.1:8000>.

## Option 2: the local site with sign-in

This option uses HTTPS and a user name and password for each role. The users are demo users.
This is not a real company sign-in.

1. Run the first-time setup. Replace `demo` with a short name for your organisation.

   ```
   python -m deploy.init --org demo
   ```

   Setup creates `.env`, the projects file, one demo user for each role, and local
   certificates. It prints the passwords once. Copy them now. ATLAS stores only a scrambled
   form.

2. Add your GitHub token and Anthropic key to `.env`, as in option 1.

3. Do the two one-time steps below. Setup prints the exact lines for your machine. ATLAS never
   changes these settings for you.

4. Start ATLAS.

   ```
   python -m deploy.up
   ```

   It checks the setup first. If something is missing, it says what to fix.

5. Open `https://atlas.demo.internal`. Use your own organisation name in place of `demo`.

6. To stop ATLAS, run `python -m deploy.down`.

### The two one-time steps

**Step 1: add the host names.** This lets your browser find the local site. Add this line to
your hosts file. Use your own organisation name in place of `demo`.

```
127.0.0.1 atlas.demo.internal scenario.demo.internal
```

| System | How |
|---|---|
| Windows | Open Notepad as administrator. Open `C:\Windows\System32\drivers\etc\hosts`. Add the line at the end. Save. |
| macOS | Run `sudo nano /etc/hosts`. Add the line at the end. Save. |
| Linux | Run `sudo nano /etc/hosts`. Add the line at the end. Save. |

**Step 2: trust the local certificate.** This removes the browser warning. Setup creates a
certificate file and prints its path. The private key stays on your machine.

| System | How |
|---|---|
| Windows | In PowerShell, run `certutil -user -addstore Root "<path>"`. Accept the prompt. You do not need an administrator account. |
| macOS | Run `security add-trusted-cert -r trustRoot -k ~/Library/Keychains/login.keychain-db "<path>"`. |
| Linux | Run `sudo cp "<path>" /usr/local/share/ca-certificates/atlas-local-ca.crt`. Then run `sudo update-ca-certificates`. Or import the file into your browser as an authority. |

Restart your browser after step 2.

## The projects file

A project is one set of sources that ATLAS reads. Setup copies an example file to
`data/local/projects.toml`. The example has two public projects: KubeVirt and Argo CD.

To add your own project, edit that file. For each project you give:

| Field | Meaning |
|---|---|
| `name` | The name shown in ATLAS. |
| `repository` | The GitHub repository, as `owner/name`. |
| `issue_query` | Which issues ATLAS reads. |
| `ci` | The test system: `prow` or `github_actions`. |
| `release_tag_pattern` | How release tags look. |
| `labels` | The project's labels for bugs, flaky tests and critical issues. |
| `evaluation` | `true` only for the project that the quality checks run on. |

ATLAS only reads these sources. It never writes to them. Publishing is off unless you name a
test repository of your own. Never name the upstream project's repository.

## What is not in this build

- A real company sign-in.
- Use from other computers.
- An AI model that runs inside your own network.
