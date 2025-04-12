# Blawby Gmail Agent

A Gmail Add-On and Cloudflare-powered AI backend that helps lawyers manage their email more efficiently.

## Project Structure

- `gmail-addon/` - Google Workspace Add-On (Apps Script)
- `cloudflare-worker/` - Cloudflare Workers backend (TypeScript)
- `prd.md` - Product Requirements Document

## Features

- 🏷️ Smart Email Labeling
- 🪄 AI Reply Generation
- ✍️ Voice Profile Builder
- 📂 Client/Matter Intelligence
- 💰 Billing Tracker
- ✂️ Email Snippets Library
- 🧠 Whisper-mode QA Tool

## Development Setup

### Cloudflare Worker

1. Install dependencies:
   ```
   cd cloudflare-worker
   npm install
   ```

2. Set up Cloudflare secrets:
   ```
   npx wrangler secret put OPENAI_API_KEY
   npx wrangler secret put GOOGLE_CLIENT_ID
   npx wrangler secret put GOOGLE_CLIENT_SECRET
   ```

3. Create KV namespace for OAuth tokens:
   ```
   npx wrangler kv:namespace create OAUTH_TOKENS
   ```
   Then update the ID in `wrangler.toml`

4. Local development:
   ```
   npm run dev
   ```

5. Deploy to Cloudflare:
   ```
   npm run deploy
   ```

### Gmail Add-On

1. Create a new Google Apps Script project
2. Copy the files from `gmail-addon/src` to your project
3. Update the `API_BASE_URL` to point to your deployed Cloudflare Worker
4. Deploy as a Google Workspace Add-On

## Setup Google Cloud Project

1. Create a new project in Google Cloud Console
2. Enable the Gmail API
3. Configure OAuth consent screen
4. Create OAuth credentials (Web application)
5. Add authorized redirect URIs:
   - `https://agent.blawby.com/auth/callback`

## Environment Variables

The following environment variables need to be set in Cloudflare:

- `OPENAI_API_KEY` - OpenAI API key
- `GOOGLE_CLIENT_ID` - Google OAuth client ID
- `GOOGLE_CLIENT_SECRET` - Google OAuth client secret 
- `JWT_SECRET` - Secret for JWT token signing

## License

Copyright (c) 2023 Blawby - All Rights Reserved 