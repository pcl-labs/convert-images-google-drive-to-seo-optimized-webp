/**
 * Blawby Gmail Agent Add-On
 * 
 * This Google Workspace Add-On integrates with the Blawby system to help lawyers
 * manage their email more efficiently using AI assistance.
 */

// Configuration
const API_BASE_URL = 'https://blawby-gmail-agent.xyz.workers.dev';
const ADDON_NAME = 'Blawby Gmail Agent';

/**
 * Checks if the user is authorized to use the add-on.
 * @return {boolean} Whether the user is authorized.
 */
function isAuthorized() {
  try {
    // Check if we can get an OAuth token
    const token = ScriptApp.getOAuthToken();
    
    // TODO: Make API call to check if token is valid and user is registered
    // For now, consider authorized if we have a token
    return token != null;
  } catch (e) {
    console.error('Authorization check failed:', e);
    return false;
  }
}

/**
 * Runs when the add-on is installed.
 * @param {Object} e The event object.
 * @return {Card[]} The cards to display.
 */
function onInstall(e) {
  console.log('Add-on installed');
  return onHomepage(e);
}

/**
 * Runs when the add-on is opened from the homepage.
 * @param {Object} e The event object.
 * @return {Card[]} The cards to display.
 */
function onHomepage(e) {
  const card = createSetupCard();
  return [card];
}

/**
 * Creates the setup card for the add-on.
 * @return {Card} The card to display.
 */
function createSetupCard() {
  const card = CardService.newCardBuilder();
  
  // Header
  card.setHeader(CardService.newCardHeader()
    .setTitle(ADDON_NAME)
    .setImageUrl('https://www.gstatic.com/images/icons/material/system/1x/assignment_black_48dp.png'));
  
  // Connect section
  const connectSection = CardService.newCardSection()
    .setHeader('Connect to Blawby')
    .addWidget(CardService.newTextParagraph()
      .setText('To get started, connect your Gmail account to the Blawby system.'))
    .addWidget(CardService.newButtonSet()
      .addButton(CardService.newTextButton()
        .setText('Connect Gmail')
        .setAuthorizationAction(CardService.newAuthorizationAction()
          .setAuthorizationUrl('https://agent.blawby.com/auth/authorize'))));
  
  card.addSection(connectSection);
  
  return card.build();
}

/**
 * Runs when the add-on is opened on an email thread.
 * @param {Object} e The event object.
 * @return {Card[]} The cards to display.
 */
function onGmailMessage(e) {
  const accessToken = ScriptApp.getOAuthToken();
  
  if (!accessToken) {
    return [createSetupCard()];
  }
  
  const messageId = e.gmail.messageId;
  const thread = GmailApp.getMessageById(messageId).getThread();
  
  return [createEmailActionsCard(thread, messageId)];
}

/**
 * Creates a card with actions for the current email.
 * @param {GmailThread} thread The Gmail thread.
 * @param {string} messageId The current message ID.
 * @return {Card} The card to display.
 */
function createEmailActionsCard(thread, messageId) {
  const card = CardService.newCardBuilder();
  
  // Header
  card.setHeader(CardService.newCardHeader()
    .setTitle('Blawby Assistant')
    .setImageUrl('https://www.gstatic.com/images/icons/material/system/1x/assistant_black_48dp.png'));
  
  // Email info section
  const message = GmailApp.getMessageById(messageId);
  const sender = message.getFrom();
  const subject = message.getSubject();
  
  const infoSection = CardService.newCardSection()
    .addWidget(CardService.newKeyValue()
      .setTopLabel('From')
      .setContent(sender))
    .addWidget(CardService.newKeyValue()
      .setTopLabel('Subject')
      .setContent(subject));
  
  card.addSection(infoSection);
  
  // Actions section
  const actionsSection = CardService.newCardSection()
    .setHeader('Actions')
    .addWidget(CardService.newTextButton()
      .setText('Generate Reply')
      .setOnClickAction(CardService.newAction()
        .setFunctionName('generateReply')
        .setParameters({messageId: messageId})))
    .addWidget(CardService.newTextButton()
      .setText('Smart Label')
      .setOnClickAction(CardService.newAction()
        .setFunctionName('applySmartLabels')
        .setParameters({threadId: thread.getId()})));
  
  card.addSection(actionsSection);
  
  // Time tracking section
  const timeTrackingSection = CardService.newCardSection()
    .setHeader('Time Tracking')
    .addWidget(CardService.newTextInput()
      .setFieldName('timeSpent')
      .setTitle('Hours Spent')
      .setValue('0.2'))
    .addWidget(CardService.newTextInput()
      .setFieldName('clientMatter')
      .setTitle('Client/Matter')
      .setValue(''))
    .addWidget(CardService.newButtonSet()
      .addButton(CardService.newTextButton()
        .setText('Log Time')
        .setOnClickAction(CardService.newAction()
          .setFunctionName('logTime')
          .setParameters({messageId: messageId}))));
  
  card.addSection(timeTrackingSection);
  
  return card.build();
}

/**
 * Generates an AI reply for the current email.
 * @param {Object} e The event object.
 * @return {Card} The updated card with generated reply.
 */
