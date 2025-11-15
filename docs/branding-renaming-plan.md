# Quill Branding & Repo Renaming Plan

New brand
- **Name**: Quill
- **Tagline**: Create SEO Ranking blogs from YouTube. Ship fast, flexible, and SEO-optimized blogs with AI assist out of box. Quill brings the best of the LLM ecosystem.

This plan covers renaming the GitHub repo, updating local remotes, updating app titles/meta, adding favicons/manifest/SEO assets, and updating docs/config.

## Prereqs
- macOS shell. Node.js available (for optional `pwa-asset-generator`).
- GitHub CLI `gh` logged in (optional).

# TODOs
- **[repo-rename]** Rename GitHub repository to `quill` (or `quill-ai`) and update local remotes
- **[local-dir-rename]** Skip local folder rename (decision)
- **[templates-meta]** Update app name and SEO meta in `templates/base.html` and `templates/base_public.html`
- **[favicons]** Add favicon and web app icons under `static/favicon` and link them in base templates
- **[webmanifest]** Add `static/site.webmanifest` and link in templates
- **[readme]** Update `README.md` title, description, badges and links
- **[docs]** Update docs branding in `docs/*.md`
- **[pkgjson]** Update `package.json` name/description/repository/homepage
- **[deploy-config]** Review deploy config (`wrangler.toml`, etc.) for names
- **[robots-og]** Add `static/robots.txt` and Open Graph image for previews
- **[search-replace]** Search/replace any user-facing "App" defaults to "Quill"

# 1) GitHub repo rename
Option A: Web UI (recommended)
- Rename repo in GitHub Settings to `quill` (or `quill-ai`). GitHub will set a redirect.

Option B: GitHub CLI
```bash
# rename current repo (replace <OWNER>/<OLDNAME>)
gh repo rename quill --repo <OWNER>/<OLDNAME>

# or
# gh repo rename quill-ai --repo <OWNER>/<OLDNAME>
```

Update local remote
```bash
# show current remotes
git remote -v
# update origin to the new URL (HTTPS example)
git remote set-url origin https://github.com/<OWNER>/quill.git
# or
# git remote set-url origin git@github.com:<OWNER>/quill.git
```

# 2) Local directory rename (skipped)
```bash
# from one level above the project dir
mv convert-image-webp-optimizer-google-drive quill
```
Note: We are not renaming the local folder. If needed later, run the command above and update your IDE/venv pointers.

# 3) Update templates for title/meta and link favicons/manifest
Files
- `templates/base.html`
- `templates/base_public.html`

Set title default to Quill
```bash
# macOS sed inline replace
sed -i '' "s/{{ title or 'App' }}/{{ title or 'Quill' }}/g" templates/base.html
sed -i '' "s/{{ title or 'App' }}/{{ title or 'Quill' }}/g" templates/base_public.html
```

Add description and social meta (manual edit) under the `<head>` tag in both files, below `<meta name="viewport" ...>` and before scripts
```html
<meta name="description" content="Create SEO Ranking blogs from YouTube. Ship fast, flexible, and SEO-optimized blogs with AI assist out of box. Quill brings the best of the LLM ecosystem.">
<link rel="manifest" href="{{ url_for('static', path='site.webmanifest') }}">
<link rel="icon" href="{{ url_for('static', path='favicon/favicon-32x32.png') }}" sizes="32x32" type="image/png">
<link rel="icon" href="{{ url_for('static', path='favicon/favicon-16x16.png') }}" sizes="16x16" type="image/png">
<link rel="apple-touch-icon" href="{{ url_for('static', path='favicon/apple-touch-icon.png') }}" sizes="180x180">
<meta name="theme-color" content="#0f172a">

<!-- Open Graph -->
<meta property="og:title" content="Quill">
<meta property="og:description" content="Create SEO Ranking blogs from YouTube. Ship fast, flexible, and SEO-optimized blogs with AI assist out of box. Quill brings the best of the LLM ecosystem.">
<meta property="og:type" content="website">
<meta property="og:image" content="{{ url_for('static', path='og/og-image.png') }}">
<meta property="og:url" content="https://<your-domain>">
<meta property="og:site_name" content="Quill">

<!-- Twitter -->
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="Quill">
<meta name="twitter:description" content="Create SEO Ranking blogs from YouTube. Ship fast, flexible, and SEO-optimized blogs with AI assist out of box. Quill brings the best of the LLM ecosystem.">
<meta name="twitter:image" content="{{ url_for('static', path='og/og-image.png') }}">
```

