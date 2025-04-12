import { Hono } from 'hono';
import { cors } from 'hono/cors';
import { jwt } from 'hono/jwt';
import { OpenAIService } from './services/openai';
import { GmailService } from './services/gmail';
import { OAuthService } from './services/oauth';

// Define environment bindings
interface Env {
  OAUTH_TOKENS: KVNamespace;
  OPENAI_API_KEY?: string;
  GOOGLE_CLIENT_ID?: string;
  GOOGLE_CLIENT_SECRET?: string;
  JWT_SECRET?: string;
  DEBUG_MODE?: string; // Add debug mode flag
}

// Define interfaces
interface EmailContent {
  subject: string;
  body: string;
  from: string;
  to?: string;
  date?: string;
}

interface EmailThread {
  id: string;
  messages: EmailContent[];
}

// Create Hono app
const app = new Hono<{ Bindings: Env }>();

// Helper to initialize services
function initializeServices(env: Env, userId: string) {
  const redirectUri = 'https://blawby-gmail-agent.paulchrisluke.workers.dev/auth/callback';
  
  const oauthService = new OAuthService(
    env.GOOGLE_CLIENT_ID || '',
    env.GOOGLE_CLIENT_SECRET || '',
    redirectUri,
    env.OAUTH_TOKENS
  );
  
  const openaiService = new OpenAIService(env.OPENAI_API_KEY || '');
  
  return { oauthService, openaiService };
}

// Helper for structured logging
function logDebug(env: Env, message: string, data?: any) {
  if (env.DEBUG_MODE === 'true') {
    console.log(`[DEBUG] ${message}`, data ? JSON.stringify(data) : '');
  }
}

function logError(env: Env, message: string, error: any) {
  console.error(`[ERROR] ${message}`, error);
  if (env.DEBUG_MODE === 'true' && error instanceof Error) {
    console.error(`[ERROR_STACK] ${error.stack}`);
  }
}

