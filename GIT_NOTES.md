# Git notes

A running log of the Git steps on this project: the situation, what was done in plain English, and the commands.
Each Git idea is explained the first time it appears.

## 19 Nov 2026: the repo existed only on the laptop

**Situation.** The folder had one commit (GitHub's template files) but no link to GitHub, so GitHub Desktop
showed "Publish repository". A private key file had appeared in the folder and was ticked for commit.

**What was done.**
1. The key file was added to `.gitignore` and deleted, and the key itself was deleted in Google Cloud
   (a deleted key is useless even if a copy exists somewhere).
2. More ignore rules were added for secrets and private files.

**Ideas.**
- **Commit:** a saved snapshot of the files, with a message saying what changed.
- **`.gitignore`:** a list of file patterns Git pretends do not exist, so they can never be committed.
  `*` means "anything", so `n8n-jobfit-*.json` covers every file starting with `n8n-jobfit-` and ending in `.json`.
- **Remote:** the address of the copy on GitHub. A repo without a remote lives only on your laptop.
  `git remote -v` lists the remotes; an empty answer means there is none.
- **Push:** send your new commits to the remote. **Publish** in GitHub Desktop creates a new GitHub repo and pushes,
  so it is wrong when the GitHub repo already exists; then you add the remote and push instead.

**Commands (the same steps typed in a terminal instead of GitHub Desktop).**
```
git status                     # what changed since the last commit
git remote -v                  # which GitHub address this repo is linked to (none yet)
git remote add origin https://github.com/aadhilshameed/job-fit-workflow.git
                               # "origin" is the usual name for the main remote
git add .gitignore             # put the file into the next snapshot ("staging")
git commit -m "Ignore secret and private files"
git push -u origin main        # send it; -u remembers origin/main, so later a plain "git push" is enough
```
