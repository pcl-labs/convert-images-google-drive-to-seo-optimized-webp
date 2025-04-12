interface GmailMessage {
  id: string;
  threadId: string;
  labelIds: string[];
  snippet: string;
  payload: {
    headers: {
      name: string;
      value: string;
    }[];
    body: {
      data?: string;
    };
    parts?: {
      mimeType: string;
      body: {
        data?: string;
      };
    }[];
  };
  internalDate: string;
}

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

export class GmailService {
  private accessToken: string;
  
  constructor(accessToken: string) {
    this.accessToken = accessToken;
  }
  
  /**
   * Fetches recent messages from Gmail
   */
  async getRecentMessages(maxResults = 10): Promise<GmailMessage[]> {
    const response = await fetch(
      `https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults=${maxResults}`,
      {
        headers: {
          'Authorization': `Bearer ${this.accessToken}`,
          'Content-Type': 'application/json'
        }
      }
    );
    
    if (!response.ok) {
      throw new Error(`Failed to fetch messages: ${response.statusText}`);
    }
    
    const data = await response.json();
    const messages = data.messages || [];
    
    // Fetch full message details
    const fullMessages: GmailMessage[] = [];
    
    for (const msg of messages) {
      const msgResponse = await fetch(
        `https://gmail.googleapis.com/gmail/v1/users/me/messages/${msg.id}?format=full`,
        {
          headers: {
            'Authorization': `Bearer ${this.accessToken}`,
            'Content-Type': 'application/json'
          }
        }
      );
      
      if (msgResponse.ok) {
        const fullMsg = await msgResponse.json();
        fullMessages.push(fullMsg);
      }
    }
    
    return fullMessages;
  }
  
  /**
   * Fetches and parses a thread by ID
   */
  async getThread(threadId: string): Promise<EmailThread> {
    const response = await fetch(
      `https://gmail.googleapis.com/gmail/v1/users/me/threads/${threadId}?format=full`,
      {
        headers: {
          'Authorization': `Bearer ${this.accessToken}`,
          'Content-Type': 'application/json'
        }
      }
    );
    
    if (!response.ok) {
      throw new Error(`Failed to fetch thread: ${response.statusText}`);
    }
    
    const threadData = await response.json();
    const messages = threadData.messages || [];
    
    const parsedMessages: EmailContent[] = messages.map((msg: GmailMessage) => {
      // Extract headers
      const headers = msg.payload.headers;
      const subject = headers.find(h => h.name.toLowerCase() === 'subject')?.value || '';
      const from = headers.find(h => h.name.toLowerCase() === 'from')?.value || '';
      const to = headers.find(h => h.name.toLowerCase() === 'to')?.value || '';
      const date = headers.find(h => h.name.toLowerCase() === 'date')?.value || '';
      
      // Extract body
      let body = '';
      
      // Try to get plain text body
      if (msg.payload.mimeType === 'text/plain' && msg.payload.body.data) {
        body = this.decodeBase64Url(msg.payload.body.data);
      } else if (msg.payload.parts) {
        // Look for plain text part
        const plainPart = msg.payload.parts.find(part => part.mimeType === 'text/plain');
        if (plainPart && plainPart.body.data) {
          body = this.decodeBase64Url(plainPart.body.data);
        }
      }
      
      return { subject, from, to, body, date };
    });
    
    return {
      id: threadId,
      messages: parsedMessages
    };
  }
  