// Apply CORS middleware
app.use('*', cors({
  origin: '*', // For testing, allow all origins
  allowHeaders: ['Authorization', 'Content-Type'],
  allowMethods: ['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
  maxAge: 86400,
}));

// Define routes
app.get('/', (c) => {
  return c.json({
    name: 'Blawby Gmail Agent API',
    version: '1.0.0',
    status: 'operational'
  });
});

// OAuth routes
const auth = new Hono<{ Bindings: Env }>();

auth.get('/authorize', async (c) => {
  // Google OAuth authorization URL builder
  const clientId = c.env.GOOGLE_CLIENT_ID || '';
  const redirectUri = 'https://blawby-gmail-agent.paulchrisluke.workers.dev/auth/callback';
  const scope = encodeURIComponent('https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.modify');
  
  const authUrl = `https://accounts.google.com/o/oauth2/auth?client_id=${clientId}&redirect_uri=${redirectUri}&response_type=code&scope=${scope}&access_type=offline&prompt=consent`;
  
  return c.redirect(authUrl);
});

auth.get('/callback', async (c) => {
  // Handle OAuth callback from Google
  const code = c.req.query('code');
  
  if (!code) {
    return c.json({ error: 'Authorization code not provided' }, 400);
  }
  
  try {
    // Exchange code for tokens
    const { oauthService } = initializeServices(c.env, 'temp-user-id');
    const tokens = await oauthService.exchangeCodeForTokens(code);
    
    // Generate a userId (in a real app, this would be tied to the user's account)
    // For demo, we'll use a hash of the refresh token
    const userId = `user-${btoa(tokens.refreshToken).slice(0, 10)}`;
    
    // Store tokens
    await oauthService.storeTokens(userId, tokens);
    
    // Generate JWT for API authentication
    const jwtToken = await generateJWT(c.env, userId);
    
    // For testing, just return the token instead of redirecting
    return c.json({ 
      success: true, 
      token: jwtToken,
      userId: userId,
      message: "Authentication successful! Use this token for API calls."
    });
  } catch (error) {
    console.error('OAuth error:', error);
    return c.json({ error: 'Failed to complete OAuth flow' }, 500);
  }
});

auth.get('/logout', async (c) => {
  const authHeader = c.req.header('Authorization');
  if (!authHeader) {
    return c.json({ error: 'Unauthorized' }, 401);
  }
  
  const token = authHeader.replace('Bearer ', '');
  
  try {
    // Verify JWT and get userId
    const payload = await verifyJWT(c.env, token);
    const userId = payload.sub;
    
    // Revoke tokens
    const { oauthService } = initializeServices(c.env, userId);
    await oauthService.revokeTokens(userId);
    
    return c.json({ success: true });
  } catch (error) {
    return c.json({ error: 'Failed to logout' }, 500);
  }
});

// Gmail API interaction routes
const gmail = new Hono<{ Bindings: Env }>();

// Protected by auth middleware
gmail.use('*', async (c, next) => {
  const authHeader = c.req.header('Authorization');
  if (!authHeader) {
    return c.json({ error: 'Unauthorized' }, 401);
  }
  
  const token = authHeader.replace('Bearer ', '');
  
  try {
    // Verify JWT
    const payload = await verifyJWT(c.env, token);
    c.set('userId', payload.sub);
    await next();
  } catch (error) {
    return c.json({ error: 'Invalid token' }, 401);
  }
});

gmail.post('/classify', async (c) => {
  const userId = c.get('userId');
  const body = await c.req.json();
  
  // Validate request
  if (!body.email || !body.email.subject || !body.email.body) {
    return c.json({ error: 'Invalid request: email data required' }, 400);
  }
  
  try {
    logDebug(c.env, 'Classifying email', { userId, emailSubject: body.email.subject });
    
    // Initialize services
    const { oauthService, openaiService } = initializeServices(c.env, userId);
    
    // Classify the email
    const email: EmailContent = body.email;
    const classification = await openaiService.classifyEmail(email);
    
    logDebug(c.env, 'Email classified successfully', { 
      userId, 
      labels: classification.labels,
      confidence: classification.confidence 
    });
    
    return c.json(classification);
  } catch (error) {
    logError(c.env, 'Classification error', error);
    
    // Determine if this is an OpenAI API error
    const errorMessage = error instanceof Error ? error.message : 'Unknown error';
    const statusCode = errorMessage.includes('OpenAI API') ? 503 : 500;
    
    return c.json({ 
      error: 'Failed to classify email', 
      message: c.env.DEBUG_MODE === 'true' ? errorMessage : undefined 
    }, statusCode);
  }
});

gmail.post('/generate-reply', async (c) => {
  const userId = c.get('userId');
  const body = await c.req.json();
  
  // Validate request
  if (!body.threadId) {
    return c.json({ error: 'Invalid request: threadId required' }, 400);
  }
  
  try {
    logDebug(c.env, 'Generating reply', { userId, threadId: body.threadId });
    
    // Initialize services
    const { oauthService, openaiService } = initializeServices(c.env, userId);
    
    // Get access token
    const accessToken = await oauthService.getValidAccessToken(userId);
    if (!accessToken) {
      logError(c.env, 'Failed to get access token', { userId });
      return c.json({ error: 'Failed to get access token' }, 401);
    }
    
    // Create Gmail service
    const gmailService = new GmailService(accessToken);
    
    // Get thread
    const thread = await gmailService.getThread(body.threadId);
    
    // TODO: Fetch voice profile from KV
    // For now, use a placeholder
    const voiceProfile = "• Formal and concise writing style\n• Uses clear paragraph structure with direct statements\n• Frequently references legal precedent and statutory provisions\n• Closes emails with 'Best regards' followed by name\n• Occasionally uses bulleted lists for multi-part requests";
    
    // Generate reply
    const replyData = await openaiService.generateReply(thread, voiceProfile);
    
    logDebug(c.env, 'Reply generated successfully', { 
      userId, 
      threadId: body.threadId,
      timeEstimate: replyData.timeEstimate 
    });
    
    return c.json(replyData);
  } catch (error) {
    logError(c.env, 'Reply generation error', error);
    
    // Determine error type and appropriate response
    let statusCode = 500;
    let errorMessage = 'Failed to generate reply';
    
    if (error instanceof Error) {
      if (error.message.includes('OpenAI API')) {
        statusCode = 503;
        errorMessage = 'AI service unavailable';
      } else if (error.message.includes('Failed to fetch thread')) {
        statusCode = 404;
        errorMessage = 'Thread not found';
      } else if (error.message.includes('token')) {
        statusCode = 401;
        errorMessage = 'Authentication error';
      }
    }
    
    return c.json({ 
      error: errorMessage, 
      details: c.env.DEBUG_MODE === 'true' ? error instanceof Error ? error.message : String(error) : undefined 
    }, statusCode);
  }
});

gmail.post('/create-voice-profile', async (c) => {
  const userId = c.get('userId');
  
  try {
    // Initialize services
    const { oauthService, openaiService } = initializeServices(c.env, userId);
    
    // Get access token
    const accessToken = await oauthService.getValidAccessToken(userId);
    if (!accessToken) {
      return c.json({ error: 'Failed to get access token' }, 401);
    }
    
    // Create Gmail service
    const gmailService = new GmailService(accessToken);
    
    // Get sent emails
    const sentEmails = await gmailService.getSentMessages(50);
    
    if (sentEmails.length === 0) {
      return c.json({ error: 'No sent emails found to create voice profile' }, 400);
    }
    
    // Create voice profile
    const voiceProfile = await openaiService.createVoiceProfile(sentEmails);
    
    // TODO: Store voice profile in KV
    // For now, just return it
    
    return c.json({ voiceProfile });
  } catch (error) {
    console.error('Voice profile creation error:', error);
    return c.json({ error: 'Failed to create voice profile' }, 500);
  }
});

gmail.post('/apply-labels', async (c) => {
  const userId = c.get('userId');
  const body = await c.req.json();
  
  // Validate request
  if (!body.threadId || !body.labels || !Array.isArray(body.labels)) {
    return c.json({ error: 'Invalid request: threadId and labels array required' }, 400);
  }
  
  try {
    // Initialize services
    const { oauthService } = initializeServices(c.env, userId);
    
    // Get access token
    const accessToken = await oauthService.getValidAccessToken(userId);
    if (!accessToken) {
      return c.json({ error: 'Failed to get access token' }, 401);
    }
    
    // Create Gmail service
    const gmailService = new GmailService(accessToken);
    
    // Apply labels
    await gmailService.applyLabels(body.threadId, body.labels);
    
    return c.json({ success: true });
  } catch (error) {
    console.error('Label application error:', error);
    return c.json({ error: 'Failed to apply labels' }, 500);
  }
});

// Add the missing API endpoints
const api = new Hono<{ Bindings: Env }>();

api.post('/process-email', async (c) => {
  const body = await c.req.json();
  
  // Validate request
  if (!body.user || !body.email) {
    return c.json({ error: 'Invalid request: user and email data required' }, 400);
  }
  
  try {
    logDebug(c.env, 'Processing email', { 
      userId: body.user.id, 
      emailId: body.email.id 
    });
    
    // Initialize services
    const { openaiService } = initializeServices(c.env, body.user.id);
    
    // Convert to expected format
    const emailContent: EmailContent = {
      subject: body.email.subject,
      body: body.email.body,
      from: body.email.sender,
      date: body.email.receivedAt
    };
    
    // Classify the email
    const classification = await openaiService.classifyEmail(emailContent);
    
    return c.json({
      success: true,
      classification: classification,
      emailId: body.email.id
    });
  } catch (error) {
    logError(c.env, 'Email processing error', error);
    return c.json({ 
      error: 'Failed to process email', 
      message: c.env.DEBUG_MODE === 'true' ? error instanceof Error ? error.message : String(error) : undefined 
    }, 500);
  }
});

api.post('/process-thread', async (c) => {
  const body = await c.req.json();
  
  // Validate request
  if (!body.user || !body.emails || !body.threadId) {
    return c.json({ error: 'Invalid request: user, emails, and threadId required' }, 400);
  }
  
  try {
    logDebug(c.env, 'Processing thread', { 
      userId: body.user.id, 
      threadId: body.threadId,
      emailCount: body.emails.length 
    });
    
    // Initialize services
    const { openaiService } = initializeServices(c.env, body.user.id);
    
    // Convert to expected format
    const thread: EmailThread = {
      id: body.threadId,
      messages: body.emails.map((email: any) => ({
        subject: email.subject,
        body: email.body,
        from: email.sender,
        date: email.receivedAt
      }))
    };
    
    // TODO: Fetch voice profile from KV
    // For now, use a placeholder
    const voiceProfile = "• Formal and concise writing style\n• Uses clear paragraph structure with direct statements\n• Frequently references legal precedent and statutory provisions\n• Closes emails with 'Best regards' followed by name\n• Occasionally uses bulleted lists for multi-part requests";
    
    // Generate reply
    const replyData = await openaiService.generateReply(thread, voiceProfile);
    
    return c.json({
      success: true,
      reply: replyData,
      threadId: body.threadId
    });
  } catch (error) {
    logError(c.env, 'Thread processing error', error);
    return c.json({ 
      error: 'Failed to process thread', 
      message: c.env.DEBUG_MODE === 'true' ? error instanceof Error ? error.message : String(error) : undefined 
    }, 500);
  }
});

// API routes
app.route('/auth', auth);
app.route('/gmail', gmail);
app.route('/api', api);

// Helper functions
async function generateJWT(env: Env, userId: string): Promise<string> {
  // In a real app, use a proper JWT library
  // For demo, we're using Hono's JWT utility
  const payload = {
    sub: userId,
    iat: Math.floor(Date.now() / 1000),
    exp: Math.floor(Date.now() / 1000) + (7 * 24 * 60 * 60), // 7 days
  };
  
  const secret = env.JWT_SECRET || 'default-secret-change-in-production';
  
  const encoder = new TextEncoder();
  const secretKey = encoder.encode(secret);
  
  const header = { alg: 'HS256', typ: 'JWT' };
  
  const encodedHeader = btoa(JSON.stringify(header));
  const encodedPayload = btoa(JSON.stringify(payload));
  
  const data = `${encodedHeader}.${encodedPayload}`;
  
  const key = await crypto.subtle.importKey(
    'raw',
    secretKey,
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign']
  );
  
  const signature = await crypto.subtle.sign('HMAC', key, encoder.encode(data));
  
  const encodedSignature = btoa(String.fromCharCode(...new Uint8Array(signature)));
  
  return `${encodedHeader}.${encodedPayload}.${encodedSignature}`;
}

async function verifyJWT(env: Env, token: string): Promise<{ sub: string; exp: number }> {
  try {
    const secret = env.JWT_SECRET || 'default-secret-change-in-production';
    const [header, payload, signature] = token.split('.');
    
    // Decode payload
    const decodedPayload = JSON.parse(atob(payload));
    
    // Check expiration
    const now = Math.floor(Date.now() / 1000);
    if (decodedPayload.exp && decodedPayload.exp < now) {
      throw new Error('Token expired');
    }
    
    // In a real app, verify the signature
    // For demo purposes, we're skipping actual verification
    
    return decodedPayload;
  } catch (error) {
    throw new Error('Invalid token');
  }
}

// Scheduled task handler
export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    return app.fetch(request, env, ctx);
  },
  
  async scheduled(event: ScheduledEvent, env: Env, ctx: ExecutionContext): Promise<void> {
    console.log("Running scheduled task to process emails");
    
    // TODO: Implement daily email processing logic
    // 1. Get all users
    // 2. For each user, get recent emails
    // 3. Classify emails and apply labels
    // 
    // This would involve:
    // - Getting user IDs from KV
    // - Getting access tokens
    // - Getting recent messages
    // - Classifying with OpenAI
    // - Applying labels
  }
}; 