# 4) Add favicon set and OG assets
Create folders
```bash
mkdir -p static/favicon static/og
```

Prepare source logo (SVG preferred)
- Place your logo as `assets/quill-logo.svg` (or `assets/quill-logo.png`).

Option A: Generate icons with pwa-asset-generator
```bash
# install (project local or global)
npx pwa-asset-generator assets/quill-logo.png static/favicon \
  --favicon --icon-only --opaque false --background "#0f172a" \
  --padding "10%" --manifest static/site.webmanifest \
  --mstile --apple \
  --path /static/favicon
```
This will generate favicon files and update/create a manifest. Verify output paths.

Option B: Manual assets
```bash
# create minimal favicons from a 512x512 png using ImageMagick (if installed)
# convert assets/quill-logo.png -resize 180x180 static/favicon/apple-touch-icon.png
# convert assets/quill-logo.png -resize 32x32  static/favicon/favicon-32x32.png
# convert assets/quill-logo.png -resize 16x16  static/favicon/favicon-16x16.png
```

Add an OG image placeholder (1200x630 recommended)
```bash
# copy or export to this path
# e.g., cp assets/og-image.png static/og/og-image.png
```

# 5) Add/Update Web App Manifest
Create `static/site.webmanifest` (if not created by generator):
```bash
cat > static/site.webmanifest << 'JSON'
{
  "name": "Quill",
  "short_name": "Quill",
  "description": "Create SEO Ranking blogs from YouTube. Ship fast, flexible, and SEO-optimized blogs with AI assist out of box. Quill brings the best of the LLM ecosystem.",
  "start_url": "/",
  "display": "standalone",
  "background_color": "#0f172a",
  "theme_color": "#0f172a",
  "icons": [
    {
      "src": "/static/favicon/favicon-192.png",
      "sizes": "192x192",
      "type": "image/png"
    },
    {
      "src": "/static/favicon/favicon-512.png",
      "sizes": "512x512",
      "type": "image/png"
    }
  ]
}
JSON
```

# 6) Robots.txt and sitemap placeholder
```bash
cat > static/robots.txt << 'TXT'
User-agent: *
Allow: /

Sitemap: https://<your-domain>/sitemap.xml
TXT
```

# 7) Package metadata
Edit `package.json`
- `name`: `"quill"`
- `description`: `"Create SEO Ranking blogs from YouTube..."`
- `repository.url`: `https://github.com/<OWNER>/quill.git`
- `homepage`: `https://<your-domain>`

Command-line (macOS sed examples; adjust as needed):
```bash
# name
sed -i '' 's/"name"\s*:\s*"[^"]*"/"name": "quill"/' package.json
# description
sed -i '' 's/"description"\s*:\s*"[^"]*"/"description": "Create SEO Ranking blogs from YouTube. Ship fast, flexible, and SEO-optimized blogs with AI assist out of box. Quill brings the best of the LLM ecosystem."/' package.json
```
Update repository/homepage fields manually if structure differs.

# 8) README and docs
- `README.md`: Update title to `# Quill` and replace old description with tagline.
- Update any badges/links to new repo URL and site URL.
- `docs/DEPLOYMENT.md`, `docs/feature-plan.md`, `docs/coding-rules.md`: replace old app name with Quill and update any URLs.

Helpful find commands
```bash
# list files referencing old repo/location
grep -R "convert-image-webp-optimizer-google-drive" -n . | head -n 50
# find user-facing default title occurrences
grep -R "title or 'App'" -n templates
```

# 9) Deployment config and environment
- `wrangler.toml`: review any `name`, `route`, or bucket binding names that include old branding; rename carefully.
- Any CI/CD settings or secrets referencing old repo path should be updated.

# 10) App-level search/replace for user-facing text
Be surgical; do not change code-level identifiers unless desired.
```bash
grep -R "\bApp\b" -n templates | grep -v node_modules
```
Manually ensure any visible UI titles or headings use "Quill".

# 11) Commit and push
```bash
git add -A
git commit -m "chore(brand): rename to Quill, add favicons/manifest/seo meta"
git push origin HEAD
```

# Verification checklist
- **Title** shows Quill on all pages.
- **Favicon** appears in browser tab.
- **OG/Twitter** preview image appears correctly.
- **Manifest** loads at `/static/site.webmanifest` and passes Lighthouse PWA checks.
- **Robots** available at `/static/robots.txt`.
- **Repo remote** points to `quill`.

# Rollback notes
- GitHub keeps redirects from old repo to new; verify before deleting old links.
- Keep old favicon filenames in place shortly if external references exist.
