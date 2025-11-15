# Quill Branding - Remaining Tasks

This document tracks the remaining tasks to complete the Quill rebranding.

## Completed ✅

- ✅ Favicon generation and setup
- ✅ Web manifest creation
- ✅ Robots.txt creation
- ✅ Template updates (base.html, base_public.html, sidebar, login)
- ✅ Package.json branding
- ✅ README.md branding
- ✅ User-facing "App" → "Quill" replacements
- ✅ Wrangler.toml naming conventions updated

## Remaining Tasks

### 1. Add Open Graph Image
- **Status**: Pending
- **Action**: Create or place a 1200x630 PNG image at `static/og/og-image.png`
- **Note**: This image will be used for social media previews (Twitter, Facebook, LinkedIn, etc.)

### 2. Update Domain Placeholders
- **Status**: Pending
- **Files to update**:
  - `templates/base.html` - Replace `<your-domain>` in OG meta tags (lines 18, 77)
  - `templates/base_public.html` - Replace `<your-domain>` in OG meta tags (lines 18, 77)
  - `static/robots.txt` - Replace `<your-domain>` in sitemap URL (line 3)
- **Action**: Replace all instances of `<your-domain>` with your actual production domain

### 3. GitHub Repository Rename
- **Status**: Pending
- **Action**: Rename the GitHub repository to `quill` (or `quill-ai`)
- **Steps**:
  1. Go to GitHub repository Settings → General → Repository name
  2. Rename to `quill` (or `quill-ai`)
  3. Update local remote URL:
     ```bash
     git remote set-url origin https://github.com/<OWNER>/quill.git
     # or
     git remote set-url origin git@github.com:<OWNER>/quill.git
     ```

### 4. Update Package.json Repository/Homepage
- **Status**: Pending
- **Action**: Update `package.json` with:
  - `repository.url`: `https://github.com/<OWNER>/quill.git`
  - `homepage`: `https://<your-domain>`

### 5. Cloudflare Resources Setup
- **Status**: Pending
- **Action**: When setting up Cloudflare resources, use the new naming conventions:
  ```bash
  # Create D1 database
  wrangler d1 create quill-db
  
  # Create queues
  wrangler queues create quill-queue
  wrangler queues create quill-dlq
  
  # Update wrangler.toml with the database_id after creation
  ```
- **Note**: `wrangler.toml` has already been updated with Quill naming conventions

### 6. Update DEPLOYMENT.md
- **Status**: Pending
- **Action**: Update `docs/DEPLOYMENT.md` to reflect:
  - New database name: `quill-db` (instead of `image-optimizer-db`)
  - New queue names: `quill-queue` and `quill-dlq`
  - New worker name: `quill-api`

### 7. Verification Checklist
- **Status**: Pending
- **Verify**:
  - [ ] Title shows "Quill" on all pages
  - [ ] Favicon appears in browser tab
  - [ ] OG/Twitter preview image appears correctly (after adding og-image.png)
  - [ ] Manifest loads at `/static/site.webmanifest`
  - [ ] Robots.txt available at `/static/robots.txt`
  - [ ] All domain placeholders replaced
  - [ ] GitHub repo renamed and remote updated
  - [ ] Cloudflare resources use Quill naming

## Notes

- The branding-renaming-plan.md has been deleted as all automated tasks are complete
- All code-level changes are complete
- Remaining tasks are primarily manual configuration and asset creation
- Domain placeholders should be updated before production deployment

