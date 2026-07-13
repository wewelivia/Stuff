# Getting this on GitHub, and onto the work PC

You have a GitHub account. Here is the shortest safe path from this folder to a repo
you can pull on the Barclays PC.

## One-time: create the repo and push (from wherever you unzip this)

```bash
cd HouseView
git init
git add .
git commit -m "Restart: Bloomberg live provider + FastAPI serves HTML + snapshot fallback"

# create an EMPTY repo on github.com first (no README, so histories do not clash),
# e.g. named house-view-dashboard, then:
git branch -M main
git remote add origin https://github.com/<your-username>/house-view-dashboard.git
git push -u origin main
```

If GitHub asks for a password on push, it wants a **Personal Access Token**, not your
account password: github.com -> Settings -> Developer settings -> Personal access
tokens -> Fine-grained token, give it access to just this repo with Contents
read/write. Paste the token as the password.

**Private repo.** This is internal Barclays work, so make the repo Private when you
create it. Nothing in this folder contains data or credentials, but keep it private
regardless.

## On the Barclays PC: clone once, then pull for updates

```bash
git clone https://github.com/<your-username>/house-view-dashboard.git
cd house-view-dashboard
pip install -r requirements.txt
# then the blpapi install from requirements.txt's note
```

To pull later changes:

```bash
git pull
```

## The workflow going forward

Because Bloomberg only runs on the work PC, the natural loop is:

1. Build and edit here or on the Mac in `snapshot` mode (no Terminal needed).
2. `git push` from the Mac.
3. `git pull` on the work PC, flip `config.yaml` to `provider_mode: blpapi`, run
   `uvicorn`, and you are live off the Terminal.

Keep `config.yaml`'s `provider_mode` line as the only thing that differs between the
two machines. If you would rather not edit it each time, set the mode from an
environment variable instead and read it in `get_provider()`; say the word and I will
wire that.

## What must never be committed

The `.gitignore` already blocks `.env`, `*.key`, `*_api_key*`, `config.local.yaml`
and `secrets.yaml`. If you ever add a data-provider key, put it in one of those, not
in `config.yaml`. There are no keys in the Bloomberg path (it authenticates through
the Terminal), which is one more reason blpapi is the clean primary source.
