---

# 📄 Full Product Requirements Document (PRD)  
## Product Name: **Blawby Gmail Agent**  
**Domain**: `agent.blawby.com`  
**Type**: Gmail Add-On + Cloudflare-Powered AI Backend  
**Audience**: Lawyers & Legal Teams  
**Version**: v1.0  
**Owner**: Blawby (Legal SaaS Platform)
**Status Update**: API Layer Complete, Vectorize Integration In Progress ✅

---

## 🧩 Problem Statement  
Lawyers spend a large portion of their time manually managing Gmail: triaging high-priority emails, replying to client threads, tracking deadlines, and logging billable hours. This work is tedious, inconsistent, and untracked — yet critical.  
They need an assistant that works *within Gmail*, understands *legal tone and workflow*, and *helps them act faster and smarter*.

---

## 🎯 Product Goals  
Build a secure, Gmail-native AI assistant that:
- ✅ Applies legal-specific labels to emails
- ✅ Auto-generates high-quality draft replies in the lawyer's voice
- 🔄 Identifies clients/matters (Partially implemented)
- 🔄 Tracks time for billing (UI ready, backend storage pending)
- ✅ Offers semantic memory of prior communication
- ✅ Improves efficiency and consistency — without leaving Gmail

---

## 🛠️ Tech Stack Overview

| Layer | Tool / Service | Status |
|-------|----------------|--------|
| Gmail Add-On | Google Workspace Add-On (Apps Script) | ✅ Initial Implementation |
| Backend Logic | Cloudflare Workers (TypeScript) | ✅ Complete |
| Data Pipelines | Cloudflare Cron Triggers | ✅ Configured |
| Embeddings + Memory | Cloudflare Vectorize | 🔄 In Progress |
| Token Storage | Cloudflare KV | ✅ Complete |
| Logging/Analytics | Cloudflare D1 (or KV) | ✅ Basic Implementation |
| AI Model | OpenAI (GPT-4, `text-embedding-ada-002`) | ✅ Integrated |
| Dev Tools | Wrangler, Vite, GitHub Actions | ✅ Complete |
| API Layer | Hono Router, TypeScript | ✅ Complete |

---

## 🧠 Core Features

### 1. 🔐 Gmail OAuth & Permissions
- ✅ Uses `gmail.readonly`, `gmail.modify` scopes
- ✅ Initiated from `agent.blawby.com`
- ✅ Tokens stored securely in KV
- ✅ Required for all downstream features

---

### 2. 🏷️ Smart Email Labeling
- ✅ Daily Gmail scan via Cron (last 24h)
- ✅ Sends message text to OpenAI for classification
- ✅ Applies labels via Gmail API:

| Label | Trigger | Status |
|-------|---------|--------|
| ⚖️ Client Action | Request from or about a client | ✅ Working |
| 📅 Time Sensitive | Deadlines, dates, urgency | ✅ Working |
| 📞 Follow-Up | You owe a reply | ✅ Working |
| 📁 New Matter | Intake or business development | ✅ Working |
| 🧾 Billing Related | Invoices, retainers, fee language | ✅ Working |
| ✨ AI Draft Ready | AI has generated a reply | 🔄 Pending |

---

### 3. 🪄 AI Reply Generation (Sidebar Only)
- ✅ "Generate Reply" button in Gmail Add-On sidebar
- ✅ Worker pulls:
  - Current thread
  - Voice Profile
  - Relevant past messages from Vectorize
- ✅ Prompt sent to OpenAI
- ✅ Displays editable draft in sidebar
- ✅ Lawyer manually inserts (never auto-sends)
- ✅ API endpoints for email/thread processing
- ✅ Local development and testing environment

---

### 4. ✍️ Voice Profile Builder (Onboarding)
- ✅ After OAuth, system fetches 500–1,000 sent emails
- ✅ Chunks + embeds messages
- ✅ Uses OpenAI to infer:
  - Tone (formal, concise, casual)
  - Structure (bullets, paragraphs)
  - Phrases and closings
- ✅ Voice Profile stored in KV/D1
- 🔄 User confirms or adjusts in sidebar (UI pending)

