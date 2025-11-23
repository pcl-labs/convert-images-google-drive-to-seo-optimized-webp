export interface Env {
  CHAT_SESSIONS: DurableObjectNamespace;
  API_BASE_URL?: string;
}

type Role = "user" | "assistant" | "system";
const PASS_THROUGH_HEADERS = ["authorization", "x-api-key", "cookie"];

export interface SessionMessage {
  messageId: string;
  role: Role;
  content: string;
  createdAt: string;
  toolCall?: Record<string, unknown>;
}

export interface SessionRecord {
  sessionId: string;
  createdAt: string;
  updatedAt: string;
  title?: string;
  metadata?: Record<string, unknown>;
  messages: SessionMessage[];
}

interface SessionEvent {
  type: "snapshot" | "message" | "metadata";
  payload: unknown;
}

interface SessionEventsPayload {
  session_id: string;
  events?: Array<Record<string, unknown>>;
  jobs?: Array<Record<string, unknown>>;
}

const encoder = new TextEncoder();

const jsonResponse = (body: unknown, init?: ResponseInit): Response => {
  return new Response(JSON.stringify(body), {
    status: init?.status ?? 200,
    headers: {
      "Content-Type": "application/json",
      ...init?.headers,
    },
  });
};

const notFound = (message = "Not found") =>
  jsonResponse({ error: message }, { status: 404 });

const badRequest = (message: string) =>
  jsonResponse({ error: message }, { status: 400 });

interface ToolDefinition {
  name: string;
  description: string;
  method: "GET" | "POST";
  path: string;
  input?: Record<string, unknown>;
  samples?: Record<string, unknown>;
}

const TOOL_DEFINITIONS: ToolDefinition[] = [
  {
    name: "ingest_youtube",
    description: "Register a YouTube URL as a document by fetching transcript + metadata.",
    method: "POST",
    path: "/ingest/youtube",
    input: {
      url: "https://www.youtube.com/watch?v=...",
      document_metadata: { tags: ["youtube"] },
    },
  },
  {
    name: "ingest_text",
    description: "Create/update a document from provided raw text.",
    method: "POST",
    path: "/ingest/text",
    input: {
      document_id: "doc-uuid",
      text: "Raw markdown or MDX content",
    },
  },
  {
    name: "generate_blog",
    description: "Run the generate_blog pipeline for a document.",
    method: "POST",
    path: "/api/v1/pipelines/generate_blog",
    input: {
      document_id: "doc-uuid",
      options: {
        tone: "conversational",
        include_images: false,
      },
    },
  },
  {
    name: "optimize_drive",
    description: "Optimize Drive-backed assets for a document.",
    method: "POST",
    path: "/api/v1/optimize",
    input: {
      document_id: "doc-uuid",
      extensions: ["jpg", "png"],
    },
  },
  {
    name: "drive_publish",
    description: "Publish current MDX to Google Drive.",
    method: "POST",
    path: "/api/v1/documents/{document_id}/drive/publish",
    input: {
      stage: "published",
    },
  },
  {
    name: "drive_link_workspace",
    description: "Ensure a document has a Drive workspace (folders + Doc) linked.",
    method: "POST",
    path: "/api/v1/documents/{document_id}/drive/link",
    input: {
      document_name: "Optional display name for Drive artifacts",
      metadata: {},
    },
  },
  {
    name: "drive_status",
    description: "Fetch Drive linkage metadata for a document.",
    method: "GET",
    path: "/api/v1/documents/{document_id}/drive",
  },
  {
    name: "document_versions",
    description: "List the stored versions for a document.",
    method: "GET",
    path: "/api/v1/documents/{document_id}/versions",
  },
  {
    name: "document_export",
    description: "Create a document export (e.g., Google Docs, Zapier, WordPress).",
    method: "POST",
    path: "/api/v1/documents/{document_id}/exports",
    input: {
      target: "google_docs",
      version_id: "latest",
    },
  },
  {
    name: "session_events",
    description: "Fetch recent pipeline events and job statuses for the current agent session.",
    method: "GET",
    path: "/api/v1/sessions/events",
  },
];

const TOOL_MAP = new Map(TOOL_DEFINITIONS.map((tool) => [tool.name, tool]));

