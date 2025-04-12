interface TokenResponse {
  access_token: string;
  refresh_token: string;
  expires_in: number;
  token_type: string;
  scope: string;
}

interface StoredTokens {
  accessToken: string;
  refreshToken: string;
  expiry: number; // Timestamp in milliseconds
}

export class OAuthService {
  private clientId: string;
  private clientSecret: string;
  private redirectUri: string;
  private kvStore: KVNamespace;
  
  constructor(clientId: string, clientSecret: string, redirectUri: string, kvStore: KVNamespace) {
    this.clientId = clientId;
    this.clientSecret = clientSecret;
    this.redirectUri = redirectUri;
    this.kvStore = kvStore;
  }
  
  /**
   * Exchanges an authorization code for tokens
   */
  async exchangeCodeForTokens(code: string): Promise<StoredTokens> {
    const params = new URLSearchParams();
    params.append('code', code);
    params.append('client_id', this.clientId);
    params.append('client_secret', this.clientSecret);
    params.append('redirect_uri', this.redirectUri);
    params.append('grant_type', 'authorization_code');
    
    const response = await fetch('https://oauth2.googleapis.com/token', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded'
      },
      body: params.toString()
    });
    
    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`Token exchange failed: ${response.status} ${response.statusText} - ${errorText}`);
    }
    
    const tokenData: TokenResponse = await response.json();
    
    // Calculate expiry time (subtract 5 minutes as buffer)
    const expiryTime = Date.now() + (tokenData.expires_in * 1000) - (5 * 60 * 1000);
    
    const tokens: StoredTokens = {
      accessToken: tokenData.access_token,
      refreshToken: tokenData.refresh_token,
      expiry: expiryTime
    };
    
    return tokens;
  }
  
  /**
   * Refreshes an access token using the refresh token
   */
  async refreshAccessToken(refreshToken: string): Promise<Partial<StoredTokens>> {
    const params = new URLSearchParams();
    params.append('refresh_token', refreshToken);
    params.append('client_id', this.clientId);
    params.append('client_secret', this.clientSecret);
    params.append('grant_type', 'refresh_token');
    
    const response = await fetch('https://oauth2.googleapis.com/token', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded'
      },
      body: params.toString()
    });
    
    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`Token refresh failed: ${response.status} ${response.statusText} - ${errorText}`);
    }
    
    const tokenData = await response.json();
    
    // Calculate expiry time (subtract 5 minutes as buffer)
    const expiryTime = Date.now() + (tokenData.expires_in * 1000) - (5 * 60 * 1000);
    
    return {
      accessToken: tokenData.access_token,
      expiry: expiryTime
    };
  }
  
  /**
   * Stores tokens in KV store
   */
  async storeTokens(userId: string, tokens: StoredTokens): Promise<void> {
    await this.kvStore.put(`tokens:${userId}`, JSON.stringify(tokens));
  }
  
  /**
   * Retrieves tokens from KV store
   */
  async getTokens(userId: string): Promise<StoredTokens | null> {
    const tokensStr = await this.kvStore.get(`tokens:${userId}`);
    
    if (!tokensStr) {
      return null;
    }
    
    return JSON.parse(tokensStr) as StoredTokens;
  }
  
  /**
   * Gets a valid access token, refreshing if necessary
   */
  async getValidAccessToken(userId: string): Promise<string | null> {
    const tokens = await this.getTokens(userId);
    
    if (!tokens) {
      return null;
    }
    
    // Check if token is expired
    if (tokens.expiry <= Date.now()) {
      try {
        // Refresh the token
        const refreshedTokens = await this.refreshAccessToken(tokens.refreshToken);
        
        // Update stored tokens
        const updatedTokens: StoredTokens = {
          ...tokens,
          ...refreshedTokens
        };
        
        await this.storeTokens(userId, updatedTokens);
        
        return updatedTokens.accessToken;
      } catch (error) {
        console.error('Failed to refresh token:', error);
        return null;
      }
    }
    
    return tokens.accessToken;
  }
  
  /**
   * Revokes tokens and removes from storage
   */
  async revokeTokens(userId: string): Promise<void> {
    const tokens = await this.getTokens(userId);
    
    if (tokens) {
      try {
        // Revoke access token
        await fetch(`https://oauth2.googleapis.com/revoke?token=${tokens.accessToken}`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/x-www-form-urlencoded'
          }
        });
        
        // Revoke refresh token
        await fetch(`https://oauth2.googleapis.com/revoke?token=${tokens.refreshToken}`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/x-www-form-urlencoded'
          }
        });
      } catch (error) {
        console.error('Error revoking tokens:', error);
      }
    }
    
    // Remove from storage regardless of revocation success
    await this.kvStore.delete(`tokens:${userId}`);
  }
} 