function generateReply(e) {
  const messageId = e.parameters.messageId;
  const message = GmailApp.getMessageById(messageId);
  const thread = message.getThread();
  const subject = message.getSubject();
  
  // Get thread messages for context
  const messages = thread.getMessages();
  const threadContext = messages.map(msg => ({
    from: msg.getFrom(),
    subject: msg.getSubject(),
    body: msg.getPlainBody(),
    date: msg.getDate()
  }));
  
  // TODO: Call backend API to generate reply
  // For now, use a placeholder response
  const generatedReply = "Thank you for your email. I've reviewed the documents and will get back to you by Friday with my complete analysis.";
  
  // Create a card to display the generated reply
  const card = CardService.newCardBuilder();
  
  card.setHeader(CardService.newCardHeader()
    .setTitle('Generated Reply')
    .setImageUrl('https://www.gstatic.com/images/icons/material/system/1x/edit_black_48dp.png'));
  
  const replySection = CardService.newCardSection()
    .addWidget(CardService.newTextParagraph()
      .setText('Review and edit this AI-generated reply:'))
    .addWidget(CardService.newTextArea()
      .setFieldName('replyText')
      .setValue(generatedReply))
    .addWidget(CardService.newButtonSet()
      .addButton(CardService.newTextButton()
        .setText('Insert as Draft')
        .setOnClickAction(CardService.newAction()
          .setFunctionName('insertDraft')
          .setParameters({messageId: messageId}))));
  
  card.addSection(replySection);
  
  return card.build();
}

/**
 * Inserts the generated reply as a draft.
 * @param {Object} e The event object.
 * @return {Card} A notification card.
 */
function insertDraft(e) {
  const messageId = e.parameters.messageId;
  const replyText = e.formInput.replyText;
  const message = GmailApp.getMessageById(messageId);
  
  // Create a draft reply
  message.createDraftReply(replyText);
  
  // Create a notification card
  const card = CardService.newCardBuilder();
  
  card.setHeader(CardService.newCardHeader()
    .setTitle('Success')
    .setImageUrl('https://www.gstatic.com/images/icons/material/system/1x/check_circle_black_48dp.png'));
  
  const notificationSection = CardService.newCardSection()
    .addWidget(CardService.newTextParagraph()
      .setText('Draft reply created successfully.'))
    .addWidget(CardService.newButtonSet()
      .addButton(CardService.newTextButton()
        .setText('Back to Email')
        .setOnClickAction(CardService.newAction()
          .setFunctionName('onGmailMessage')
          .setParameters({messageId: messageId}))));
  
  card.addSection(notificationSection);
  
  return card.build();
}

/**
 * Applies smart labels to the current thread.
 * @param {Object} e The event object.
 * @return {Card} A notification card.
 */
function applySmartLabels(e) {
  const threadId = e.parameters.threadId;
  const thread = GmailApp.getThreadById(threadId);
  const messages = thread.getMessages();
  
  // Extract content for classification
  const lastMessage = messages[messages.length - 1];
  const content = {
    subject: lastMessage.getSubject(),
    body: lastMessage.getPlainBody(),
    from: lastMessage.getFrom()
  };
  
  // TODO: Call backend API to classify email
  // For now, use placeholder labels
  const labels = ['⚖️ Client Action', '📅 Time Sensitive'];
  
  // Apply labels to the thread
  labels.forEach(labelName => {
    let label = GmailApp.getUserLabelByName(labelName);
    if (!label) {
      label = GmailApp.createLabel(labelName);
    }
    thread.addLabel(label);
  });
  
  // Create a notification card
  const card = CardService.newCardBuilder();
  
  card.setHeader(CardService.newCardHeader()
    .setTitle('Labels Applied')
    .setImageUrl('https://www.gstatic.com/images/icons/material/system/1x/label_black_48dp.png'));
  
  const notificationSection = CardService.newCardSection()
    .addWidget(CardService.newTextParagraph()
      .setText('The following labels were applied:'))
    .addWidget(CardService.newTextParagraph()
      .setText(labels.join(', ')))
    .addWidget(CardService.newButtonSet()
      .addButton(CardService.newTextButton()
        .setText('Back to Email')
        .setOnClickAction(CardService.newAction()
          .setFunctionName('onGmailMessage')
          .setParameters({messageId: lastMessage.getId()}))));
  
  card.addSection(notificationSection);
  
  return card.build();
}

/**
 * Logs time spent on an email.
 * @param {Object} e The event object.
 * @return {Card} A notification card.
 */
function logTime(e) {
  const messageId = e.parameters.messageId;
  const timeSpent = e.formInput.timeSpent;
  const clientMatter = e.formInput.clientMatter;
  const message = GmailApp.getMessageById(messageId);
  
  // TODO: Call backend API to log time
  // For now, just log to console
  console.log(`Logged ${timeSpent} hours for ${clientMatter} on email: ${message.getSubject()}`);
  
  // Create a notification card
  const card = CardService.newCardBuilder();
  
  card.setHeader(CardService.newCardHeader()
    .setTitle('Time Logged')
    .setImageUrl('https://www.gstatic.com/images/icons/material/system/1x/schedule_black_48dp.png'));
  
  const notificationSection = CardService.newCardSection()
    .addWidget(CardService.newTextParagraph()
      .setText(`Logged ${timeSpent} hours for ${clientMatter}`))
    .addWidget(CardService.newButtonSet()
      .addButton(CardService.newTextButton()
        .setText('Back to Email')
        .setOnClickAction(CardService.newAction()
          .setFunctionName('onGmailMessage')
          .setParameters({messageId: messageId}))));
  
  card.addSection(notificationSection);
  
  return card.build();
}

/**
 * Callback function for processing the API token after OAuth.
 * @param {Object} e The event object.
 * @return {Card} The card to display.
 */
function processToken(e) {
  const token = e.parameters.token;
  
  if (!token) {
    return createSetupCard();
  }
  
  // Store the token
  getUserProperty('apiToken', token);
  
  // Show the profile card
  return createProfileCard();
} 