function ensureApiBase(env: Env): string {
  const base = env.API_BASE_URL;
  if (!base) {
    throw new Error("API_BASE_URL is not configured");
  }
  return base.replace(/\/$/, "");
}

function buildToolTarget(
  tool: ToolDefinition,
  env: Env,
  requestUrl: URL,
): URL {
  const base = ensureApiBase(env);
  let resolvedPath = tool.path;
  const usedParams = new Set<string>();
  const placeholderRegex = /{([^}]+)}/g;
  let match: RegExpExecArray | null;
  while ((match = placeholderRegex.exec(tool.path)) !== null) {
    const key = match[1];
    const value = requestUrl.searchParams.get(key);
    if (!value) {
      throw new Error(`${tool.name} requires ?${key}= parameter`);
    }
    usedParams.add(key);
    resolvedPath = resolvedPath.replace(
      new RegExp(`{${key}}`, "g"),
      encodeURIComponent(value),
    );
  }
  const targetUrl = new URL(`${base}${resolvedPath}`);
  requestUrl.searchParams.forEach((value, key) => {
    if (!usedParams.has(key)) {
      targetUrl.searchParams.set(key, value);
    }
  });
  return targetUrl;
}

function listTools(): Response {
  return jsonResponse({
    tools: TOOL_DEFINITIONS.map((tool) => ({
      name: tool.name,
      description: tool.description,
      method: tool.method,
      path: tool.path,
      input: tool.input,
    })),
  });
}

async function invokeTool(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const [, , , toolName] = url.pathname.split("/", 5);
  if (!toolName) {
    return badRequest("Tool name required");
  }
  const tool = TOOL_MAP.get(toolName);
  if (!tool) {
    return notFound("Unknown tool");
  }
  let targetUrl: URL;
  try {
    targetUrl = buildToolTarget(tool, env, url);
  } catch (error) {
    return badRequest(error instanceof Error ? error.message : String(error));
  }
  const headers = new Headers();
  for (const header of PASS_THROUGH_HEADERS) {
    const value = request.headers.get(header);
    if (value) {
      headers.set(header, value);
    }
  }
  const sessionId =
    request.headers.get("x-agent-session-id") ?? url.searchParams.get("session_id");
  if (sessionId) {
    headers.set("x-agent-session-id", sessionId);
    if (!targetUrl.searchParams.has("session_id")) {
      targetUrl.searchParams.set("session_id", sessionId);
    }
  }
  const accept = request.headers.get("accept");
  if (accept) {
    headers.set("accept", accept);
  }
  const hasBody = tool.method === "POST";
  let body: BodyInit | undefined;
  if (hasBody) {
    const contentType = request.headers.get("content-type");
    if (contentType) {
      headers.set("content-type", contentType);
    } else {
      headers.set("content-type", "application/json");
    }
    body = await request.clone().text();
  }
  try {
    const apiResponse = await fetch(targetUrl.toString(), {
      method: tool.method,
      headers,
      body,
    });
    const passthroughHeaders: Record<string, string> = {
      "Content-Type": apiResponse.headers.get("content-type") ?? "application/json",
    };
    const text = await apiResponse.text();
    return new Response(text, {
      status: apiResponse.status,
      headers: passthroughHeaders,
    });
  } catch (error) {
    console.error("tool_invoke_failed", {
      tool: toolName,
      target: targetUrl.toString(),
      error: error instanceof Error ? error.message : String(error),
    });
    return jsonResponse(
      {
        error: "Failed to reach backend API for tool invocation",
      },
      { status: 502 },
    );
  }
}

async function createSession(env: Env): Promise<Response> {
  const id = env.CHAT_SESSIONS.newUniqueId();
  const stub = env.CHAT_SESSIONS.get(id);
  const resp = await stub.fetch("https://session/init", { method: "POST" });
  const data = await resp.json<SessionRecord>();
  return jsonResponse(data, { status: 201 });
}

function getSessionStub(env: Env, sessionId: string): DurableObjectStub | null {
  try {
    const id = env.CHAT_SESSIONS.idFromString(sessionId);
    return env.CHAT_SESSIONS.get(id);
  } catch {
    return null;
  }
}