---

### 5. 📂 Client or Matter Intelligence
- 🔄 Extracts named entities from threads (e.g., "Smith v. Jones")
- 🔄 Tags threads with inferred client/matter
- 🔄 Used for:
  - Time tracking
  - Filtering
  - Semantic recall
- 🔄 Metadata stored alongside Vectorize entries (pending)

---

### 6. 💰 Billing Tracker
- ✅ After AI reply generated, sidebar prompts:
  - "Log 0.2 hrs for Smith LLP – Lease Review"
- 🔄 Tracks:
  - Time estimate
  - Client/matter
  - Message subject/date
- 🔄 Stored in D1, exportable for billing software (CSV or API) (pending)

---

### 7. ✂️ Email Snippets Library
- 🔄 Extracts frequent sentence structures from sent email history
- 🔄 Examples:
  - "Please find attached..."
  - "Let me know if you have any questions..."
- 🔄 Stored per user
- 🔄 Used dynamically in LLM reply construction (pending)

---

### 8. 🧠 Whisper-mode QA Tool
- 🔄 Before generating new reply, sidebar shows:
  - "Previously you replied like this..."
  - Pulls 1–2 similar replies from Vectorize
- 🔄 Builds confidence and gives lawyer context (pending)

---

### 9. 📊 Analytics Layer (Internal MVP)
- 🔄 Track:
  - Which labels are applied and how often
  - Which replies are inserted, edited, or ignored
  - Time saved (est. per reply)
- 🔄 Optional dashboard in future
- 🔄 Used to tune performance, quality, and user engagement (pending)

---

## 🛡️ Security & Privacy

| Concern | Approach | Status |
|--------|----------|--------|
| Gmail Access | OAuth 2.0, scoped permissions | ✅ Complete |
| Token Security | Encrypted in KV | ✅ Complete |
| Data Ownership | Full delete support | ✅ Complete |
| AI Replies | Never sent automatically | ✅ Complete |
| Admin Access | Cloudflare Zero Trust or scoped admin panel | 🔄 Pending |
| Email Indexing | Vector embeddings only, not raw content | 🔄 Planned |

---

## 🧮 AI Prompt Architecture

### Classification Prompt (Labeling)
```
Given this email, assign one or more labels from:
Client Action, Time Sensitive, Follow-Up, Billing Related, New Matter.
If none apply, respond: No Label.
```
✅ Implemented and working

### Voice Profile Summary
```
Analyze the tone, structure, and key phrases from these 50 emails. Summarize the user's legal communication style in ~5 bullet points.
```
✅ Implemented and working

### Reply Generation Prompt
```
Given the email thread, voice profile, and prior similar messages, write a reply in the lawyer's voice.

Include key facts, tone, and any recurring phrases.

[Thread Summary]
[Voice Profile]
[Relevant Past Replies]
```
✅ Partially implemented (missing Vectorize integration for past replies)

---

## 📅 Project Timeline (v1.0)

| Week | Deliverable | Status |
|------|-------------|--------|
| Week 1 | OAuth flow + Gmail Add-On shell | ✅ Complete |
| Week 2 | Email fetch + label classifier | ✅ Complete |
| Week 3 | Voice Profile builder | ✅ Complete |
| Week 4 | Cloudflare Vectorize integration | 🔄 In Progress |
| Week 5 | Sidebar AI reply + Whisper QA | ✅ API Ready, UI Integration Pending |
| Week 6 | Client tagging + billing tracker | 🔄 Pending |
| Week 7 | Analytics, logging, polish | ✅ Basic Implementation |
| Week 8 | QA, Add-On submission, launch prep | 🔄 Pending |

---

## ✅ Success Criteria

- ✅ Lawyer installs Add-On and connects Gmail
- ✅ Labels appear accurately within 24h
- ✅ AI replies match their tone and context
- 🔄 Time tracking is intuitive and helpful (UI ready, backend pending)
- 🔄 Usage increases, edits go down over time (to be measured)
- 🔄 Feedback loop improves suggestions (to be implemented)

---

## 🔄 Implementation Recommendations