  /**
   * Applies labels to a thread
   */
  async applyLabels(threadId: string, labelNames: string[]): Promise<void> {
    // First, make sure the labels exist
    const existingLabels = await this.getLabels();
    const labelsToAdd: string[] = [];
    
    for (const labelName of labelNames) {
      const existingLabel = existingLabels.find(l => l.name === labelName);
      
      if (existingLabel) {
        labelsToAdd.push(existingLabel.id);
      } else {
        // Create the label if it doesn't exist
        const newLabel = await this.createLabel(labelName);
        labelsToAdd.push(newLabel.id);
      }
    }
    
    // Apply the labels to the thread
    const response = await fetch(
      `https://gmail.googleapis.com/gmail/v1/users/me/threads/${threadId}/modify`,
      {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${this.accessToken}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          addLabelIds: labelsToAdd
        })
      }
    );
    
    if (!response.ok) {
      throw new Error(`Failed to apply labels: ${response.statusText}`);
    }
  }
  
  /**
   * Gets all user labels
   */
  private async getLabels() {
    const response = await fetch(
      'https://gmail.googleapis.com/gmail/v1/users/me/labels',
      {
        headers: {
          'Authorization': `Bearer ${this.accessToken}`,
          'Content-Type': 'application/json'
        }
      }
    );
    
    if (!response.ok) {
      throw new Error(`Failed to fetch labels: ${response.statusText}`);
    }
    
    const data = await response.json();
    return data.labels || [];
  }
  
  /**
   * Creates a new label
   */
  private async createLabel(name: string) {
    const response = await fetch(
      'https://gmail.googleapis.com/gmail/v1/users/me/labels',
      {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${this.accessToken}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ name })
      }
    );
    
    if (!response.ok) {
      throw new Error(`Failed to create label: ${response.statusText}`);
    }
    
    return response.json();
  }
  
  /**
   * Fetches sent messages for voice profile creation
   */
  async getSentMessages(maxResults = 50): Promise<EmailContent[]> {
    const response = await fetch(
      `https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults=${maxResults}&q=in:sent`,
      {
        headers: {
          'Authorization': `Bearer ${this.accessToken}`,
          'Content-Type': 'application/json'
        }
      }
    );
    
    if (!response.ok) {
      throw new Error(`Failed to fetch sent messages: ${response.statusText}`);
    }
    
    const data = await response.json();
    const messages = data.messages || [];
    
    // Fetch and parse full message details
    const parsedMessages: EmailContent[] = [];
    
    for (const msg of messages) {
      const msgResponse = await fetch(
        `https://gmail.googleapis.com/gmail/v1/users/me/messages/${msg.id}?format=full`,
        {
          headers: {
            'Authorization': `Bearer ${this.accessToken}`,
            'Content-Type': 'application/json'
          }
        }
      );
      
      if (msgResponse.ok) {
        const fullMsg: GmailMessage = await msgResponse.json();
        
        // Extract headers
        const headers = fullMsg.payload.headers;
        const subject = headers.find(h => h.name.toLowerCase() === 'subject')?.value || '';
        const from = headers.find(h => h.name.toLowerCase() === 'from')?.value || '';
        const to = headers.find(h => h.name.toLowerCase() === 'to')?.value || '';
        const date = headers.find(h => h.name.toLowerCase() === 'date')?.value || '';
        
        // Extract body
        let body = '';
        
        if (fullMsg.payload.mimeType === 'text/plain' && fullMsg.payload.body.data) {
          body = this.decodeBase64Url(fullMsg.payload.body.data);
        } else if (fullMsg.payload.parts) {
          const plainPart = fullMsg.payload.parts.find(part => part.mimeType === 'text/plain');
          if (plainPart && plainPart.body.data) {
            body = this.decodeBase64Url(plainPart.body.data);
          }
        }
        
        parsedMessages.push({ subject, from, to, body, date });
      }
    }
    
    return parsedMessages;
  }
  
  /**
   * Decode base64url encoded string to text
   */
  private decodeBase64Url(encoded: string): string {
    // Convert base64url to standard base64
    const base64 = encoded.replace(/-/g, '+').replace(/_/g, '/');
    
    // Decode using atob and handle UTF-8
    try {
      const binaryString = atob(base64);
      const bytes = new Uint8Array(binaryString.length);
      
      for (let i = 0; i < binaryString.length; i++) {
        bytes[i] = binaryString.charCodeAt(i);
      }
      
      return new TextDecoder().decode(bytes);
    } catch (e) {
      console.error('Failed to decode base64:', e);
      return '';
    }
  }
} 