# TODO Checklist

- [x] Google Drive Authentication
- [x] Upload images to Drive folder
- [x] Download images from Drive folder to temp directory
- [x] Resume support with cache.json
- [x] Image resizing (portrait/landscape rules)
- [x] Image compression & conversion to .webp
- [x] Alt text extraction and metadata storage
- [x] User confirmation for cleanup (delete originals)
- [x] CLI interface with all required flags
- [x] Support for Drive share link and folder ID
- [x] Output directory override
- [x] File type filtering by extension
- [x] Conflict handling (skip/overwrite/versioned)
- [x] Robustness: error handling, retries, fail log
- [x] Validation before deletion
- [x] OAuth re-authentication support
- [x] Optional config file support
- [x] Dry run and test mode
- [x] README usage clarity and examples

Absolutely — here's the **fully updated project requirement sheet**, now including **image resizing** before conversion:

---

## 📝 Project Requirement Sheet: Google Drive Image Optimizer

### **Project Name**

`google-drive-image-optimizer`

---

### **Objective**

Build a locally run Python script to:

1. Authenticate with Google Drive using OAuth 2.0.
2. Upload images to a specific Drive folder.
3. Download images for processing with resume support.
4. Resize, compress, and convert images to `.webp`.
5. Extract filenames as alt text and save metadata.
6. Prompt user to delete originals after optimization.

---

### **Core Features**

#### 1. **Google Drive Authentication**