async function forwardToSession(
  stub: DurableObjectStub,
  path: string,
  request: Request,
) {
  const init: RequestInit = {
    method: request.method,
    headers: request.headers,
  };
  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = request.body;
  }
  return stub.fetch(`https://session${path}`, init);
}

async function handleSessionRoute(
  request: Request,
  env: Env,
  url: URL,
): Promise<Response> {
  const pathname = url.pathname;
  const [, , , sessionId] = pathname.split("/");
  if (!sessionId) {
    return badRequest("Session ID is required");
  }
  const stub = getSessionStub(env, sessionId);
  if (!stub) {
    return notFound("Unknown session");
  }
  const base = `/chat/sessions/${sessionId}`;
  const suffix = pathname.slice(base.length) || "/";
  const target = `${suffix}${url.search}`;
  return forwardToSession(stub, target, request);
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const { pathname } = url;

    if (pathname === "/agent/tools" && request.method === "GET") {
      return listTools();
    }

    if (pathname.startsWith("/agent/tools/") && pathname.endsWith("/invoke")) {
      if (request.method !== "POST") {
        return badRequest("Tool invocation must use POST");
      }
      return invokeTool(request, env);
    }

    if (pathname === "/chat/sessions" && request.method === "POST") {
      return createSession(env);
    }

    if (pathname.startsWith("/chat/sessions/")) {
      return handleSessionRoute(request, env, url);
    }

    if (pathname === "/health") {
      return jsonResponse({ status: "ok", worker: "chat-gateway" });
    }

    return notFound();
  },
};

type Listener = {
  id: string;
  controller: ReadableStreamDefaultController<Uint8Array>;
  keepAlive?: number;
  pollAbort?: AbortController;
};

export class ChatSessionDurable {
  private state: DurableObjectState;
  private listeners: Set<Listener> = new Set();

  constructor(state: DurableObjectState, private env: Env) {
    this.state = state;
  }

  private buildForwardHeaders(requestHeaders: Headers): Headers {
    const headers = new Headers();
    for (const header of PASS_THROUGH_HEADERS) {
      const value = requestHeaders.get(header);
      if (value) {
        headers.set(header, value);
      }
    }
    headers.set("accept", "application/json");
    return headers;
  }

