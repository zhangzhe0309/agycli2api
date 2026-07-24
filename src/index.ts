import crypto from "node:crypto";
import cors from "cors";
import express from "express";
import type { Request, Response } from "express";
import { getToken } from "./auth.js";
import { ANTIGRAVITY_ENDPOINT_DAILY, ANTIGRAVITY_HEADERS } from "./config.js";
import { handleGenerateContent, handleListModels, fetchProject, fetchModels, buildPayload, type ModelConfig } from "./proxy.js";
import os from "os";

const app = express();
app.use(cors());
app.use(express.json({ limit: "50mb" }));

let isRandomKey = false;
if (!process.env.AGYCLI2API_KEY) {
	process.env.AGYCLI2API_KEY = crypto.randomBytes(16).toString("hex");
	isRandomKey = true;
}

// API Key Authentication Middleware
app.use((req, res, next) => {
	const expectedKey = process.env.AGYCLI2API_KEY;

	const providedKey =
		req.query.key ||
		req.headers["x-goog-api-key"] ||
		req.headers["x-api-key"] ||
		(req.headers.authorization?.startsWith("Bearer ")
			? req.headers.authorization.slice(7)
			: undefined);

	if (providedKey !== expectedKey) {
		return res
			.status(401)
			.json({ error: { message: "Unauthorized: Invalid API Key" } });
	}

	next();
});

// Standard Gemini endpoints proxy mapped to Cloud Code
app.get("/v1beta/models", handleListModels);

app.post("/v1beta/models/:modelAndAction", (req, res) => {
	const [model, action] = req.params.modelAndAction.split(":");
	const isStreaming = action === "streamGenerateContent";
	return handleGenerateContent(req, res, isStreaming, model || "");
});

// --- OpenAI-compatible /chat/completions route ---
// Hermes gemini provider sends OpenAI format when is_native_gemini_base_url() is false.
// This middleware translates OpenAI messages[] → Gemini contents[] and forwards to the native endpoint.
app.post("/chat/completions", async (req: Request, res: Response) => {
  try {
    const body = req.body;
    const messages = body.messages || [];
    const model = body.model || "gemini-3.6-flash-medium";
    const maxTokens = body.max_tokens || body.max_output_tokens || 1024;
    const temperature = body.temperature;

    // Build Gemini contents[] from OpenAI messages[]
    const contents: any[] = [];
    let systemInstruction: any = null;

    for (const msg of messages) {
      if (msg.role === "system") {
        const text = typeof msg.content === "string"
          ? msg.content
          : (msg.content as any[]).map((p: any) => p.text || "").join("");
        systemInstruction = { role: "user", parts: [{ text: `System: ${text}` }] };
        continue;
      }
      const geminiRole = msg.role === "assistant" ? "model" : "user";
      const text = typeof msg.content === "string"
        ? msg.content
        : (msg.content as any[]).map((p: any) => p.text || "").join("");
      contents.push({ role: geminiRole, parts: [{ text }] });
    }

    const generationConfig: any = {};
    if (maxTokens) generationConfig.maxOutputTokens = maxTokens;
    if (temperature !== undefined) generationConfig.temperature = temperature;

    const geminiBody: any = { contents };
    if (systemInstruction) geminiBody.systemInstruction = systemInstruction;
    if (Object.keys(generationConfig).length) geminiBody.generationConfig = generationConfig;

    // Forward to native Gemini endpoint
    const endpoint = `${ANTIGRAVITY_ENDPOINT_DAILY}/v1internal:generateContent`;
    const token = await getToken();
    const projectName = await fetchProject(token);
    const availableModels = await fetchModels(token, projectName);
    const modelConfig = availableModels[model] as ModelConfig | undefined;
    const modelEnum = modelConfig?.model || "MODEL_PLACEHOLDER_M187";

    const sessionKey = crypto.randomUUID();
    const session = {
      conversationId: crypto.randomUUID(),
      trajectoryId: crypto.randomUUID(),
      stepIndex: 3,
      sessionId: sessionKey,
      lastActive: Date.now(),
      historyHashes: new Set<string>(),
      lastUserMsgCnt: contents.filter(m => m.role === "user").length,
      lastExecutionId: null,
    };

    const payload = buildPayload(
      geminiBody, session, projectName, model, modelEnum,
      generationConfig, systemInstruction,
      session.conversationId,
    );

    const headers: Record<string, string> = {
      ...ANTIGRAVITY_HEADERS,
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    };

    const response = await fetch(endpoint, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
    });

    if (!response.ok) {
      const errText = await response.text();
      console.error(`Upstream error: ${response.status} - ${errText}`);
      return res.status(response.status).send(errText);
    }

    const data = await response.json();
    if (data?.response) {
      res.json(data.response);
    } else {
      res.json(data);
    }
  } catch (err) {
    console.error("Error in /chat/completions bridge:", err);
    res.status(500).json({ error: { message: (err as Error).message } });
  }
});

const PORT = process.env.PORT || 3403;
app.listen(PORT, () => {
	console.log(`agycli2api running on http://localhost:${PORT}`);
	console.log(
		`Using credentials from ~/.gemini/antigravity-cli/antigravity-oauth-token`,
	);
	console.log(
		`API Key authentication is ENABLED. AGYCLI2API_KEY: ${process.env.AGYCLI2API_KEY}`,
	);
	if (isRandomKey) {
		console.log(
			`(The AGYCLI2API_KEY was randomly generated because it was not specified)`,
		);
	}
});
