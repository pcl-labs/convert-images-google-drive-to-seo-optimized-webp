#!/usr/bin/env node

/**
 * Daily test runner for Blawby Gmail Agent
 * 
 * This script:
 * 1. Runs the test suite
 * 2. Makes health check calls to the production API
 * 3. Reports results (logs/notifications)
 * 
 * Run via cron job: 0 0 * * * node /path/to/run-daily-tests.js
 */

const { execSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const fetch = require('node-fetch');

// Configuration
const API_URL = 'https://blawby-gmail-agent.paulchrisluke.workers.dev';
const LOG_DIR = path.join(__dirname, '../logs');
const MAX_LOG_FILES = 7; // Keep a week of logs

// Ensure log directory exists
if (!fs.existsSync(LOG_DIR)) {
  fs.mkdirSync(LOG_DIR, { recursive: true });
}

// Generate log filename
const timestamp = new Date().toISOString().split('T')[0];
const logFile = path.join(LOG_DIR, `daily-test-${timestamp}.log`);
const logStream = fs.createWriteStream(logFile, { flags: 'a' });

// Helper to log messages
function log(message) {
  const timestamp = new Date().toISOString();
  const logMessage = `[${timestamp}] ${message}`;
  console.log(logMessage);
  logStream.write(logMessage + '\n');
}

// Run tests and log results
async function runTests() {
  try {
    log('Starting daily test suite run');
    
    // Step 1: Run test suite
    log('Running test suite...');
    try {
      const testOutput = execSync('cd "' + path.join(__dirname, '..') + '" && npm test', { 
        encoding: 'utf8',
        stdio: 'pipe'
      });
      log('Test suite completed successfully:');
      log(testOutput);
    } catch (error) {
      log('Test suite failed:');
      log(error.stdout || error.message);
      throw new Error('Test suite failed');
    }
    
    // Step 2: Health check API
    log('Performing API health check...');
    const healthCheck = await fetch(`${API_URL}/`);
    
    if (!healthCheck.ok) {
      log(`API health check failed: HTTP ${healthCheck.status}`);
      throw new Error('API health check failed');
    }
    
    const healthData = await healthCheck.json();
    log(`API health check successful: Status ${healthData.status}`);
    
    // Step 3: Clean up old logs
    const logFiles = fs.readdirSync(LOG_DIR);
    if (logFiles.length > MAX_LOG_FILES) {
      log(`Cleaning up old log files (keeping last ${MAX_LOG_FILES})...`);
      
      const sortedFiles = logFiles
        .map(file => ({ file, mtime: fs.statSync(path.join(LOG_DIR, file)).mtime }))
        .sort((a, b) => b.mtime.getTime() - a.mtime.getTime());
      
      sortedFiles.slice(MAX_LOG_FILES).forEach(({ file }) => {
        fs.unlinkSync(path.join(LOG_DIR, file));
        log(`Removed old log file: ${file}`);
      });
    }
    
    log('Daily test run completed successfully');
    return true;
  } catch (error) {
    log(`ERROR: ${error.message}`);
    
    // Add notification logic here (e.g., send email, SMS, etc.)
    // For example: sendNotification(`Blawby Gmail Agent test failed: ${error.message}`);
    
    return false;
  } finally {
    logStream.end();
  }
}

// Run the tests
runTests().then(success => {
  process.exit(success ? 0 : 1);
}); 