  private async sleep(ms: number): Promise<void> {
    await new Promise((resolve) => setTimeout(resolve, ms));
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/init") {
      return this.handleInit();
    }
    if (request.method === "GET" && url.pathname === "/") {
      const session = await this.loadSession();
      if (!session) {
        return notFound("Session not initialized");
      }
      return jsonResponse(session);
    }
    if (url.pathname === "/messages") {
      if (request.method === "GET") {
        return this.handleListMessages();
      }
      if (request.method === "POST") {
        return this.handleCreateMessage(request);
      }
    }
    if (url.pathname === "/events" && request.method === "GET") {
      return this.handleEventStream(request);
    }
    if (request.method === "DELETE" && url.pathname === "/") {
      await this.state.storage.delete("session");
      return new Response(null, { status: 204 });
    }
    return notFound();
  }

  private async loadSession(): Promise<SessionRecord | null> {
    const session = await this.state.storage.get<SessionRecord>("session");
    return session ?? null;
  }

  private async saveSession(record: SessionRecord): Promise<void> {
    record.updatedAt = new Date().toISOString();
    await this.state.storage.put("session", record);
  }

  private async handleInit(): Promise<Response> {
    const existing = await this.loadSession();
    if (existing) {
      return jsonResponse(existing);
    }
    const now = new Date().toISOString();
    const record: SessionRecord = {
      sessionId: this.state.id.toString(),
      createdAt: now,
      updatedAt: now,
      messages: [],
    };
    await this.saveSession(record);
    return jsonResponse(record, { status: 201 });
  }

  private async handleListMessages(): Promise<Response> {
    const session = await this.loadSession();
    if (!session) {
      return notFound("Session not initialized");
    }
    return jsonResponse({ messages: session.messages });
  }

  private async handleCreateMessage(request: Request): Promise<Response> {
    const session = await this.loadSession();
    if (!session) {
      return notFound("Session not initialized");
    }
    let body: { role?: Role; content?: string } = {};
    try {
      body = (await request.json()) as { role?: Role; content?: string };
    } catch {
      return badRequest("Invalid JSON payload");
    }
    const role = body.role ?? "user";
    const content = body.content ?? "";
    if (!content.trim()) {
      return badRequest("Message content is required");
    }
    const message: SessionMessage = {
      messageId: crypto.randomUUID(),
      role,
      content,
      createdAt: new Date().toISOString(),
    };
    session.messages.push(message);
    await this.saveSession(session);
    this.broadcast({ type: "message", payload: message });
    return jsonResponse(message, { status: 201 });
  }

  private handleEventStream(request: Request): Response {
    let activeListener: Listener | null = null;
    const stream = new ReadableStream<Uint8Array>({
      start: async (controller) => {
        const listener: Listener = {
          id: crypto.randomUUID(),
          controller,
          keepAlive: setInterval(() => {
            controller.enqueue(encoder.encode(`: keep-alive ${Date.now()}\n\n`));
          }, 15000),
        };
        activeListener = listener;
        this.listeners.add(listener);
        controller.enqueue(encoder.encode(`: connected ${Date.now()}\n\n`));
        const forwardHeaders = this.buildForwardHeaders(request.headers);
        this.startSessionEventPump(forwardHeaders, controller, listener).catch((error) => {
          console.error("session_event_pump_failed", error);
        });
        const snapshot = await this.loadSession();
        if (snapshot) {
          controller.enqueue(
            encoder.encode(
              `data: ${JSON.stringify({
                type: "snapshot",
                payload: snapshot,
              })}\n\n`,
            ),
          );
        }
      },
      cancel: () => {
        if (activeListener) {
          this.releaseListener(activeListener);
          activeListener = null;
        }
      },
    });
    return new Response(stream, {
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-store",
        Connection: "keep-alive",
      },
    });
  }

  private async startSessionEventPump(
    headers: Headers,
    controller: ReadableStreamDefaultController<Uint8Array>,
    listener: Listener,
  ): Promise<void> {
    const base = this.env.API_BASE_URL;
    if (!base) {
      return;
    }
    const sessionId = this.state.id.toString();
    headers.set("X-Agent-Session-Id", sessionId);
    const trimmedBase = base.replace(/\/$/, "");
    const abort = new AbortController();
    listener.pollAbort = abort;
    let lastSequence: number | undefined;
    while (!abort.signal.aborted) {
      try {
        const target = new URL(`${trimmedBase}/api/v1/sessions/events`);
        target.searchParams.set("session_id", sessionId);
        if (lastSequence !== undefined) {
          target.searchParams.set("after_sequence", String(lastSequence));
        }
        const response = await fetch(target.toString(), {
          headers,
          signal: abort.signal,
        });
        if (!response.ok) {
          throw new Error(`session events fetch failed (${response.status})`);
        }
        const payload = (await response.json()) as SessionEventsPayload;
        const events = payload.events ?? [];
        for (const event of events) {
          const seq = (event as Record<string, unknown>).sequence;
          if (typeof seq === "number") {
            lastSequence = seq;
          }
          controller.enqueue(
            encoder.encode(
              `data: ${JSON.stringify({
                type: "pipeline.event",
                payload: event,
              })}\n\n`,
            ),
          );
        }
        if ((payload.jobs ?? []).length) {
          controller.enqueue(
            encoder.encode(
              `data: ${JSON.stringify({
                type: "jobs.snapshot",
                payload: payload.jobs,
              })}\n\n`,
            ),
          );
        }
      } catch (error) {
        if (abort.signal.aborted) {
          break;
        }
        controller.enqueue(
          encoder.encode(
            `: session-events-error ${(error as Error).message}\n\n`,
          ),
        );
        await this.sleep(5000);
        continue;
      }
      await this.sleep(3000);
    }
  }

  private broadcast(event: SessionEvent) {
    const payload = encoder.encode(`data: ${JSON.stringify(event)}\n\n`);
    for (const listener of [...this.listeners]) {
      try {
        listener.controller.enqueue(payload);
      } catch (error) {
        console.error("broadcast error", error);
        this.releaseListener(listener);
      }
    }
  }

  private releaseListener(listener: Listener) {
    if (listener.keepAlive) {
      clearInterval(listener.keepAlive);
    }
     if (listener.pollAbort) {
       listener.pollAbort.abort();
     }
    this.listeners.delete(listener);
  }
}
