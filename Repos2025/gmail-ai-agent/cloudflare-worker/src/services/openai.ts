import OpenAI from 'openai';

interface EmailContent {
  subject: string;
  body: string;
  from: string;
  to?: string;
  date?: string;
}

interface EmailThread {
  messages: EmailContent[];
}

interface ClassificationResult {
  labels: string[];
  confidence: Record<string, number>;
}

interface ReplyGeneration {
  reply: string;
  timeEstimate: number;
}

export class OpenAIService {
  private client: OpenAI;

  constructor(apiKey: string) {
    this.client = new OpenAI({
      apiKey: apiKey,
    });
  }

  /**
   * Classifies an email with appropriate labels
   */
  async classifyEmail(email: EmailContent): Promise<ClassificationResult> {
    const prompt = `
    Given this email, assign one or more labels from:
    ⚖️ Client Action, 📅 Time Sensitive, 📞 Follow-Up, 🧾 Billing Related, 📁 New Matter.
    If none apply, respond: No Label.
    
    Subject: ${email.subject}
    From: ${email.from}
    Body:
    ${email.body.substring(0, 1500)} ${email.body.length > 1500 ? '...(truncated)' : ''}
    
    Respond with ONLY the labels that apply, comma-separated. Include a confidence score for each label from 0-1.
    `;

    const response = await this.client.chat.completions.create({
      model: 'gpt-4',
      messages: [
        { role: 'system', content: 'You are a legal email classifier. You analyze emails and apply appropriate labels based on the content.' },
        { role: 'user', content: prompt }
      ],
      temperature: 0.3
    });

    const content = response.choices[0].message.content || 'No Label';
    
    // Parse the output
    const labels: string[] = [];
    const confidence: Record<string, number> = {};
    
    if (content !== 'No Label') {
      const parts = content.split(',').map(p => p.trim());
      
      for (const part of parts) {
        // Look for label and confidence pattern
        const match = part.match(/([^(]+)(?:\s*\((\d+\.\d+)\))?/);
        if (match) {
          const label = match[1].trim();
          labels.push(label);
          
          // Get confidence if provided, otherwise use 1.0
          const conf = match[2] ? parseFloat(match[2]) : 1.0;
          confidence[label] = conf;
        }
      }
    }
    
    return { labels, confidence };
  }

  /**
   * Generates a reply based on the email thread and the lawyer's voice profile
   */
  async generateReply(thread: EmailThread, voiceProfile: string): Promise<ReplyGeneration> {
    // Extract the most recent message
    const latestMessage = thread.messages[thread.messages.length - 1];
    
    // Build thread summary for context
    const threadSummary = thread.messages.map(msg => 
      `From: ${msg.from}\nSubject: ${msg.subject}\nDate: ${msg.date || 'Unknown'}\n${msg.body.substring(0, 300)}\n---`
    ).join('\n');

    const prompt = `
    Given the email thread, voice profile, and any prior similar messages, write a reply in the lawyer's voice.

    [Voice Profile]
    ${voiceProfile}

    [Thread Summary]
    ${threadSummary}

    The reply should maintain the lawyer's tone, include key facts from the email thread, and end with an appropriate sign-off.
    If a time estimate or scheduling is involved, please include concrete days/times.
    `;

    const response = await this.client.chat.completions.create({
      model: 'gpt-4',
      messages: [
        { role: 'system', content: 'You are a legal assistant that drafts emails in the lawyer\'s typical voice and style.' },
        { role: 'user', content: prompt }
      ],
      temperature: 0.7
    });

    const reply = response.choices[0].message.content || '';
    
    // Estimate time based on content complexity
    // This is a simple heuristic - could be improved
    const wordCount = reply.split(/\s+/).length;
    const timeEstimate = Math.round((wordCount / 100) * 0.1 * 10) / 10; // Rough estimate of 0.1h per 100 words
    
    return { 
      reply, 
      timeEstimate: Math.max(0.1, Math.min(timeEstimate, 0.5)) // Cap between 0.1 and 0.5 hours
    };
  }

  /**
   * Analyzes a lawyer's sent emails to create a voice profile
   */
  async createVoiceProfile(sentEmails: EmailContent[]): Promise<string> {
    // Take a sample of emails (max 10 for API efficiency)
    const sampleSize = Math.min(sentEmails.length, 10);
    const sample = sentEmails
      .slice(0, sampleSize)
      .map(email => email.body.substring(0, 500))
      .join('\n\n---\n\n');
    
    const prompt = `
    Analyze the tone, structure, and key phrases from these email samples. 
    Summarize the lawyer's communication style in ~5 bullet points.
    
    ${sample}
    `;

    const response = await this.client.chat.completions.create({
      model: 'gpt-4',
      messages: [
        { role: 'system', content: 'You analyze writing patterns to create a voice profile for a lawyer.' },
        { role: 'user', content: prompt }
      ],
      temperature: 0.5
    });

    return response.choices[0].message.content || '';
  }
} 