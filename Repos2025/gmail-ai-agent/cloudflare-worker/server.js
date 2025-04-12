// Local development server using Miniflare
// Run with: node server.js

const { Miniflare } = require('miniflare');
const dotenv = require('dotenv');

dotenv.config();

async function main() {
  const mf = new Miniflare({
    scriptPath: './dist/index.js',
    modules: true,
    bindings: {
      OPENAI_API_KEY: process.env.OPENAI_API_KEY || 'dummy-key',
      GOOGLE_CLIENT_ID: process.env.GOOGLE_CLIENT_ID || 'dummy-client-id',
      GOOGLE_CLIENT_SECRET: process.env.GOOGLE_CLIENT_SECRET || 'dummy-client-secret',
      JWT_SECRET: process.env.JWT_SECRET || 'local-dev-secret',
    },
    kvNamespaces: ['OAUTH_TOKENS'],
    port: 8787,
  });

  // Start the server
  console.log('Starting local development server on http://localhost:8787');
  const server = await mf.createServer();
  server.listen(8787);
  
  console.log('Environment variables loaded:');
  console.log('- OPENAI_API_KEY:', process.env.OPENAI_API_KEY ? '✅ Set' : '❌ Missing');
  console.log('- GOOGLE_CLIENT_ID:', process.env.GOOGLE_CLIENT_ID ? '✅ Set' : '❌ Missing');
  console.log('- GOOGLE_CLIENT_SECRET:', process.env.GOOGLE_CLIENT_SECRET ? '✅ Set' : '❌ Missing');
  console.log('- JWT_SECRET:', process.env.JWT_SECRET ? '✅ Set' : '❌ Missing');
}

main().catch(console.error); 