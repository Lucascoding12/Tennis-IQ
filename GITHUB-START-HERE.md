# Tennis IQ — GitHub upload and Render deployment

This repository contains the **original Python FastAPI backend and frontend together**. You do **not** need Netlify: the backend serves the website itself.

## 1. Upload these files to GitHub (browser-only)

1. Sign in to https://github.com/new.
2. Choose a repository name, such as `tennis-iq`. Choose **Private** unless you deliberately want to publish the source.
3. Do not select **Add a README**, **.gitignore** or **license** when creating the repository.
4. Create the repository, then select **uploading an existing file** (or **Add file > Upload files**).
5. **Extract this ZIP first.** Open the extracted `tennis-iq` folder, select all of its contents, then drag them onto the GitHub upload page. Keep the `backend/` and `frontend/` folder structure. If GitHub's browser upload does not preserve folders on your browser, use GitHub Desktop instead: https://desktop.github.com/.
6. Click **Commit changes**. Check that the GitHub repository root contains `render.yaml`, `Dockerfile`, `backend/`, and `frontend/` — there should NOT be an extra `tennis-iq/` folder wrapped around everything.

GitHub Desktop alternative: **File > Add local repository > Choose** the extracted `tennis-iq` folder; if it isn't a git repository, create a repository there, commit all source files, then **Publish repository** (choose Private if desired).

## 2. Deploy without Netlify

1. Open https://dashboard.render.com/ and sign in.
2. Choose **New > Blueprint**, connect your GitHub account, and select the `tennis-iq` repository.
3. Apply the included `render.yaml` blueprint. It creates one Docker web service with a 1 GB persistent disk. The included Render **Standard** plan is **paid**; check the current price in Render before confirming.
4. Render installs Python dependencies, starts FastAPI, and the application downloads ATP histories and trains models in the background. Open the service's public `.onrender.com` URL to use the full website.
5. Check `https://YOUR-RENDER-SERVICE.onrender.com/api/health`. The API's `ready` field should become `true` once initialization succeeds. If it stays false or reports an error, inspect **Render > Service > Logs**. ATP endpoints may change or block automated fetching; successful setup is not guaranteed.

For model administration, Render generates `TIQ_ADMIN_TOKEN`. Find it in the service's **Environment** tab. Do not commit or share the token.

## What is and isn't preloaded

This repository contains source code, **not** the ~170,000 downloaded matches, pre-trained models, or a verified current ATP dataset. The backend is designed to create those on first boot. They are stored on the persistent Render disk (`TIQ_DATA_DIR=/var/data`) and excluded from GitHub. The code is syntax-checked, but initial data ingestion and live deployment have not been verified here.

## Run locally

Windows: double-click `Start Tennis IQ (Windows).bat` after extracting. macOS: `Start Tennis IQ (Mac).command`; Linux: `sh start.sh`. Open http://localhost:8000.

## Source layout

- `backend/app.py`: FastAPI routes, data engine and frontend hosting
- `backend/atp.py`: ATP data fetching
- `backend/dataset.py`: match histories and Elo calculations
- `backend/lab.py`, `backend/points.py`, `backend/system.py`: predictions and screening
- `frontend/`: website
- `Dockerfile`, `render.yaml`: one-service cloud deployment

**Do not deploy only `frontend/` to Netlify.** That reproduces the blank-data problem.