* Uses `credentials.json` from [Google Drive API Quickstart](https://developers.google.com/workspace/drive/api/quickstart/python).
* OAuth tokens are stored in `token.json`.
* Required scopes: `https://www.googleapis.com/auth/drive`.
* Libraries:

  * `google-auth`
  * `google-auth-oauthlib`
  * `google-auth-httplib2`
  * `google-api-python-client`

* Store in token.json

#### 2. **Upload Interface**

* Accept a local folder of images.
* Supported file types: `.jpg`, `.jpeg`, `.png`, `.bmp`, `.tiff`.
* Upload to a Drive folder via provided folder ID.
* Keep original filenames intact.

#### 2a. **Drive Folder Selection via Share Link**

* Accept either a Google Drive folder ID or a share link (e.g., https://drive.google.com/drive/folders/<FOLDER_ID>?usp=sharing) as input.
* Automatically extract the folder ID from a share link for all Drive API operations.
* Validate the folder ID and check for read/write permissions before proceeding.

#### 3. **Download & Caching**

* Download images from a specific Drive folder.
* Save to a local temp directory.
* Track progress in `cache.json`:

  * Images downloaded
  * Images processed
  * Unfinished jobs
* Resumable on script restart with `--resume` flag.

#### 4. **Image Resizing**

* Resize before compression/conversion:

  * **Portrait** (height > width): Resize to **1200px height × 900px width**
  * **Landscape** (width ≥ height): Resize to **900px height × 1200px width**
* Maintain aspect ratio with padding/cropping if necessary (basic resizing OK if stretch is acceptable).
* Use Pillow for resizing.

#### 5. **Image Compression & Conversion**

* Compress and convert to `.webp` using `Pillow` (WebP format).
* Target file size: under **300KB** (configurable).
* Save converted files to:

  * Local `optimized/` directory
  * Or re-upload to a separate Drive folder (optional `--upload-optimized` flag).

#### 6. **Alt Text Metadata**

* Extract from filename:

  * `golden-gate-bridge.jpg` → `alt="golden gate bridge"`
* Store in one of:

  * `alt_text_map.json`: `{ "filename.webp": "alt text here" }`
  * Sidecar files: `filename.alt.txt`
* (Optional) Embed in WebP metadata if supported by library (fallback to sidecar).

#### 7. **User Confirmation for Cleanup**

* After all files are processed:

  * Prompt:

    > "Optimization complete. Delete original files from Drive? \[y/N]"
* If yes:

  * Move to Drive trash or delete via API.

#### 8. **CLI Interface**

* Flags:

  * `--upload-dir [path]`
  * `--drive-folder-id [id]`
  * `--optimize`
  * `--resume`
  * `--upload-optimized`
  * `--cleanup`

---

### **Dependencies**

```bash
pip install --upgrade google-api-python-client google-auth-httplib2 google-auth-oauthlib Pillow tqdm
```

---

### **File Structure**

```
google-drive-image-optimizer/
│
├── main.py                    # CLI controller
├── auth.py                    # Google OAuth logic
├── drive_utils.py             # Drive upload/download/delete
├── image_processor.py         # Resize, compress, convert
├── cache.json                 # Resume checkpoint
├── alt_text_map.json          # Alt metadata
├── credentials.json           # OAuth client ID from Google Console
├── token.json                 # Stored after auth
├── optimized/                 # Final processed images
├── README.md
```

---

### **Stretch Goals (Optional)**

* GUI (Tkinter or PyQt)
* Upload optimized files to new Drive folder
* Slack/Email alerts
* Image deduplication
* Logging to file

---

### **Additional Considerations**

* **Error Handling:** Gracefully handle API errors, missing permissions, and invalid folder IDs/links.
* **Logging:** Add optional logging for debugging and audit trails.
* **Configurable Output Directory:** Allow user to specify where optimized images are saved locally.
* **File Type Filtering:** Ensure only supported image types are processed, skip others.
* **Progress Reporting:** Show progress bars or status updates for uploads/downloads/processing.
* **Dry Run Mode:** Optionally preview actions (e.g., which files would be processed/deleted) without making changes.
* **Cross-Platform Support:** Ensure compatibility with Windows, macOS, and Linux.
* **Unit Tests:** Include tests for core logic (especially image processing and Drive interactions).

---

### **Additional Best Practices & Features**

#### 1. Folder & File Organization Logic
- Use a dedicated temp directory (e.g., `temp_download/`) for downloads. Clean up by default, but allow a `--keep-temp` flag.
- Default output for optimized images is `optimized/`, but allow override with `--output-dir`.

#### 2. Input Image Discovery
- Only process files with supported extensions (`.jpg`, `.jpeg`, `.png`, `.bmp`, `.tiff`).
- No recursive subfolder traversal (all images must be in the specified folder).

#### 3. Conflict Handling
- Default: skip files that already exist in the output directory.
- Add `--overwrite` to force reprocessing.
- Optionally, add `--versioned` to save as `filename_v2.webp` if a conflict exists.

#### 4. Cache and Resume Control
- Use `cache.json` in the project root by default; allow `--cache-file` to specify a custom path.
- Store processed file IDs, timestamps, and Drive file ID → local filename mapping.

#### 5. Robustness / Recovery
- Log failures to `failures.log` or `failed_files.json`.
- Implement a retry mechanism (e.g., `--max-retries 3`).
- Skip files after max retries and log them.

#### 6. Validation Before Deletion
- After conversion/upload, verify the `.webp` file exists and (optionally) matches a checksum or file size threshold.
- Only queue originals for deletion if the optimized file is confirmed.

#### 7. OAuth Scoping / Multi-Account Support
- Add a `--reauth` flag to force a new OAuth flow (ignore/delete `token.json`).

#### 8. Optional Settings File
- Support a `config.json` or `.env` for persistent settings (folder IDs, output dir, etc.).
- CLI flags override config file values.

#### 9. Testing / Mocking Support
- Add a `--dry-run` flag to preview actions without making changes.
- Add a `--test-mode` to mock Drive calls and file operations.

#### 10. README / Usage Clarity
- Include example commands for all major actions.
- Document how to extract a folder ID from a share link.
- Add troubleshooting for auth issues (e.g., delete `token.json` to re-auth).

#### 11. Naming and Placement of New Images
- Save optimized images in the `optimized/` directory (or user-specified `--output-dir`).
- Use the original filename, changing only the extension to `.webp` (e.g., `myphoto.jpg` → `myphoto.webp`).
- If a conflict exists, follow the conflict handling logic (`--overwrite`, `--skip-existing`, or `--versioned`).

---

### **Summary: Additional Flags/Ideas**

| Flag/Setting      | Description |
|-------------------|-------------|
| --output-dir      | Custom directory for saving optimized images |
| --ext             | Comma-separated list of extensions to process |
| --overwrite       | Overwrite existing optimized files |
| --skip-existing   | Skip files that are already optimized |
| --fail-log        | Path to log failed conversions |
| --cache-file      | Custom path for cache file |
| --config          | Load settings from config.json |
| --reauth          | Force new Google account auth |
| --dry-run         | Preview what would be done without changes |
| --test-mode       | Skip real Drive calls, useful for local testing |
| --keep-temp       | Keep temp download directory after processing |
| --max-retries     | Number of times to retry failed operations |
| --versioned       | Save versioned filenames if conflicts |

---

Would you like me to generate the full project scaffold with file templates and boilerplate now for loading into Cursor?
