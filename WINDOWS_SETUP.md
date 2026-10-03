# Windows step-by-step guide (run it, push it to GitHub, submit it)

You do **not** need to know programming to follow this. Copy each command exactly.

## How to open "CMD" (the black command window)
Press the **Windows key**, type `cmd`, press **Enter**. A black window opens. That is CMD.
Anything in a grey box below is typed into CMD, followed by **Enter**.

---
## PART A - Install 3 programs (one time only)

1. **Python 3.12 or newer** - https://www.python.org/downloads/
   On the first installer screen **tick "Add python.exe to PATH"**, then click *Install Now*.
2. **Docker Desktop** - https://www.docker.com/products/docker-desktop/
   Install, restart the PC if asked, then open Docker Desktop and wait until it says it is running.
   (It gives us the database with the map add-on, PostGIS, already set up.)
3. **Git for Windows** - https://git-scm.com/download/win  (press *Next* on every screen).

Check they work. Close CMD, open a **new** CMD, then type:
```
python --version
git --version
docker --version
```
Each should print a version. `python` must say 3.12 or higher.

---
## PART B - Run the project

**B1. Unzip.** Right-click `iris-upsert.zip` -> *Extract All* -> Extract. You get a folder `iris-upsert`.

**B2. Open CMD inside that folder.** Open the folder in File Explorer, click the address bar at the
top, type `cmd`, press **Enter**. (CMD opens already in the right place.)

**B3. Start the database** (Docker Desktop must be running):
```
docker compose up -d
```
First time it downloads the database (a few minutes). Wait about 20 seconds after it finishes.

**B4. Make a private Python box for this project:**
```
python -m venv .venv
.venv\Scripts\activate
```
Your line now starts with `(.venv)`. That means it worked.
(Use **CMD**, not PowerShell; PowerShell can block this step.)

**B5. Install the project:**
```
pip install -e ".[test]"
```

**B6. Run the tests** (this proves it works):
```
pytest
```
Expected last line: `33 passed`.

**B7. Watch the whole story run:**
```
iris-promote demo
```
You will see 6 runs. Look at run 2, **"IDENTICAL RERUN: nothing changes"** (`inserted=0 updated=0`).
That is the main requirement. It also writes the manifest files into the `manifests` folder.

**B8. (Optional) Try it by hand:**
```
iris-promote run fixtures\batch1_initial.csv --batch-id mytest
iris-promote run fixtures\batch1_initial.csv --batch-id mytest
iris-promote inspect-rejects --run-id 1
```

**When finished:** `docker compose down` stops the database.
Next time you only need: `docker compose up -d` then `.venv\Scripts\activate`.

### If something goes wrong
| Message | Fix |
|---|---|
| `'python' is not recognized` | Reinstall Python and tick **Add python.exe to PATH**; open a NEW CMD. |
| `cannot connect to the database` | Open Docker Desktop, wait until running, run `docker compose up -d`, wait 20 s. |
| `port is already allocated` / port 5432 busy | Another PostgreSQL is on your PC. Stop it (Services -> postgresql -> Stop), then `docker compose up -d`. |
| `database "iris_test" does not exist` | `docker compose down -v` then `docker compose up -d` (the extra database is only created on first start). |
| `extension "postgis" is not available` | You are on a database without PostGIS. Use the Docker one from this project. |
| Docker will not start | Docker Desktop needs virtualisation/WSL2 enabled; see its on-screen help. Or use "No Docker" below. |

### No Docker? (alternative)
Install **PostgreSQL 16** from https://www.postgresql.org/download/windows/ , and at the end run
*Stack Builder* -> *Spatial Extensions* -> **PostGIS 3.4**. Using pgAdmin, create two databases
named `iris` and `iris_test`. Then in CMD (use your own password):
```
set IRIS_DATABASE_URL=postgresql://postgres:YOURPASSWORD@localhost:5432/iris
set IRIS_TEST_DATABASE_URL=postgresql://postgres:YOURPASSWORD@localhost:5432/iris_test
```
and continue from B4.

---
## PART C - Put it on GitHub (README included)

The `README.md` is already inside the project. GitHub shows it automatically on the repo page,
so you do **not** write or upload a second one.

1. Make a free account at https://github.com .
2. Click the **+** (top right) -> **New repository**.
   - Name: `iris-cand-07-upsert`
   - Choose **Private** (safer for an assessment; you can share it with the reviewer later).
   - **Do NOT tick** "Add a README", ".gitignore" or a licence (we already have them).
   - Click **Create repository**. Copy the address shown, like
     `https://github.com/YOURNAME/iris-cand-07-upsert.git`
3. In CMD, inside the `iris-upsert` folder, tell Git who you are (one time only):
```
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```
4. Send the project up:
```
git init
git add .
git status
```
   `git status` lists the files. Check that **`.venv` is NOT listed** (it is ignored on purpose).
```
git commit -m "IRIS-CAND-07: safe staging-to-core upsert promotion"
git branch -M main
git remote add origin https://github.com/YOURNAME/iris-cand-07-upsert.git
git push -u origin main
```
   A browser window may open asking you to sign in to GitHub. Sign in and allow it.
5. Refresh your repo page. You should see all files and the README shown underneath.

*Prefer clicking over typing?* Install **GitHub Desktop**, choose File -> Add local repository
-> pick the `iris-upsert` folder -> Publish repository.

**Later changes:** `git add .` then `git commit -m "what I changed"` then `git push`.

---
## PART D - Submit it

The task says: **"Submission: Git repository / zip + run instructions"**. So either works.
Follow the exact instructions in the email/portal you received; if they do not say, do this:

**Option 1 - GitHub link (recommended):** send the repository link. If it is Private, open
the repo -> *Settings* -> *Collaborators* -> *Add people* and add the reviewer's GitHub name
or email, or switch it to Public if they allow that.

**Option 2 - Zip:** make a copy of the project folder, **delete the `.venv` folder from the copy**
(it is huge and not needed), right-click the copy -> *Send to* -> *Compressed (zipped) folder*,
and attach that zip.

**Either way, include run instructions.** They are already in `README.md` and this file. In your
message, paste the short version:
```
Requirements: Python 3.12+, Docker (PostgreSQL 16 + PostGIS 3.4 via docker compose)
1) docker compose up -d
2) python -m venv .venv  &  .venv\Scripts\activate  &  pip install -e ".[test]"
3) pytest            -> 33 passed
4) iris-promote demo -> replays insert / rerun / correction / reject scenarios
See README.md for design decisions, assumptions and production notes.
```

### Last check before sending (important - the task says it must run from a clean checkout)
1. Make a NEW empty folder, e.g. `C:\test`. In CMD: `cd C:\test`
2. `git clone https://github.com/YOURNAME/iris-cand-07-upsert.git` (or unzip your zip there)
3. Follow Part B again from B3 inside that folder. If `pytest` says `33 passed`, you are done.
4. Read `README.md` once so you can explain your choices (key, freshness rule, rejects) in an interview.
