# Google Drive Image Optimizer

A Python tool that automatically downloads images from Google Drive, optimizes them to WebP format with SEO-friendly filenames, and uploads them back to the same folder while optionally deleting the originals.

## Features

- **Automatic Download**: Downloads images from any Google Drive folder
- **WebP Conversion**: Converts images to optimized WebP format
- **SEO Optimization**: Creates SEO-friendly filenames with folder-based prefixes
- **Smart Resizing**: Automatically resizes images to optimal dimensions (1200x900 for landscape, 900x1200 for portrait)
- **Compression**: Compresses images to under 300KB while maintaining quality
- **Batch Processing**: Handles multiple images efficiently
- **Duplicate Prevention**: Skips files that already exist in Drive
- **Automatic Cleanup**: Optionally deletes original images after optimization
- **Error Handling**: Comprehensive error logging and retry mechanisms

## Installation

1. Clone the repository:
```bash
git clone https://github.com/pcl-labs/convert-images-google-drive-to-seo-optimized-webp.git
cd convert-images-google-drive-to-seo-optimized-webp
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Set up Google Drive API:
   - Go to [Google Cloud Console](https://console.cloud.google.com/)
   - Create a new project or select existing one
   - Enable the Google Drive API
   - Create credentials (OAuth 2.0 Client ID)
   - Download the credentials and save as `credentials.json` in the project root

## Usage

### Basic Usage

```bash
python main.py --drive-folder "YOUR_GOOGLE_DRIVE_FOLDER_ID_OR_LINK"
```

### Advanced Options

```bash
python main.py \
  --drive-folder "https://drive.google.com/drive/folders/YOUR_FOLDER_ID" \
  --ext "jpg,jpeg,png,bmp,tiff,heic" \
  --overwrite \
  --cleanup
```

### Command Line Arguments

- `--drive-folder`: Google Drive folder ID or share link (required)
- `--ext`: Comma-separated list of image extensions to process (default: jpg,jpeg,png,bmp,tiff,heic)
- `--overwrite`: Overwrite existing optimized files
- `--skip-existing`: Skip files that are already optimized
- `--cleanup`: Automatically delete original images after optimization
- `--max-retries`: Number of retry attempts for failed operations (default: 3)
- `--versioned`: Save versioned filenames if conflicts occur
- `--dry-run`: Preview actions without making changes
- `--reauth`: Force new Google account authentication

## How It Works

1. **Authentication**: Uses OAuth 2.0 to authenticate with Google Drive API
2. **Download**: Downloads all images from the specified Drive folder to a temporary directory
3. **Processing**: For each image:
   - Resizes to optimal dimensions
   - Converts to WebP format
   - Compresses to under 300KB
   - Creates SEO-friendly filename with folder prefix
4. **Upload**: Uploads optimized images back to the same Drive folder
5. **Cleanup**: Optionally deletes original images and removes temporary files

## File Structure

```
├── main.py              # Main CLI entry point
├── drive_utils.py       # Google Drive API utilities
├── image_processor.py   # Image processing and optimization
├── requirements.txt     # Python dependencies
├── .gitignore          # Git ignore rules
└── README.md           # This file
```

## Output

- Optimized images are saved as WebP files with SEO-friendly names
- Original filenames are preserved in the alt text mapping
- Progress and error logs are displayed in the console
- Failed operations are logged to `failures.log`

## Security

- OAuth credentials (`credentials.json`) and tokens (`token.json`) are excluded from version control
- All sensitive files are listed in `.gitignore`
- No API keys or secrets are stored in the repository

## Requirements

- Python 3.7+
- Google Drive API access
- Internet connection for Drive API calls

## Dependencies

- `google-api-python-client`: Google Drive API client
- `google-auth-httplib2`: Google authentication
- `google-auth-oauthlib`: OAuth 2.0 authentication
- `Pillow`: Image processing
- `pillow-heif`: HEIC image support
- `tqdm`: Progress bars

## License

This project is open source and available under the MIT License.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## Support

For issues and questions, please open an issue on GitHub. 