### 1. 📊 Gmail API Rate Limit Strategy
- ✅ Implement progressive batching with exponential backoff
- ✅ Start with smaller batches (50-100 emails) for initial voice profile
- ✅ Queue remaining processing during low-usage periods
- 🔄 Add status indicator showing "Profile building: 30%" in sidebar
- 🔄 Distribute processing over multiple sessions for high-volume users

### 2. ⚠️ Error Handling Framework
- ✅ Create graceful degradation system for service interruptions
- ✅ If classification fails, fall back to manual labeling
- ✅ For failed AI replies, cache context and retry once with different parameters
- ✅ Prompt user to try again later if unsuccessful after retry
- ✅ Log errors to D1 for monitoring and improvement

### 3. 👍 AI Reply Feedback System
- 🔄 Add thumbs up/down after inserting AI replies
- 🔄 Include optional comment field for specific feedback
- 🔄 Store ratings in Cloudflare D1 with prompt/response pairs
- 🔄 Use feedback to fine-tune future responses for that specific user
- 🔄 Aggregate feedback trends for model improvements

### 4. 🗄️ Data Retention Policy
- 🔄 Implement 90-day rolling window for raw email content
- 🔄 Store only vector embeddings long-term
- 🔄 Provide configurable retention settings
- ✅ Add explicit purge options for compliance with legal requirements
- 🔄 Include data export functionality for user ownership

### 5. 🛠️ Practice-Specific Customization
- 🔄 Add configuration section for practice-specific terminology
- 🔄 Enable custom label creation based on practice area
- 🔄 Provide domain-specific templates (litigation vs. corporate)
- 🔄 Allow import/export of settings for team standardization
- 🔄 Support firm-wide terminology glossaries

---

## 🛠 Dev Infra Ready
- ✅ Wrangler (Cloudflare Workers)
- ✅ Vite (local tooling + Apps Script bundling)
- ✅ GitHub Actions (CI/CD for Workers + Gmail Add-On push)
- 🔄 Cloudflare Zero Trust (if needed for staging/admin access)

---

## 📋 Key Learnings & Observations

Based on initial implementation:

1. ✅ **Cloudflare KV Performance**: Excellent for token storage but will need to monitor quotas with scale.

2. ✅ **OAuth Flow**: Successfully implemented two-stage authentication (Google OAuth + JWT) for secure API access.

3. ✅ **OpenAI Classification**: Achieved high accuracy for legal label classification with minimal tuning.

4. ✅ **Voice Profile Generation**: Works well with ~50 emails; performance degrades with too many or too few samples.

5. ✅ **Error Handling**: Implemented robust error handling with structured logging and debugging capabilities.

6. ✅ **Testing Infrastructure**: CI/CD pipelines and daily health checks ensure system stability.

7. ✅ **API Layer**: Successfully implemented and tested email/thread processing endpoints with:
   - Proper request validation
   - Structured error responses
   - Debug logging
   - Reasonable response times (1-2s for email, ~12s for thread processing)

8. ✅ **Local Development**: Wrangler dev environment provides:
   - Simulated KV storage
   - Environment variable management
   - Real-time logging
   - Hot reloading
   - Easy testing setup

9. 🔄 **Performance Optimization**: Initial observations:
   - Thread processing takes longer than single email processing
   - OpenAI API response times are consistent
   - Debug logging adds minimal overhead
   - KV operations are fast and reliable

---

## 🚀 Next Steps

### Immediate Priorities

1. **Gmail Add-On Deployment**:
   - Complete the Apps Script deployment pipeline
   - Submit for Google Workspace Marketplace review
   - Create onboarding guide for users

2. **Vectorize Integration**:
   - Set up Cloudflare Vectorize index
   - Implement document embedding pipeline
   - Build semantic search for Whisper-mode

3. **Client/Matter Detection**:
   - Enhance entity extraction for legal documents
   - Implement pattern recognition for matter references
   - Build storage model for client/matter relationships

### Technical Implementation Plan

1. Complete week 4-8 deliverables from timeline
2. Perform large-scale testing with production-level email volumes
3. Establish monitoring and alerting for production environment
4. Create admin dashboard for usage analytics

---